import { describe, it, expect } from "vitest";
import { createTestDocument, addTable, setCellText } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";

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
