// FILE: node/packages/mcp-server/src/hot_doc.test.ts
// Phase 2a: hot-DOM reuse + output priming (docs/PERFORMANCE.md §5).
// The safety spine: a taken DOM must never still be read by background
// fills, and primed products must byte-equal a fresh parse of the file.
import { describe, it, expect, beforeEach, afterAll } from "vitest";
import {
  readFileSync,
  writeFileSync,
  mkdtempSync,
  rmSync,
  copyFileSync,
  utimesSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  DocumentObject,
  RedlineEngine,
  _extractTextFromDoc,
} from "@adeu/core";
import { DocCache } from "./doc-cache.js";

const FIXTURE = resolve(__dirname, "../tests/fixtures/gap2_minimal_repro.docx");
const tmp = mkdtempSync(join(tmpdir(), "adeu-hotdoc-"));
afterAll(() => {
  try {
    rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
});

const loadDoc = (buf: Buffer, opts?: any) => DocumentObject.load(buf, opts);
const readerFor = (p: string) => () => readFileSync(p);

describe("hot-DOM slot", () => {
  let cache: DocCache;
  beforeEach(() => {
    cache = new DocCache(3);
  });

  it("a read pins the DOM; an edit takes it exactly once", async () => {
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    expect(cache.ingest_count).toBe(1);

    const doc = await cache.takeHotDoc(FIXTURE);
    expect(doc).not.toBeNull();
    expect(cache.hot_hits).toBe(1);

    // Consume-on-take: a second taker must re-parse.
    const again = await cache.takeHotDoc(FIXTURE);
    expect(again).toBeNull();
  });

  it("taking the DOM forces the pending clean fill first (no half-edited fills)", async () => {
    const entry = await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    // Fill is quiet-deferred — normally still pending this soon.
    const doc = await cache.takeHotDoc(FIXTURE);
    expect(doc).not.toBeNull();
    // By the time the DOM is handed out, the fill must have completed.
    expect(entry.clean_text).not.toBeNull();

    const fresh = await DocumentObject.load(readFileSync(FIXTURE));
    expect(entry.clean_text).toBe(_extractTextFromDoc(fresh, true, true));
  });

  it("a stale slot (file changed on disk) never serves", async () => {
    const p = join(tmp, "stale.docx");
    copyFileSync(FIXTURE, p);
    await cache.get(p, readerFor(p), loadDoc);
    const later = new Date(Date.now() + 5_000);
    utimesSync(p, later, later);
    expect(await cache.takeHotDoc(p)).toBeNull();
  });

  it("restoreHotDoc re-pins after a rollback/dry-run", async () => {
    await cache.get(FIXTURE, readerFor(FIXTURE), loadDoc);
    const doc = await cache.takeHotDoc(FIXTURE);
    expect(doc).not.toBeNull();
    cache.restoreHotDoc(FIXTURE, doc!);
    const again = await cache.takeHotDoc(FIXTURE);
    expect(again).toBe(doc);
  });
});

describe("output priming after a batch", () => {
  it("primed products byte-equal a fresh parse of the written file (equivalence gate)", async () => {
    const cache = new DocCache(3);
    const src = join(tmp, "prime-src.docx");
    copyFileSync(FIXTURE, src);

    // Simulate the batch handler: load, edit, save to a new file.
    const doc = await DocumentObject.load(readFileSync(src));
    const engine = new RedlineEngine(doc, "HotDoc");
    const probe = _extractTextFromDoc(doc, false, false) as string;
    // Pick a real word from the document as a target (first 6+ letter run).
    const m = probe.match(/[A-Za-z]{6,}/);
    expect(m).not.toBeNull();
    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: m![0],
          new_text: m![0] + " (primed)",
          match_mode: "first",
        },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(1);
    const out = join(tmp, "prime-out.docx");
    writeFileSync(out, await doc.save());

    cache.primeFromDoc(out, doc);

    // A read joining the prime must not re-ingest from disk...
    const entry = await cache.get(out, readerFor(out), loadDoc);
    expect(cache.ingest_count).toBe(0);

    // ...and must serve EXACTLY what a fresh parse of the file yields.
    const reloaded = await DocumentObject.load(readFileSync(out));
    const freshRaw = _extractTextFromDoc(reloaded, false, true) as string;
    expect(entry.raw_text).toBe(freshRaw);

    const clean = await cache.ensureCleanText(entry, readerFor(out), loadDoc);
    const reloaded2 = await DocumentObject.load(readFileSync(out));
    expect(clean).toBe(_extractTextFromDoc(reloaded2, true, true));
  });

  it("a chained edit takes the primed DOM without any disk parse", async () => {
    const cache = new DocCache(3);
    const src = join(tmp, "chain-src.docx");
    copyFileSync(FIXTURE, src);

    const doc = await DocumentObject.load(readFileSync(src));
    const engine = new RedlineEngine(doc, "HotDoc");
    const probe = _extractTextFromDoc(doc, false, false) as string;
    const m = probe.match(/[A-Za-z]{6,}/);
    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: m![0],
          new_text: m![0] + " (chain)",
          match_mode: "first",
        },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(1);
    const out = join(tmp, "chain-out.docx");
    writeFileSync(out, await doc.save());

    cache.primeFromDoc(out, doc);
    const taken = await cache.takeHotDoc(out);
    expect(taken).toBe(doc);
    expect(cache.ingest_count).toBe(0);

    // Prime build was forced by the take — the entry must exist and match a
    // fresh parse (products were computed BEFORE the taker mutates).
    const entry = await cache.get(out, readerFor(out), loadDoc);
    expect(cache.ingest_count).toBe(0);
    const reloaded = await DocumentObject.load(readFileSync(out));
    expect(entry.raw_text).toBe(_extractTextFromDoc(reloaded, false, true));
  });
});
