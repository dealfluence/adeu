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

describe("Whole-paragraph deletion non-destructive container preservation", () => {
  it("preserves individual paragraph containers when consecutive paragraphs are deleted", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "5.1 Close-out Amount");
    addParagraph(doc, "Upon the occurrence of an Early Termination Date, the amount shall be determined.");
    addParagraph(doc, "The Determining Party may consider the following sources:");
    addParagraph(doc, "Surviving paragraph at the end of the section.");
    const origBytes = await doc.save();

    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Reviewer");
    const [applied, skipped] = eng.apply_edits([
      { type: "modify", target_text: "5.1 Close-out Amount\n\n", new_text: "" },
      {
        type: "modify",
        target_text: "Upon the occurrence of an Early Termination Date, the amount shall be determined.\n\n",
        new_text: "",
      },
      {
        type: "modify",
        target_text: "The Determining Party may consider the following sources:\n\n",
        new_text: "",
      },
    ]);
    expect(applied).toBe(3);
    expect(skipped).toBe(0);

    const redlinedBytes = await engDoc.save();

    // 1. Clean view shows only the surviving paragraph
    expect((await extractTextFromBuffer(redlinedBytes, true, undefined, false)).trim()).toBe(
      "Surviving paragraph at the end of the section.",
    );

    // 2. Original-view reader sees all 4 separate paragraphs (not concatenated into 1)
    expect(await extractOoxmlOriginalParagraphs(redlinedBytes)).toEqual(
      await extractOoxmlOriginalParagraphs(origBytes),
    );

    // 3. reject_all_revisions() restores all 4 separate paragraphs
    const rejDoc = await DocumentObject.load(redlinedBytes);
    const engRej = new RedlineEngine(rejDoc);
    engRej.reject_all_revisions();
    const rejBytes = await rejDoc.save();
    expect(await extractTextFromBuffer(rejBytes, true)).toBe(
      await extractTextFromBuffer(origBytes, true),
    );

    // 4. accept_all_revisions() leaves only the surviving paragraph
    const accDoc = await DocumentObject.load(redlinedBytes);
    const engAcc = new RedlineEngine(accDoc);
    engAcc.accept_all_revisions();
    const accBytes = await accDoc.save();
    expect((await extractTextFromBuffer(accBytes, true, undefined, false)).trim()).toBe(
      "Surviving paragraph at the end of the section.",
    );
  });
});
