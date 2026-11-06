import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

describe("OOXML schema compliance (CT_RPr order, CT_ParaRPr order, empty rPr pruning, mc:Ignorable repair)", () => {
  it("inserts w:b and w:i before w:color and w:sz in CT_RPr and prunes empty w:rPr", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "");
    const xmlDoc = doc.part._element.ownerDocument!;
    const r = xmlDoc.createElement("w:r");
    const rPr = xmlDoc.createElement("w:rPr");
    const rFonts = xmlDoc.createElement("w:rFonts");
    rFonts.setAttribute("w:ascii", "Times New Roman");
    const color = xmlDoc.createElement("w:color");
    color.setAttribute("w:val", "000000");
    const sz = xmlDoc.createElement("w:sz");
    sz.setAttribute("w:val", "24");
    rPr.appendChild(rFonts);
    rPr.appendChild(color);
    rPr.appendChild(sz);
    r.appendChild(rPr);
    const t = xmlDoc.createElement("w:t");
    t.textContent = "Anchor text";
    r.appendChild(t);
    p.appendChild(r);

    const p2 = addParagraph(doc, "");
    const r2 = xmlDoc.createElement("w:r");
    const rPr2 = xmlDoc.createElement("w:rPr");
    rPr2.appendChild(xmlDoc.createElement("w:b"));
    r2.appendChild(rPr2);
    const t2 = xmlDoc.createElement("w:t");
    t2.textContent = "Bold only";
    r2.appendChild(t2);
    p2.appendChild(r2);

    const origBytes = await doc.save();
    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Counsel");
    const [applied, skipped] = eng.apply_edits([
      {
        type: "modify",
        target_text: "Anchor text",
        new_text: "**Bold** and _italic_ replacement",
      },
      {
        type: "modify",
        target_text: "**Bold only**",
        new_text: "Plain only",
      },
    ]);
    expect(applied).toBe(2);
    expect(skipped).toBe(0);

    // Check CT_RPr child order on every <w:rPr>
    for (const rprEl of findAllDescendants(engDoc.part._element, "w:rPr")) {
      const childTags = Array.from(rprEl.childNodes)
        .filter((n: any) => n.nodeType === 1)
        .map((n: any) => n.tagName);
      expect(childTags.length).toBeGreaterThan(0);
      const colorIdx = childTags.indexOf("w:color");
      const szIdx = childTags.indexOf("w:sz");
      const bIdx = childTags.indexOf("w:b");
      const iIdx = childTags.indexOf("w:i");
      if (bIdx !== -1 && colorIdx !== -1) {
        expect(bIdx).toBeLessThan(colorIdx);
      }
      if (bIdx !== -1 && szIdx !== -1) {
        expect(bIdx).toBeLessThan(szIdx);
      }
      if (iIdx !== -1 && colorIdx !== -1) {
        expect(iIdx).toBeLessThan(colorIdx);
      }
      if (iIdx !== -1 && szIdx !== -1) {
        expect(iIdx).toBeLessThan(szIdx);
      }
    }
  });

  it("repairs undeclared mc:Ignorable prefixes on save", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Clause text.");
    doc.part._element.setAttribute("mc:Ignorable", "w14 wp14");
    doc.part._element.removeAttribute("xmlns:w14");
    doc.part._element.removeAttribute("xmlns:wp14");

    const savedBytes = await doc.save();
    const reloaded = await DocumentObject.load(savedBytes);
    const xmlStr = reloaded.part.blob;
    expect(xmlStr).toContain('xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"');
    expect(xmlStr).toContain(
      'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"',
    );
  });
});
