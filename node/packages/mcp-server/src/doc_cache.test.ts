// FILE: node/packages/mcp-server/src/doc_cache.test.ts
// Unit tests for the server-layer projection cache (docs/PERFORMANCE.md §5.1).
import { describe, it, expect, beforeEach, afterAll } from "vitest";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync, utimesSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DocumentObject, _extractTextFromDoc } from "@adeu/core";
import { DocCache } from "./doc-cache.js";

const FIXTURE = resolve(__dirname, "../tests/fixtures/gap2_minimal_repro.docx");
const FIXTURE2 = resolve(__dirname, "../tests/fixtures/gap1_minimal_repro.docx");
const FIXTURE3 = resolve(__dirname, "../tests/fixtures/gap1_deleted_row_repro.docx");
// The only conformance fixture with a structural appendix — the one shape that
// tells the two body derivations apart (see the bundle-parity test below).
const APPENDIX_FIXTURE = resolve(
  __dirname,
  "../../../../shared/conformance/fixtures/unicode.docx",
);

const tmp = mkdtempSync(join(tmpdir(), "adeu-doccache-"));
afterAll(() => {
  try {
    rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
});

const loadDoc = (buf: Buffer, opts?: any) => DocumentObject.load(buf, opts);
const readerFor = (p: string) => () => readFileSync(p);

describe("DocCache", () => {
  let cache: DocCache;
  beforeEach(() => {
    cache = new DocCache(2);
  });

  it("ingests once per version and serves hits without re-ingesting", async () => {
    const e1 = await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    expect(cache.ingest_count).toBe(1);
    const e2 = await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    expect(cache.ingest_count).toBe(1);
    expect(e2).toBe(e1);
    expect(e1.raw_text.length).toBeGreaterThan(0);
    expect(e1.raw_bundle.pagination.total_pages).toBeGreaterThanOrEqual(1);
  });

  it("raw products equal a fresh uncached computation", async () => {
    const entry = await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    const doc = await DocumentObject.load(readFileSync(FIXTURE));
    const fresh = _extractTextFromDoc(doc, false, true) as string;
    expect(entry.raw_text).toBe(fresh);
  });

  // Python's cache projects with include_appendix=False (doc_cache.py:159-164),
  // so its body never sees the "\n\n---" rule the appendix block opens with
  // (domain.ts:369-372). Node projects WITH the appendix and splits it off, and
  // split_structural_appendix only rstrips whitespace — in BOTH engines
  // (pagination.ts:77, pagination.py:163), so the rule stays on the body side
  // and Node served 556 chars where Python serves 551. The bundle body is what
  // page='all' returns and what the response-budget guard measures, so the
  // 5-char gap changed both the payload and the refusal threshold.
  it("bundles the body Python's cache holds — no appendix separator", async () => {
    const entry = await cache.get(
      APPENDIX_FIXTURE,
      readerFor(APPENDIX_FIXTURE),
      loadDoc,
    );
    const doc = await DocumentObject.load(readFileSync(APPENDIX_FIXTURE));
    const appendix_free = _extractTextFromDoc(doc, false, false) as string;

    expect(entry.raw_bundle.appendix).toContain("READONLY_BOUNDARY_START");
    expect(entry.raw_bundle.body).toBe(appendix_free);
    expect(entry.raw_bundle.body.length).toBe(551);
    expect(entry.raw_bundle.body.endsWith("---")).toBe(false);
  });

  it("a rewritten file (new mtime/size) re-ingests; an untouched one does not", async () => {
    const p = join(tmp, "mutating.docx");
    copyFileSync(FIXTURE, p);
    const a = await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(1);

    // Same content, same size — but a bumped mtime must invalidate.
    const later = new Date(Date.now() + 5_000);
    utimesSync(p, later, later);
    const b = await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(2);
    expect(b).not.toBe(a);
    expect(b.raw_text).toBe(a.raw_text);

    // Different content entirely.
    copyFileSync(FIXTURE2, p);
    const c = await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(3);
    expect(c.raw_text).not.toBe(b.raw_text);
  });

  // A size-neutral rewrite inside one filesystem timestamp tick produces the
  // SAME (path, mtime, size) key as the pre-write file, so the stat-derived
  // key cannot notice it and `store()`'s one-version-per-path eviction never
  // runs. Tools that write a document must evict explicitly — otherwise a
  // read-after-write is served the pre-write projection, and a chained edit
  // takes a pre-write hot DOM and saves it back over the new file.
  // Whole seconds: st.mtimeMs is fractional on some filesystems while
  // utimesSync writes integer milliseconds, so only an already-normalized
  // timestamp can be restored byte-exactly to reproduce a key.
  const PINNED_MTIME = new Date(1_700_000_000_000);
  const pinMtime = (p: string) => utimesSync(p, PINNED_MTIME, PINNED_MTIME);

  it("invalidate() drops products even when the stat key is unchanged", async () => {
    const p = join(tmp, "colliding-key.docx");
    copyFileSync(FIXTURE, p);
    pinMtime(p);
    await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(1);

    // Rewrite with identical size, then restore the same mtime: keyFor() now
    // yields a key identical to the one already cached.
    copyFileSync(FIXTURE, p);
    pinMtime(p);
    expect(statSync(p).mtimeMs).toBe(PINNED_MTIME.getTime());
    await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(1); // collision: stale entry still served

    cache.invalidate(p);
    await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(2);
  });

  it("invalidate() purges entries cached under EARLIER versions of the path", async () => {
    const p = join(tmp, "invalidate-versions.docx");
    copyFileSync(FIXTURE, p);
    pinMtime(p);
    const v1 = await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(1);

    copyFileSync(FIXTURE2, p); // different content and size -> different key
    await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(2);

    cache.invalidate(p);

    // Restore v1 byte-for-byte including its mtime, so keyFor() reproduces
    // v1's key exactly. It must have been purged, not merely shadowed.
    copyFileSync(FIXTURE, p);
    pinMtime(p);
    const again = await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(3);
    expect(again).not.toBe(v1);
  });

  it("invalidate() releases the hot DOM so a chained edit cannot save stale bytes", async () => {
    const p = join(tmp, "invalidate-hot.docx");
    copyFileSync(FIXTURE, p);
    const doc = await DocumentObject.load(readFileSync(p));
    cache.restoreHotDoc(p, doc);
    expect(await cache.takeHotDoc(p)).toBe(doc);

    cache.restoreHotDoc(p, doc);
    cache.invalidate(p);
    expect(await cache.takeHotDoc(p)).toBeNull();
  });

  it("keeps one live version per path (no stale sibling entries)", async () => {
    const p = join(tmp, "versioned.docx");
    copyFileSync(FIXTURE, p);
    await cache.get(p, readerFor(p), loadDoc);
    copyFileSync(FIXTURE2, p);
    await cache.get(p, readerFor(p), loadDoc);
    // Cache cap is 2; if both versions of the same path were retained the
    // next distinct file would evict one of them. Instead the second slot
    // must still be free:
    await cache.get(FIXTURE3, readerFor(FIXTURE3), loadDoc);
    expect(cache.ingest_count).toBe(3);
    // Re-reading the versioned path is still a hit (was not evicted).
    await cache.get(p, readerFor(p), loadDoc);
    expect(cache.ingest_count).toBe(3);
  });

  it("evicts least-recently-used beyond capacity", async () => {
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc); // A
    await cache.get(FIXTURE2, readerFor(FIXTURE2), loadDoc); // B
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc); // A bumped
    await cache.get(FIXTURE3, readerFor(FIXTURE3), loadDoc); // C evicts B
    expect(cache.ingest_count).toBe(3);
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc); // A still hit
    expect(cache.ingest_count).toBe(3);
    await cache.get(FIXTURE2, readerFor(FIXTURE2), loadDoc); // B re-ingests
    expect(cache.ingest_count).toBe(4);
  });

  it("concurrent cold reads single-flight into one ingest", async () => {
    const [a, b, c] = await Promise.all([
      cache.get(FIXTURE, readerFor(FIXTURE), loadDoc),
      cache.get(FIXTURE, readerFor(FIXTURE), loadDoc),
      cache.get(FIXTURE, readerFor(FIXTURE), loadDoc),
    ]);
    expect(cache.ingest_count).toBe(1);
    expect(b).toBe(a);
    expect(c).toBe(a);
  });

  it("background clean fill lands and matches a fresh clean extraction", async () => {
    const entry = await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    const clean = await cache.ensureCleanText(entry, readerFor(FIXTURE), loadDoc);

    const doc = await DocumentObject.load(readFileSync(FIXTURE));
    const fresh = _extractTextFromDoc(doc, true, true) as string;
    expect(clean).toBe(fresh);

    // Second call is a pure field read.
    const again = await cache.ensureCleanText(entry, readerFor(FIXTURE), loadDoc);
    expect(again).toBe(clean);
  });

  it("missing file surfaces the reader's own error", async () => {
    const missing = join(tmp, "nope.docx");
    const reader = () => {
      throw new Error(`file not found: ${missing}; Provide an absolute path.`);
    };
    await expect(cache.get(missing, reader, loadDoc)).rejects.toThrow(
      /file not found/,
    );
  });

  it("reports progress during a cold ingest, never on a hit", async () => {
    const messages: string[] = [];
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc, async (m) => {
      messages.push(m);
    });
    expect(messages.length).toBeGreaterThan(0);
    expect(messages[messages.length - 1]).toBe("done");

    const hitMessages: string[] = [];
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc, async (m) => {
      hitMessages.push(m);
    });
    expect(hitMessages).toEqual([]);
  });
});
