// FILE: node/packages/mcp-server/src/doc-cache.ts
/**
 * Server-layer projection cache (docs/PERFORMANCE.md §5.1).
 *
 * read_docx used to re-run the whole pipeline (unzip → parse every XML part →
 * project → paginate) on EVERY call — a page turn on a 45 MB document.xml
 * cost ~16 s of work identical to the previous call's. This cache keeps the
 * finished PRODUCTS of one ingest (projected text, pagination, outline
 * nodes) keyed by (resolved path, mtime, size), so any later read of the
 * same document VERSION is string slicing.
 *
 * Deliberate properties:
 * - The parsed DOM is never cached (gigabytes); only its products (a few MB).
 * - Freshness is stat-checked on every call: any rewrite changes mtime/size,
 *   so a new document version can never be served stale. (Accepted edge: a
 *   rewrite that preserves BOTH mtime and size — e.g. some sync tools —
 *   is indistinguishable without hashing the file on every page turn.)
 * - Responses must be byte-identical cached vs uncached — this module only
 *   precomputes what response-builders would compute from the same text.
 * - Clean-view text is filled in the BACKGROUND after the first response is
 *   sent (single-flight), so the first read pays only for what it returns,
 *   and a later clean_view request finds the text already warm.
 * - Cache lives in the MCP server layer only; @adeu/core stays stateless
 *   (cross-engine parity: the Python server can mirror this 1:1).
 */

import { statSync } from "node:fs";
import { resolve } from "node:path";
import { getHeapStatistics } from "node:v8";
import {
  DocumentObject,
  _extractTextFromDoc,
  extract_outline,
  OutlineNode,
  paginate,
  split_structural_appendix,
} from "@adeu/core";
import type { ProjectionBundle } from "./response-builders.js";

export type ProgressFn = (
  message: string,
  progress: number,
  total: number,
) => void | Promise<void>;

/**
 * Boundary-owned loader: the handler passes its loadDocxOrThrow wrapper so
 * container errors keep their agent-facing diagnosis (QA 2026-07-23 F19)
 * and the cache stays free of error-shaping policy.
 */
export type LoadDocFn = (
  buf: Buffer,
  opts?: Parameters<typeof DocumentObject.load>[1],
) => Promise<DocumentObject>;

export interface DocCacheEntry {
  key: string;
  file_path: string;
  /** Raw projection, includeAppendix=true — what full/search/appendix modes read. */
  raw_text: string;
  raw_bundle: ProjectionBundle;
  outline_nodes: OutlineNode[];
  /** Clean projection (accepted view); null until the background fill lands. */
  clean_text: string | null;
  /** Lazily derived on the first clean_view paginated/search read. */
  clean_bundle: ProjectionBundle | null;
  /** Clean-view heading map; lazily derived (see ensureCleanOutline). */
  clean_outline_nodes: OutlineNode[] | null;
  /** In-flight background clean fill; ensureCleanText awaits it. */
  clean_fill: Promise<void> | null;
  /** Set by ensureCleanText: skip the quiet-period wait and fill NOW. */
  _fill_forced?: boolean;
}

