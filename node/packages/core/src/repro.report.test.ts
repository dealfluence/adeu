import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph, addTable, setCellText } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";
import { DocumentObject } from "./docx/bridge.js";

describe("Webinar Report Bug Reproductions", () => {
  it("Bug 1: Diff Engine Fragility (Token-Splitting Punctuation)", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;
    const p = xmlDoc.createElement("w:p");

    // Replicate MS Word fragmented runs for token-splitting punctuation: [Company Name]
    // with style/formatting run fragmentation where only some runs are bold.
    // This generates: **[**Company Name**]**
    // The target "[Company Name]" will fail to be matched because brackets are not treated
    // as fuzzy boundaries, so the fuzzy regex expects "[Company" directly without the intermediate "**".
    const r1 = xmlDoc.createElement("w:r");
    const rPr1 = xmlDoc.createElement("w:rPr");
    rPr1.appendChild(xmlDoc.createElement("w:b"));
    r1.appendChild(rPr1);
    const t1 = xmlDoc.createElement("w:t");
    t1.textContent = "[";
    r1.appendChild(t1);
    p.appendChild(r1);

    const r2 = xmlDoc.createElement("w:r");
    // Run 2 is NOT bold
    const t2 = xmlDoc.createElement("w:t");
    t2.textContent = "Company Name";
    r2.appendChild(t2);
    p.appendChild(r2);

    const r3 = xmlDoc.createElement("w:r");
    const rPr3 = xmlDoc.createElement("w:rPr");
    rPr3.appendChild(xmlDoc.createElement("w:b"));
    r3.appendChild(rPr3);
    const t3 = xmlDoc.createElement("w:t");
    t3.textContent = "]";
    r3.appendChild(t3);
    p.appendChild(r3);

    doc.element.appendChild(p);

    const engine = new RedlineEngine(doc);

    // Under correct engine behavior, targeting "[Company Name]" should succeed.
    // However, on the unpatched codebase, it throws a BatchValidationError (Target text not found).
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "[Company Name]",
        new_text: "Google LLC",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);
  });

  it("Bug 2: editing across a foreign author's insertion boundary is refused (provenance-safe)", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    const p = addParagraph(doc, "The party shall provide ");
    const ins = xmlDoc.createElement("w:ins");
    ins.setAttribute("w:id", "123");
    ins.setAttribute("w:author", "Supplier's Counsel");
    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "written notice";
    r.appendChild(t);
    ins.appendChild(r);
    p.appendChild(ins);

    const suffixRun = xmlDoc.createElement("w:r");
    const suffixText = xmlDoc.createElement("w:t");
    suffixText.textContent = " within 30 days.";
    suffixRun.appendChild(suffixText);
    p.appendChild(suffixRun);

    const engine = new RedlineEngine(doc, "Reviewer");

    // The target STRADDLES Supplier's Counsel's pending <w:ins> boundary
    // (foreign-inserted "written notice" + plain " within 30 days."). Canonical
    // behavior is to REFUSE rather than silently lift the foreign insertion into
    // committed text (which would launder another author's pending proposal and
    // erase their provenance). The agent must accept that change first, or scope
    // the edit within / outside the insertion. (Matches the Python engine.)
    const edit = {
      type: "modify",
      target_text: "written notice within 30 days.",
      new_text: "notice within 15 business days.",
    };
    expect(() => engine.process_batch([edit as any], false)).toThrow(
      /active insertion from another author/,
    );
  });

  it("Bug 3: Table Layout Ambiguity and Markdown Loss of Fidelity", async () => {
    const doc = await createTestDocument();
    
    // Create a table with 2 rows, 4 columns
    const tbl = addTable(doc, 2, 4);
    
    // Set cell text to match identical structure: Date | | | 
    setCellText(tbl, 0, 0, "Date");
    setCellText(tbl, 0, 1, "");
    setCellText(tbl, 0, 2, "");
    setCellText(tbl, 0, 3, "");
    
    setCellText(tbl, 1, 0, "Date");
    setCellText(tbl, 1, 1, "");
    setCellText(tbl, 1, 2, "");
    setCellText(tbl, 1, 3, "");

    const buf = await doc.save();
    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc);

    // Target one of the empty rows to apply a date.
    const edit = {
      type: "modify",
      target_text: "Date |  |  | ",
      new_text: "Date | June 29, 2026 |  | ",
    };

    // Ideally, the system should allow distinct targeting of cells (e.g. preserving grid coordinates).
    // But due to the coordinate-flattening design flaw, it throws "Ambiguous match".
    const res = engine.process_batch([edit as any]);
    console.log("BUG 3 PROCESS_BATCH RESULT:", JSON.stringify(res, null, 2));
    expect(res.edits_applied).toBe(1);
  });
});
