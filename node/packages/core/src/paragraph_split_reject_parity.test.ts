import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";

async function extractOoxmlOriginalParagraphs(docxBytes: Uint8Array): Promise<string[]> {
  const doc = await DocumentObject.load(docxBytes);
  const dom = doc.part._element;
  const out: string[] = [];
  let carry = "";

  const pNodes = Array.from(dom.getElementsByTagName("w:p"));
  for (const p of pNodes) {
    const chunks: string[] = [];
    const walk = (node: any) => {
      if (node.nodeType !== 1) return;
      const tag = node.tagName || "";
      if (tag === "w:ins" || tag === "w:pPr") return;
      if (tag === "w:t" || tag === "w:delText") {
        if (node.textContent) chunks.push(node.textContent);
        return;
      }
      for (const child of Array.from(node.childNodes)) {
        walk(child);
      }
    };
    walk(p);

    const pPr = Array.from(p.childNodes).find((c: any) => c.nodeType === 1 && c.tagName === "w:pPr") as any;
    const rPr = pPr
      ? (Array.from(pPr.childNodes).find((c: any) => c.nodeType === 1 && c.tagName === "w:rPr") as any)
      : null;
    const pilcrowIns = rPr
      ? Array.from(rPr.childNodes).some((c: any) => c.nodeType === 1 && c.tagName === "w:ins")
      : false;

    carry += chunks.join("");
    if (!pilcrowIns) {
      if (carry.trim()) out.push(carry.trim());
      carry = "";
    }
  }
  if (carry.trim()) out.push(carry.trim());
  return out;
}

describe("Paragraph split pilcrow w:ins placement and rejection parity", () => {
  it("preserves original paragraph structure when rejecting a prefix multiline split", async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"), ' +
        "acting in its sole capacity as Calculation Agent, " +
        "as designated under Section 6.8 of the Schedule to the Agreement.",
    );
    addParagraph(doc, "The Calculation Agent shall determine all rates and amounts.");
    const origBytes = await doc.save();

    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Reviewer");
    const [applied, skipped] = eng.apply_edits([
      {
        type: "modify",
        target_text:
          'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"), ' +
          "acting in its sole capacity as Calculation Agent,",
        new_text:
          "_Drafting note: See rider._\n\n" +
          "**2.1 Calculation Agent [Alternative A]**\n\n" +
          'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"),',
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);
    const redlinedBytes = await engDoc.save();

    // 1. Standard OOXML Original-view reader must see the exact 2 original paragraphs
    expect(await extractOoxmlOriginalParagraphs(redlinedBytes)).toEqual(
      await extractOoxmlOriginalParagraphs(origBytes),
    );

    // 2. reject_all_revisions() must restore the exact original clean text
    const rejDoc = await DocumentObject.load(redlinedBytes);
    const engRej = new RedlineEngine(rejDoc);
    engRej.reject_all_revisions();
    const rejBytes = await rejDoc.save();
    expect(await extractTextFromBuffer(rejBytes, true)).toBe(
      await extractTextFromBuffer(origBytes, true),
    );

    // 3. Targeted RejectChange on Chg:1 must also restore the exact original clean text
    const singleRejDoc = await DocumentObject.load(redlinedBytes);
    const engSingleRej = new RedlineEngine(singleRejDoc);
    engSingleRej.apply_review_actions([{ type: "reject", target_id: "Chg:1" }]);
    const singleRejBytes = await singleRejDoc.save();
    expect(await extractTextFromBuffer(singleRejBytes, true)).toBe(
      await extractTextFromBuffer(origBytes, true),
    );
  });
});
