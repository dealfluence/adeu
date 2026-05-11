import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph, addTable, setCellText, mergeCells, addNestedTable } from './test-utils.js';
import { DocumentObject } from './docx/bridge.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine } from './engine.js';
import { ModifyText, InsertTableRow, DeleteTableRow, RejectChange } from './models.js';

describe('Table Interop & Engine (Node.js Port)', () => {
  it('interleaved tables and text remain ordered', async () => {
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

  it('extracts and edits nested tables correctly', async () => {
    const doc = await createTestDocument();
    const outerTbl = addTable(doc, 1, 1);
    
    const rows = Array.from(outerTbl.childNodes).filter(n => (n as Element).tagName === 'w:tr') as Element[];
    const cells = Array.from(rows[0].childNodes).filter(n => (n as Element).tagName === 'w:tc') as Element[];
    
    const nestedTbl = addNestedTable(cells[0], 1, 1);
    setCellText(nestedTbl, 0, 0, "InnerSecret");

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);
    expect(text).toContain("InnerSecret");

    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);
    const [applied] = engine.apply_edits([{ type: 'modify', target_text: "InnerSecret", new_text: "OuterSecret" } as ModifyText]);
    expect(applied).toBe(1);

    const finalBuf = await midDoc.save();
    const final_text = await extractTextFromBuffer(finalBuf);
    expect(final_text).toContain("{--InnerSecret--}{++OuterSecret++}");
  });

  it('merged cells do not duplicate content extraction', async () => {
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
    const [applied] = engine.apply_edits([{ type: 'modify', target_text: "MergedUnique", new_text: "ChangedUnique" } as ModifyText]);
    expect(applied).toBe(1);
  });

  it('empty row mapping alignment stays synchronized', async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 1);
    setCellText(tbl, 0, 0, "RowA");
    setCellText(tbl, 1, 0, ""); // Empty
    setCellText(tbl, 2, 0, "RowB");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);
    
    const engine = new RedlineEngine(midDoc);
    const [applied] = engine.apply_edits([{ type: 'modify', target_text: "RowB", new_text: "RowC" } as ModifyText]);
    
    expect(applied).toBe(1);

    const resBuf = await midDoc.save();
    const resText = await extractTextFromBuffer(resBuf);

    expect(resText).toContain("RowA");
    expect(resText).toContain("{--RowB--}{++RowC++}");
  });

  it('inserts table row below', async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "A1"); setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1"); setCellText(tbl, 1, 1, "B2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([
      { type: 'insert_row', target_text: "A1 | A2", position: "below", cells: ["New B1", "New B2"] } as InsertTableRow
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

  it('deletes table row', async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 3, 2);
    setCellText(tbl, 0, 0, "A1"); setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1"); setCellText(tbl, 1, 1, "B2");
    setCellText(tbl, 2, 0, "C1"); setCellText(tbl, 2, 1, "C2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    const stats = engine.process_batch([{ type: 'delete_row', target_text: "B1" } as DeleteTableRow]);

    expect(stats.edits_applied).toBe(1);

    (engine as any).accept_all_revisions();
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).toContain("A1 | A2");
    expect(clean_text).not.toContain("B1 | B2");
    expect(clean_text).toContain("C1 | C2");
  });

  it('clean view naturally omits deleted row', async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "A1"); setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1"); setCellText(tbl, 1, 1, "B2");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);

    const engine = new RedlineEngine(midDoc);
    engine.process_batch([{ type: 'delete_row', target_text: "B1" } as DeleteTableRow]);

    // Do NOT accept revisions, extract as Clean View directly
    const finalBuf = await midDoc.save();
    const clean_text = await extractTextFromBuffer(finalBuf, true);

    expect(clean_text).toContain("A1 | A2");
    expect(clean_text).not.toContain("B1 | B2");
  });
});