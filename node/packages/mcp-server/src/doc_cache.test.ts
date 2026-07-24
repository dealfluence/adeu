// FILE: node/packages/mcp-server/src/doc_cache.test.ts
// Unit tests for the server-layer projection cache (docs/PERFORMANCE.md §5.1).
import { describe, it, expect, beforeEach, afterAll } from "vitest";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DocumentObject, _extractTextFromDoc } from "@adeu/core";
import { DocCache } from "./doc-cache.js";

const FIXTURE = resolve(__dirname, "../tests/fixtures/gap2_minimal_repro.docx");
const FIXTURE2 = resolve(__dirname, "../tests/fixtures/gap1_minimal_repro.docx");
const FIXTURE3 = resolve(__dirname, "../tests/fixtures/gap1_deleted_row_repro.docx");

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
