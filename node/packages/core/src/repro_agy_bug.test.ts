import { describe, it, expect } from "vitest";
import { createTestDocument, addTable, setCellText } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";

describe("QA Regression Test - Finding F1: Missing cell anchors for empty table cells", () => {
  it("should generate and render stable cell anchors for empty table cells", async () => {
    // 1. Build a document containing a table with empty/blank cells.
    // In this document, the cells are constructed programmatically without any pre-existing w14:paraId.
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "Hello");
    // Cell (0,1) is left completely empty, with no pre-existing w14:paraId attribute on its w:p element.

    const buf = await doc.save();

    // 2. Ingest/extract text from the document.
    const text = await extractTextFromBuffer(buf, false);

    // 3. Assert correct behavior.
    // The empty cell must still render a trailing {#cell:<id>} anchor so that it can be targeted for edits.
    // We expect the output to be formatted like: Hello | {#cell:<id>}
    const cellAnchorRegex = /Hello \| \{#cell:[0-9a-fA-F]{8}\}/;
    expect(text).toSatisfy((val: string) => {
      return cellAnchorRegex.test(val);
    }, `Expected extracted text to contain a cell anchor for the empty cell, but got:\n"${text}"`);
  });
});

describe("QA Regression Test - Top Bug: match_mode='all' fails to affect multiple matches for table row edits", () => {
  it("should delete all rows containing target_text when match_mode is all", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 3);
    setCellText(tbl, 0, 0, "ID");
    setCellText(tbl, 0, 1, "Name");
    setCellText(tbl, 0, 2, "Notes");

    setCellText(tbl, 1, 0, "1");
    setCellText(tbl, 1, 1, "Alice");
    setCellText(tbl, 1, 2, "First record");

    setCellText(tbl, 2, 0, "2");
    setCellText(tbl, 2, 1, "");
    setCellText(tbl, 2, 2, "Second record with empty name");

    const buf = await doc.save();
    const midDoc = await (doc.constructor as any).load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([
      {
        type: "delete_row",
        target_text: "record",
        match_mode: "all",
      } as any,
    ]);

    expect(stats.edits_applied).toBe(1);
    expect(stats.occurrences_modified).toBe(2);

    engine.accept_all_revisions();
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).not.toContain("First record");
    expect(clean_text).not.toContain("Second record with empty name");
  });

  it("should insert rows adjacent to all rows containing target_text when match_mode is all", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 3);
    setCellText(tbl, 0, 0, "ID");
    setCellText(tbl, 0, 1, "Name");
    setCellText(tbl, 0, 2, "Notes");

    setCellText(tbl, 1, 0, "1");
    setCellText(tbl, 1, 1, "Alice");
    setCellText(tbl, 1, 2, "First record");

    setCellText(tbl, 2, 0, "2");
    setCellText(tbl, 2, 1, "");
    setCellText(tbl, 2, 2, "Second record with empty name");

    const buf = await doc.save();
    const midDoc = await (doc.constructor as any).load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "record",
        match_mode: "all",
        position: "below",
        cells: ["NEW_ID", "NEW_NAME", "NEW_NOTES"],
      } as any,
    ]);

    expect(stats.edits_applied).toBe(1);
    expect(stats.occurrences_modified).toBe(2);

    engine.accept_all_revisions();
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    const occurrences = (clean_text.match(/NEW_ID \| NEW_NAME \| NEW_NOTES/g) || []).length;
    expect(occurrences).toBe(2);
  });
});
