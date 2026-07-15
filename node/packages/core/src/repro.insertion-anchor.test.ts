import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";
import { createTestDocument, addParagraph } from "./test-utils.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Reproduction for the insertion-anchor bug (2026-07, golden.docx):
// ModifyText("document" -> "finalized document") is diff-minimized to a
// zero-width INSERTION of "finalized " immediately before "document". The
// target paragraph already carries tracked changes ({--initial --}
// {++golden ++}) and three comment ranges, so in the raw projection the
// insertion index sits right after a virtual {>>...<<} meta block.
// get_insertion_anchor only looked for a run-backed span ENDING exactly at
// that index; every span ending there is virtual, so it fell back to
// (null, paragraph) and the engine dropped the new <w:ins> at the START of
// the paragraph — "finalized This is the golden document" after accept-all.
// The fix anchors after the nearest preceding REAL run in the same
// paragraph (mirrors the Python engine/mapper fix).
describe("Zero-width insertion anchoring with preceding redlines/comments", () => {
  it("lands the insertion before its target, not at paragraph start (golden.docx)", async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );
    const buf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "T");

    const stats = engine.process_batch([
      { type: "modify", target_text: "document", new_text: "finalized document" },
    ]);
    expect(stats.edits_applied).toBe(1);

    const out = await doc.save();
    const clean = await extractTextFromBuffer(out, true);
    expect(clean).toContain("golden finalized document");
    expect(clean.startsWith("finalized This is")).toBe(false);

    const red = await extractTextFromBuffer(out);
    expect(red.startsWith("{++finalized")).toBe(false);
  });

  it("does not nest <w:ins> inside <w:del> when the anchor run is tracked-deleted", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is the initial document");

    // Pass 1: track-delete "initial " so the raw projection becomes
    // "This is the {--initial --}{>>[Chg:1 delete] T<<}document".
    const engine1 = new RedlineEngine(doc, "T");
    engine1.process_batch([
      { type: "modify", target_text: "initial ", new_text: "" },
    ]);
    const deleted = await doc.save();

    // Pass 2: zero-width insertion right before "document".
    const doc2 = await DocumentObject.load(deleted);
    const engine2 = new RedlineEngine(doc2, "T2");
    engine2.process_batch([
      { type: "modify", target_text: "document", new_text: "finalized document" },
    ]);

    const dels = Array.from(doc2.element.getElementsByTagName("w:del"));
    for (const del of dels) {
      expect(del.getElementsByTagName("w:ins").length).toBe(0);
    }

    const out = await doc2.save();
    const clean = await extractTextFromBuffer(out, true);
    expect(clean).toContain("This is the finalized document");
  });

  it("keeps genuine paragraph-start insertions at the paragraph start", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Alpha beta gamma");
    const engine = new RedlineEngine(doc, "T");

    engine.process_batch([
      { type: "modify", target_text: "Alpha", new_text: "Intro Alpha" },
    ]);

    const out = await doc.save();
    const clean = await extractTextFromBuffer(out, true);
    expect(clean).toContain("Intro Alpha beta gamma");
  });
});
