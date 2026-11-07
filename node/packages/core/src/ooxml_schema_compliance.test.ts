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

  it("keeps w:del before w:sz in a deleted paragraph's mark properties (CT_ParaRPr order)", async () => {
    // Python twin: test_unbold_replacement_prunes_empty_rpr_and_para_rpr_maintains_canonical_order.
    const doc = await createTestDocument();
    addParagraph(doc, "Anchor paragraph.");
    const p2 = addParagraph(doc, "Paragraph to delete.");
    const xmlDoc = doc.part._element.ownerDocument!;
    const pPr = xmlDoc.createElement("w:pPr");
    const markRPr = xmlDoc.createElement("w:rPr");
    const sz = xmlDoc.createElement("w:sz");
    sz.setAttribute("w:val", "24");
    markRPr.appendChild(sz);
    pPr.appendChild(markRPr);
    p2.insertBefore(pPr, p2.firstChild);
    addParagraph(doc, "Surviving paragraph.");

    const engDoc = await DocumentObject.load(await doc.save());
    const eng = new RedlineEngine(engDoc, "Counsel");
    const [applied, skipped] = eng.apply_edits([
      { type: "modify", target_text: "Paragraph to delete.\n\n", new_text: "" },
    ]);
    expect([applied, skipped]).toEqual([1, 0]);

    const target = findAllDescendants(engDoc.part._element, "w:p")[1];
    const tags = Array.from(findChild(findChild(target, "w:pPr")!, "w:rPr")!.childNodes)
      .filter((n) => n.nodeType === 1)
      .map((n) => (n as Element).tagName);
    expect(tags.indexOf("w:del")).toBeGreaterThanOrEqual(0);
    expect(tags.indexOf("w:del")).toBeLessThan(tags.indexOf("w:sz"));
  });

  const FOUR = ["Alpha clause one.", "Bravo clause two.", "Charlie clause three.", "Delta clause four."];
  const MARK_EDITS: Record<string, { target_text: string; new_text: string }> = {
    "delete whole paragraph": { target_text: "Bravo clause two.\n\n", new_text: "" },
    "split paragraph": { target_text: "Alpha clause one.", new_text: "Alpha clause\n\none." },
    "insert paragraph": { target_text: "Charlie clause three.", new_text: "Charlie clause three.\n\nNew paragraph." },
    "merge paragraphs": {
      target_text: "Charlie clause three.\n\nDelta clause four.",
      new_text: "Charlie clause three. Delta clause four.",
    },
  };
  const EMPTY_CONTAINER = /<w:pPr\/>|<w:pPr><\/w:pPr>|<w:rPr\/>|<w:rPr><\/w:rPr>/g;

  for (const resolve of ["accept", "reject"] as const) {
    for (const [name, edit] of Object.entries(MARK_EDITS)) {
      it(`leaves no empty property containers: ${name}, then ${resolve} (all and by id)`, async () => {
        // Python twin: test_resolving_paragraph_mark_revisions_leaves_no_empty_property_containers.
        const doc = await createTestDocument();
        for (const t of FOUR) addParagraph(doc, t);
        const d = await DocumentObject.load(await doc.save());
        new RedlineEngine(d, "Counsel").process_batch([{ type: "modify", ...edit }]);
        const edited = await d.save();
        expect(d.part.blob.match(EMPTY_CONTAINER) ?? [], `${name} after edit`).toEqual([]);

        const all = await DocumentObject.load(edited);
        const e1 = new RedlineEngine(all);
        if (resolve === "accept") e1.accept_all_revisions();
        else e1.reject_all_revisions();
        const allXml = (await DocumentObject.load(await all.save())).part.blob;
        expect(allXml.match(EMPTY_CONTAINER) ?? [], `${name} / ${resolve} all`).toEqual([]);

        const byId = await DocumentObject.load(edited);
        const ids = Array.from(
          new Set(
            ["w:ins", "w:del"].flatMap((t) =>
              findAllDescendants(byId.part._element, t).map((el) => el.getAttribute("w:id")!),
            ),
          ),
        );
        new RedlineEngine(byId).process_batch(ids.map((id) => ({ type: resolve, target_id: `Chg:${id}` })) as any);
        const idXml = (await DocumentObject.load(await byId.save())).part.blob;
        expect(idXml.match(EMPTY_CONTAINER) ?? [], `${name} / ${resolve} by id`).toEqual([]);
      });
    }
  }

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