function makeBundle(text: string): ProjectionBundle {
  const [body, appendix] = split_structural_appendix(text);
  return { body, appendix, pagination: paginate(body, "") };
}

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class DocCache {
  private entries = new Map<string, DocCacheEntry>();
  private inflight = new Map<string, Promise<DocCacheEntry>>();
  /** Observability for tests: how many full ingests ran. */
  public ingest_count = 0;
  /** Timestamp of the most recent cache request — the clean fill's
   * quietness signal (the extraction is one synchronous block, so it must
   * not start while requests are actively arriving). */
  private lastTouch = 0;

  /**
   * Hot-DOM slot (docs/PERFORMANCE.md §5 "stop re-parsing"): the parsed
   * DocumentObject of the most recently ingested/edited document version,
   * kept so the NEXT operation on the same version (typically a
   * process_document_batch right after a read, or a chained edit on a batch
   * output) skips the multi-second disk parse entirely.
   *
   * Single slot, consume-on-take (an edit mutates the DOM, so it can never
   * be shared), TTL-evicted, and guarded by a heap-headroom check so a
   * multi-GB DOM is only pinned when there is room for it.
   */
  private hot: {
    key: string;
    doc: DocumentObject;
    /** Background jobs still reading this DOM (clean fill, prime build).
     * takeHotDoc forces and awaits them all before handing the DOM to a
     * mutating consumer — otherwise a deferred fill would later extract a
     * half-edited document into the cache. */
    jobs: Array<{ force: () => void; promise: Promise<unknown> }>;
  } | null = null;
  private hotTimer: ReturnType<typeof setTimeout> | null = null;
  /** Test observability: how many edits reused a hot DOM. */
  public hot_hits = 0;
  /** Priming jobs by key: a read arriving mid-prime joins instead of
   * re-ingesting from disk. */
  private priming = new Map<
    string,
    { promise: Promise<DocCacheEntry>; force: () => void }
  >();

  constructor(
    private maxEntries: number = 3,
    private hotTtlMs: number = 3 * 60_000,
  ) {}

  /**
   * Heap headroom check for the pin-release VALVE. Pinning a DOM never
   * grows the heap (it only delays release of memory the ingest already
   * allocated), so storing is always safe; the hazard is holding a pinned
   * multi-GB tree WHILE a new ingest allocates another one. Before any
   * ingest, the pin is dropped when headroom is low.
   */
  private heapHasRoom(): boolean {
    try {
      const limit = getHeapStatistics().heap_size_limit;
      return process.memoryUsage().heapUsed < limit * 0.55;
    } catch {
      return false;
    }
  }

  /** Drop the pinned DOM if a new multi-GB allocation needs the room. */
  private releaseHotIfPressured(): void {
    if (this.hot && !this.heapHasRoom()) {
      this.hot = null;
      if (this.hotTimer) {
        clearTimeout(this.hotTimer);
        this.hotTimer = null;
      }
    }
  }

  private storeHotDoc(key: string, doc: DocumentObject): void {
    if (this.hotTimer) clearTimeout(this.hotTimer);
    this.hot = { key, doc, jobs: [] };
    this.hotTimer = setTimeout(() => {
      this.hot = null;
      this.hotTimer = null;
    }, this.hotTtlMs);
    // Never keep the server process alive just to babysit a cached DOM.
    (this.hotTimer as any).unref?.();
  }

  /** Attach a background job to the hot slot iff it still holds `doc`. */
  private registerHotJob(
    doc: DocumentObject,
    force: () => void,
    promise: Promise<unknown>,
  ) {
    if (this.hot && this.hot.doc === doc) {
      this.hot.jobs.push({ force, promise });
    }
  }

  /**
   * Hand the parsed DOM of the CURRENT version of `file_path` to a mutating
   * consumer (the edit path), or null when the slot holds a different
   * version/file. Consume-on-take: the slot is cleared; all background jobs
   * reading the DOM are forced to completion first.
   */
  public async takeHotDoc(file_path: string): Promise<DocumentObject | null> {
    this.lastTouch = Date.now();
    if (!this.hot) return null;
    let key: string;
    try {
      key = this.keyFor(resolve(file_path));
    } catch {
      return null;
    }
    if (this.hot.key !== key) return null;
    const slot = this.hot;
    this.hot = null;
    if (this.hotTimer) {
      clearTimeout(this.hotTimer);
      this.hotTimer = null;
    }
    for (const job of slot.jobs) {
      try {
        job.force();
      } catch {
        /* forcing is best-effort */
      }
    }
    await Promise.all(slot.jobs.map((j) => j.promise.catch(() => {})));
    this.hot_hits++;
    return slot.doc;
  }

  /**
   * Put a DOM back in the slot after an operation that provably left it
   * equal to the on-disk file (a rolled-back failed batch — restored via
   * the engine's transactional snapshot).
   *
   * "Provably" is the caller's obligation, and it is not satisfied by the
   * operation merely having thrown: the engine reports whether its rollback
   * verified (RedlineEngine.rollback_verified), and only that answer makes a
   * post-failure DOM safe to re-pin. Pinning an unverified one publishes a
   * half-applied document under the UNCHANGED file's cache key, where the
   * next call picks it up as if it had come from disk.
   */
  public restoreHotDoc(file_path: string, doc: DocumentObject): void {
    try {
      const key = this.keyFor(resolve(file_path));
      this.storeHotDoc(key, doc);
    } catch {
      /* file gone — nothing to pin */
    }
  }

  /** Stat-derived identity of the CURRENT file version. */
  private keyFor(resolvedPath: string): string {
    const st = statSync(resolvedPath);
    return `${resolvedPath}|${st.mtimeMs}|${st.size}`;
  }

  /**
   * Drop every cached product for `resolvedPath`, whatever version it was
   * keyed under.
   *
   * `store()` already keeps one live version per path, but only when a NEW
   * version is stored. That is exactly what a colliding key defeats: when a
   * rewrite preserves both mtime and size — a size-neutral edit inside one
   * filesystem timestamp tick — no new entry is ever built, so `get()` keeps
   * returning the pre-write entry (its `entries` hit-check runs before the
   * `priming` join). Writers must therefore evict explicitly.
   *
   * Entries carry the resolved path, so they are matched exactly, as in
   * `store()`. The hot/priming/inflight maps are keyed by string only, and
   * keyFor always emits `${resolvedPath}|…`, so a whole-prefix compare is
   * exact there — a '|' inside a POSIX filename can neither miss nor
   * over-match.
   */
  private purgePath(resolvedPath: string, opts?: { keepHot?: boolean }): void {
    const prefix = `${resolvedPath}|`;
    for (const [key, entry] of [...this.entries]) {
      if (entry.file_path === resolvedPath) this.entries.delete(key);
    }
    for (const key of [...this.priming.keys()]) {
      if (key.startsWith(prefix)) this.priming.delete(key);
    }
    for (const key of [...this.inflight.keys()]) {
      if (key.startsWith(prefix)) this.inflight.delete(key);
    }
    if (!opts?.keepHot && this.hot && this.hot.key.startsWith(prefix)) {
      this.hot = null;
      if (this.hotTimer) {
        clearTimeout(this.hotTimer);
        this.hotTimer = null;
      }
    }
  }

  /**
   * Forget everything cached for `file_path`. Callers that write a document
   * without handing the post-write DOM to primeFromDoc MUST call this, or a
   * later read can be served the pre-write projection.
   */
  public invalidate(file_path: string): void {
    this.purgePath(resolve(file_path));
  }

  /**
   * Returns the products for the current version of `file_path`, ingesting
   * at most once per version (single-flight). `readBytes` is called only on
   * a miss and owns the file-not-found error shape; `onProgress` is invoked
   * only during a cold ingest.
   */
  public async get(
    file_path: string,
    readBytes: () => Buffer,
    loadDoc: LoadDocFn,
    onProgress?: ProgressFn,
  ): Promise<DocCacheEntry> {
    this.lastTouch = Date.now();
    const resolvedPath = resolve(file_path);
    let key: string;
    try {
      key = this.keyFor(resolvedPath);
    } catch {
      // Missing/unreadable: let the caller's reader throw its lean,
      // agent-appropriate error (with sibling listing).
      readBytes();
      throw new Error(`Cannot stat file: ${resolvedPath}`);
    }

    const hit = this.entries.get(key);
    if (hit) {
      // LRU bump: re-insert to move to most-recent position.
      this.entries.delete(key);
      this.entries.set(key, hit);
      return hit;
    }

    // A prime job for this exact version may be pending (output of a batch
    // that just wrote this file): join it instead of re-parsing from disk.
    const prime = this.priming.get(key);
    if (prime) {
      prime.force();
      const primed = await prime.promise.catch(() => null);
      if (primed) {
        this.entries.delete(primed.key);
        this.entries.set(primed.key, primed);
        return primed;
      }
    }

    const pending = this.inflight.get(key);
    if (pending) return pending;

    const job = this.ingest(
      key,
      resolvedPath,
      readBytes,
      loadDoc,
      onProgress,
    ).finally(() => this.inflight.delete(key));
    this.inflight.set(key, job);
    return job;
  }

  private async ingest(
    key: string,
    resolvedPath: string,
    readBytes: () => Buffer,
    loadDoc: LoadDocFn,
    onProgress?: ProgressFn,
  ): Promise<DocCacheEntry> {
    this.ingest_count++;
    // Valve: parsing may allocate a multi-GB tree — release any pinned DOM
    // first when heap headroom is low, instead of holding two at once.
    this.releaseHotIfPressured();
    const notify = async (m: string, p: number) => {
      if (onProgress) {
        try {
          await onProgress(m, p, 100);
        } catch {
          /* progress must never fail a read */
        }
      }
    };

    await notify("reading file", 2);
    const buf = readBytes();

    const doc = await loadDoc(buf, {
      onPart: onProgress
        ? async (done: number, total: number) => {
            // Parts parsing spans ~2-70 on the progress scale.
            const pct = 5 + Math.floor((done / Math.max(1, total)) * 65);
            await notify(`parsing part ${done}/${total}`, pct);
          }
        : undefined,
    });

    const entry = await this.buildEntry(key, resolvedPath, doc, notify);

    // Pin the DOM for the edit path BEFORE the deferred fill: an edit arriving
    // next takes the slot and (via the registered job) forces the fill to
    // completion first, so the fill can never read a half-edited document.
    //
    // Only the disk-ingest path may do this. primeFromDoc pins its own DOM up
    // front, and its build job runs INSIDE takeHotDoc's forcing loop — see
    // buildEntry.
    this.storeHotDoc(key, doc);
    this.scheduleCleanFill(entry, doc);
    return entry;
  }

  /**
   * Products of one loaded document: shared by the disk-ingest path and by
   * primeFromDoc (which starts from the in-memory post-edit document).
   *
   * Deliberately does NOT pin the DOM or schedule the clean fill — the caller
   * decides. When primeFromDoc's deferred job runs, it may be running because
   * takeHotDoc forced it, and takeHotDoc has already cleared the hot slot and
   * is about to hand this DOM to a mutating consumer. Pinning here would
   * re-publish a DOM that is about to be edited, and scheduleCleanFill's
   * registerHotJob would silently no-op (the slot is null), leaving an
   * unguarded fill that later extracts the half-edited document and stores it
   * as the clean projection of the PRE-edit version.
   */
  private async buildEntry(
    key: string,
    resolvedPath: string,
    doc: DocumentObject,
    notify?: (m: string, p: number) => Promise<void>,
  ): Promise<DocCacheEntry> {
    const n = notify ?? (async () => {});
    await n("projecting text", 75);
    const extract_res = _extractTextFromDoc(doc, false, true, true) as {
      text: string;
      paragraph_offsets: Map<any, [number, number]>;
    };

    await n("paginating", 88);
    const raw_bundle = makeBundle(extract_res.text);

    await n("building outline", 93);
    const outline_nodes = extract_outline(
      doc,
      raw_bundle.body,
      raw_bundle.pagination.body_pages,
      raw_bundle.pagination.body_page_offsets,
      extract_res.paragraph_offsets,
    );

    const entry: DocCacheEntry = {
      key,
      file_path: resolvedPath,
      raw_text: extract_res.text,
      raw_bundle,
      outline_nodes,
      clean_text: null,
      clean_bundle: null,
      clean_outline_nodes: null,
      clean_fill: null,
    };
    this.store(entry);
    await n("done", 100);
    return entry;
  }

  /**
   * Background warm-up of the clean view AFTER the caller's response is
   * flushed. The clean extraction is ONE synchronous block (seconds on a
   * huge document), so it must not start while requests are still
   * arriving — the VVBIG bench caught an innocent warm page-turn stalling
   * 2.1 s behind it. Wait for a quiet period (no cache request for
   * QUIET_MS), bounded by MAX_WAIT_MS; ensureCleanText sets _fill_forced
   * to skip the wait — the clean_view requester pays for clean view,
   * nobody else does. Failures leave clean_text null — the on-demand path
   * in ensureCleanText rebuilds from bytes instead.
   */
  private scheduleCleanFill(entry: DocCacheEntry, doc: DocumentObject): void {
    const QUIET_MS = 400;
    const MAX_WAIT_MS = 30_000;
    let docRef: DocumentObject | null = doc;
    entry.clean_fill = (async () => {
      try {
        // Quiet = QUIET_MS elapsed since the LATER of (fill became eligible,
        // last cache request). lastTouch alone is wrong here: after a long
        // ingest it is already stale, and the fill would start immediately —
        // exactly on top of the page-2 request that typically follows.
        const eligibleAt = Date.now();
        const started = eligibleAt;
        while (
          !entry._fill_forced &&
          Date.now() - Math.max(this.lastTouch, eligibleAt) < QUIET_MS &&
          Date.now() - started < MAX_WAIT_MS
        ) {
          await delay(100);
        }
        entry.clean_text = _extractTextFromDoc(docRef!, true, true) as string;
      } catch {
        entry.clean_text = null;
      } finally {
        docRef = null; // the hot slot (if any) now owns the DOM's lifetime
        entry.clean_fill = null;
      }
    })();
    this.registerHotJob(
      doc,
      () => {
        entry._fill_forced = true;
      },
      entry.clean_fill,
    );
  }

  /**
   * After a successful batch write: adopt the in-memory post-edit document
   * as the cache state of the OUTPUT file. The DOM is pinned for chained
   * edits immediately; the text products are built after a quiet period
   * (forced early if a read arrives and joins via the priming map). The
   * agent's read-after-edit therefore never re-parses what the server just
   * had in memory.
   *
   * SAFETY GATE: primed products must byte-equal what a fresh parse of the
   * written file would produce — guaranteed by the deterministic
   * serialize→parse round-trip and enforced by the prime-equivalence tests.
   */
  public primeFromDoc(file_path: string, doc: DocumentObject): void {
    this.lastTouch = Date.now();
    const resolvedPath = resolve(file_path);
    let key: string;
    try {
      key = this.keyFor(resolvedPath);
    } catch {
      return; // file vanished — nothing to prime
    }
    // Evict any products cached for an EARLIER version of this path first.
    // Without this, a rewrite that preserved mtime+size leaves a stale entry
    // under this same key, and get()'s entries hit-check would return it in
    // preference to the prime job registered just below.
    this.purgePath(resolvedPath, { keepHot: true });
    this.storeHotDoc(key, doc);

    let forced = false;
    const QUIET_MS = 400;
    const MAX_WAIT_MS = 30_000;
    const job = (async () => {
      const eligibleAt = Date.now();
      const started = eligibleAt;
      while (
        !forced &&
        Date.now() - Math.max(this.lastTouch, eligibleAt) < QUIET_MS &&
        Date.now() - started < MAX_WAIT_MS
      ) {
        await delay(100);
      }
      return await this.buildEntry(key, resolvedPath, doc);
    })();
    const rec = {
      promise: job,
      force: () => {
        forced = true;
      },
    };
    this.priming.set(key, rec);
    job
      .catch(() => {})
      .finally(() => {
        if (this.priming.get(key) === rec) this.priming.delete(key);
      });
    this.registerHotJob(doc, rec.force, job);
  }

  /**
   * Clean-view text for an entry: already warm, else await the in-flight
   * background fill, else (fill failed / entry from a crashed fill) rebuild
   * from bytes without caching a DOM.
   */
  public async ensureCleanText(
    entry: DocCacheEntry,
    readBytes: () => Buffer,
    loadDoc: LoadDocFn,
  ): Promise<string> {
    this.lastTouch = Date.now();
    if (entry.clean_text !== null) return entry.clean_text;
    if (entry.clean_fill) {
      // The requester of clean view pays for it now — skip the quiet wait.
      entry._fill_forced = true;
      await entry.clean_fill;
    }
    if (entry.clean_text !== null) return entry.clean_text;
    const doc = await loadDoc(readBytes());
    entry.clean_text = _extractTextFromDoc(doc, true, true) as string;
    return entry.clean_text;
  }

  /** Clean-view bundle, derived once from the clean text. */
  public async ensureCleanBundle(
    entry: DocCacheEntry,
    readBytes: () => Buffer,
    loadDoc: LoadDocFn,
  ): Promise<ProjectionBundle> {
    if (!entry.clean_bundle) {
      entry.clean_bundle = makeBundle(
        await this.ensureCleanText(entry, readBytes, loadDoc),
      );
    }
    return entry.clean_bundle;
  }

  /**
   * Clean-view heading map. The raw outline cannot stand in for it: its page
   * numbers and CriticMarkup-bearing heading text describe a different
   * projection. Python fills outline nodes per VIEW (doc_cache.py:155-188), so
   * both engines' clean-view readers get a real heading map.
   *
   * Costs a parse: the paragraph-offset map the extractor needs references
   * live elements from ITS OWN parse, so it can never be derived from the
   * cached clean string. Cached on the entry, so at most once per version.
   */
  public async ensureCleanOutline(
    entry: DocCacheEntry,
    readBytes: () => Buffer,
    loadDoc: LoadDocFn,
  ): Promise<OutlineNode[]> {
    this.lastTouch = Date.now();
    if (entry.clean_outline_nodes) return entry.clean_outline_nodes;
    const doc = await loadDoc(readBytes());
    const extract_res = _extractTextFromDoc(doc, true, true, true) as {
      text: string;
      paragraph_offsets: Map<any, [number, number]>;
    };
    if (entry.clean_text === null) entry.clean_text = extract_res.text;
    if (!entry.clean_bundle) entry.clean_bundle = makeBundle(extract_res.text);
    const bundle = entry.clean_bundle;
    entry.clean_outline_nodes = extract_outline(
      doc,
      bundle.body,
      bundle.pagination.body_pages,
      bundle.pagination.body_page_offsets,
      extract_res.paragraph_offsets,
    );
    return entry.clean_outline_nodes;
  }

  private store(entry: DocCacheEntry) {
    // One live version per path: a new version of the same file replaces the
    // old entry instead of coexisting with it.
    for (const [k, e] of this.entries) {
      if (e.file_path === entry.file_path && k !== entry.key) {
        this.entries.delete(k);
      }
    }
    this.entries.set(entry.key, entry);
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value as string;
      this.entries.delete(oldest);
    }
  }

  public clear() {
    this.entries.clear();
    this.inflight.clear();
    this.priming.clear();
    this.hot = null;
    if (this.hotTimer) {
      clearTimeout(this.hotTimer);
      this.hotTimer = null;
    }
  }
}

/** Process-wide cache: one stdio server serves one session. */
export const docCache = new DocCache(3);
