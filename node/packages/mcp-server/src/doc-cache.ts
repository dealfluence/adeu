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

  constructor(private maxEntries: number = 3) {}

  /** Stat-derived identity of the CURRENT file version. */
  private keyFor(resolvedPath: string): string {
    const st = statSync(resolvedPath);
    return `${resolvedPath}|${st.mtimeMs}|${st.size}`;
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

    let doc: DocumentObject | null = await loadDoc(buf, {
      onPart: onProgress
        ? async (done: number, total: number) => {
            // Parts parsing spans ~2-70 on the progress scale.
            const pct = 5 + Math.floor((done / Math.max(1, total)) * 65);
            await notify(`parsing part ${done}/${total}`, pct);
          }
        : undefined,
    });

    await notify("projecting text", 75);
    const extract_res = _extractTextFromDoc(doc, false, true, true) as {
      text: string;
      paragraph_offsets: Map<any, [number, number]>;
    };

    await notify("paginating", 88);
    const raw_bundle = makeBundle(extract_res.text);

    await notify("building outline", 93);
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
      clean_fill: null,
    };
    this.store(entry);
    await notify("done", 100);

    // Background warm-up of the clean view AFTER the caller's response is
    // flushed. The clean extraction is ONE synchronous block (seconds on a
    // huge document), so it must not start while requests are still
    // arriving — the VVBIG bench caught an innocent warm page-turn stalling
    // 2.1 s behind it. Wait for a quiet period (no cache request for
    // QUIET_MS), bounded by MAX_WAIT_MS; ensureCleanText sets _fill_forced
    // to skip the wait — the clean_view requester pays for clean view,
    // nobody else does. Failures leave clean_text null — the on-demand path
    // in ensureCleanText rebuilds from bytes instead.
    const QUIET_MS = 400;
    const MAX_WAIT_MS = 30_000;
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
        entry.clean_text = _extractTextFromDoc(doc!, true, true) as string;
      } catch {
        entry.clean_text = null;
      } finally {
        doc = null; // release the multi-GB DOM
        entry.clean_fill = null;
      }
    })();

    return entry;
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
  }
}

/** Process-wide cache: one stdio server serves one session. */
export const docCache = new DocCache(3);
