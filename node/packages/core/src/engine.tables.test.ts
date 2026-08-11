import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
  mergeCells,
  addNestedTable,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants, findChild } from "./docx/dom.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import {
  ModifyText,
  InsertTableRow,
  DeleteTableRow,
  RejectChange,
} from "./models.js";

describe("Table Interop & Engine (Node.js Port)", () => {
  it("interleaved tables and text remain ordered", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Section 1");
    const tbl = addTable(doc, 1, 1);
    setCellText(tbl, 0, 0, "TableContent");
    addParagraph(doc, "Section 2");

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);

    expect(text).toContain("Section 1");
    expect(text).toContain("TableContent");
    expect(text).toContain("Section 2");

    const p1 = text.indexOf("Section 1");
    const tIdx = text.indexOf("TableContent");
    const p2 = text.indexOf("Section 2");

    expect(p1).toBeLessThan(tIdx);
    expect(tIdx).toBeLessThan(p2);
  });

  it("extracts and edits nested tables correctly", async () => {
    const doc = await createTestDocument();
    const outerTbl = addTable(doc, 1, 1);

    const rows = Array.from(outerTbl.childNodes).filter(
      (n) => (n as Element).tagName === "w:tr",
    ) as Element[];
    const cells = Array.from(rows[0].childNodes).filter(
      (n) => (n as Element).tagName === "w:tc",
    ) as Element[];

    const nestedTbl = addNestedTable(cells[0], 1, 1);
    setCellText(nestedTbl, 0, 0, "InnerSecret");

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);
    expect(text).toContain("InnerSecret");

    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);
    const [applied] = engine.apply_edits([
      {
        type: "modify",
        target_text: "InnerSecret",
        new_text: "OuterSecret",
      } as ModifyText,
    ]);
    expect(applied).toBe(1);

    const finalBuf = await midDoc.save();
    const final_text = await extractTextFromBuffer(finalBuf);
    expect(final_text).toContain("{--InnerSecret--}{++OuterSecret++}");
  });

  it("merged cells do not duplicate content extraction", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "MergedUnique");

    // Simulate python-docx's cell.merge(cell)
    mergeCells(tbl, 0, 0, 1);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);

    const count = (text.match(/MergedUnique/g) || []).length;
    expect(count).toBe(1);

    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);
    const [applied] = engine.apply_edits([
      {
        type: "modify",
        target_text: "MergedUnique",
        new_text: "ChangedUnique",
      } as ModifyText,
    ]);
    expect(applied).toBe(1);
  });

  it("empty row mapping alignment stays synchronized", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 1);
    setCellText(tbl, 0, 0, "RowA");
    setCellText(tbl, 1, 0, ""); // Empty
    setCellText(tbl, 2, 0, "RowB");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    const [applied] = engine.apply_edits([
      { type: "modify", target_text: "RowB", new_text: "RowC" } as ModifyText,
    ]);

    expect(applied).toBe(1);

    const resBuf = await midDoc.save();
    const resText = await extractTextFromBuffer(resBuf);

    expect(resText).toContain("RowA");
    expect(resText).toContain("{--RowB--}{++RowC++}");
  });

  it("inserts table row below", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "A1");
    setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1");
    setCellText(tbl, 1, 1, "B2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "A1 | A2",
        position: "below",
        cells: ["New B1", "New B2"],
      } as InsertTableRow,
    ]);

    expect(stats.edits_applied).toBe(1);

    // Call accept_all_revisions (requires implementation in engine.ts)
    (engine as any).accept_all_revisions();
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).toContain("A1 | A2");
    expect(clean_text).toContain("New B1 | New B2");
    expect(clean_text).toContain("B1 | B2");
  });

  it("deletes table row", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 2);
    setCellText(tbl, 0, 0, "A1");
    setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1");
    setCellText(tbl, 1, 1, "B2");
    setCellText(tbl, 2, 0, "C1");
    setCellText(tbl, 2, 1, "C2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([
      { type: "delete_row", target_text: "B1" } as DeleteTableRow,
    ]);

    expect(stats.edits_applied).toBe(1);

    (engine as any).accept_all_revisions();
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).toContain("A1 | A2");
    expect(clean_text).not.toContain("B1 | B2");
    expect(clean_text).toContain("C1 | C2");
  });

  it("clean view naturally omits deleted row", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "A1");
    setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1");
    setCellText(tbl, 1, 1, "B2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    engine.process_batch([
      { type: "delete_row", target_text: "B1" } as DeleteTableRow,
    ]);

    // Do NOT accept revisions, extract as Clean View directly
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).toContain("A1 | A2");
    expect(clean_text).not.toContain("B1 | B2");
  });
  it("P0 Case 1: writes into an empty value cell via its {#cell:paraId} anchor", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "Date");
    // Leave cell (0,1) empty — mirrors the cloud-service-agreement form row.

    // The empty cell's paragraph needs a w14:paraId for the anchor scheme.
    const rows = Array.from(tbl.childNodes).filter(
      (n) => (n as Element).tagName === "w:tr",
    ) as Element[];
    const cells = Array.from(rows[0].childNodes).filter(
      (n) => (n as Element).tagName === "w:tc",
    ) as Element[];
    const emptyP = cells[1].getElementsByTagName("w:p")[0];
    emptyP.setAttribute("w14:paraId", "DEADBEEF");

    const buf = await doc.save();
    const projected = await extractTextFromBuffer(buf, false);
    // The anchor must be present and addressable.
    expect(projected).toContain("{#cell:DEADBEEF}");

    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);
    // Target the anchor, insert text before it.
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "{#cell:DEADBEEF}",
        new_text: "June 22, 2026",
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    (engine as any).accept_all_revisions();
    const cleanText = await extractTextFromBuffer(await midDoc.save(), true);
    expect(cleanText).toContain("June 22, 2026");
  });

  it('P0 Case 2: anchored-regex miss on "( x )" yields a literal nearest-match hint', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Some intro text. ( x ) Date of last signature.");
    const engine = new RedlineEngine(doc);

    const errors = engine.validate_edits([
      {
        type: "modify",
        regex: true,
        target_text: "^\\( x \\)$",
        new_text: "",
      } as any,
    ]);
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("Target text not found");
    expect(errors[0]).toContain('Did you mean the literal "( x )"');
    expect(errors[0]).toContain("drop the ^/$ anchors");
  });

  it("compiled report includes type for structural table operations", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "Row1 Col1");
    setCellText(tbl, 0, 1, "Row1 Col2");
    setCellText(tbl, 1, 0, "Row2 Col1");
    setCellText(tbl, 1, 1, "Row2 Col2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);

    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "Row1 Col1",
        position: "below",
        cells: ["NewRow Col1", "NewRow Col2"],
      } as InsertTableRow,
      {
        type: "delete_row",
        target_text: "Row2 Col1",
      } as DeleteTableRow,
    ]);

    expect(stats.edits_applied).toBe(2);
    expect(stats.edits).toHaveLength(2);
    expect(stats.edits[0].type).toBe("insert_row");
    expect(stats.edits[1].type).toBe("delete_row");
  });

  // BUG_adeu_accept_all_table_row_loss — Python-engine parity.
  // Accepting a change that empties a table cell must not leave the <w:tc>
  // without a <w:p>: ECMA-376 requires at least one block-level element per
  // cell and Word reports a document with an empty <w:tc/> as corrupt.
  it("accept_all keeps the last paragraph of an emptied cell", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "ALPHA");
    setCellText(tbl, 0, 1, "KEEP");
    addParagraph(doc, "tail paragraph");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);

    // Deleting all of cell 0's text empties it, but the sibling cell keeps
    // content so the row survives — the cell needs an empty paragraph.
    const stats = engine.process_batch([
      { type: "modify", target_text: "ALPHA", new_text: "" } as ModifyText,
    ]);
    expect(stats.edits_applied).toBe(1);

    const tracked = await midDoc.save();
    const acceptDoc = await DocumentObject.load(tracked);
    const acceptEngine = new RedlineEngine(acceptDoc);
    (acceptEngine as any).accept_all_revisions();

    const finalDoc = await DocumentObject.load(await acceptDoc.save());
    const cells = findAllDescendants((finalDoc as any).element, "w:tc");
    expect(cells.length).toBe(2);
    for (const cell of cells) {
      expect(findAllDescendants(cell, "w:p").length).toBeGreaterThanOrEqual(1);
    }

    const clean_text = await extractTextFromBuffer(await acceptDoc.save(), true);
    expect(clean_text).toContain("KEEP");
    expect(clean_text).not.toContain("ALPHA");
  });

  // BUG_adeu_accept_all_table_row_loss — the Python engine stamped a spurious
  // w:trPr/w:del when a replacement covered a cell's whole text, and
  // accept_all_revisions then dropped the row. Node never had the row
  // inference; this pins the behaviour so the two engines stay in step.
  it("accept_all keeps the row when a replacement covers a whole cell", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 1);
    setCellText(tbl, 0, 0, "ALPHA");
    setCellText(tbl, 1, 0, "BETA");
    addParagraph(doc, "tail paragraph");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);
    engine.process_batch([
      { type: "modify", target_text: "ALPHA", new_text: "GAMMA" } as ModifyText,
    ]);

    const tracked = await midDoc.save();
    const trackedDoc = await DocumentObject.load(tracked);
    for (const row of findAllDescendants((trackedDoc as any).element, "w:tr")) {
      const trPr = findChild(row, "w:trPr");
      expect(trPr && findChild(trPr, "w:del")).toBeFalsy();
    }

    const acceptEngine = new RedlineEngine(trackedDoc);
    (acceptEngine as any).accept_all_revisions();
    const finalBuf = await trackedDoc.save();

    const finalDoc = await DocumentObject.load(finalBuf);
    expect(findAllDescendants((finalDoc as any).element, "w:tr").length).toBe(2);

    const clean_text = await extractTextFromBuffer(finalBuf, true);
    expect(clean_text).toContain("GAMMA");
    expect(clean_text).toContain("BETA");
    expect(clean_text).not.toContain("ALPHA");
  });
});
