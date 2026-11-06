import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { generate_structured_edits } from "./diff.js";
import { RedlineEngine } from "./engine.js";

describe("Diff paragraph sub-alignment and self-contained balance", () => {
  it("sub-aligns unequal replace blocks so rejecting revisions restores all original paragraphs", async () => {
    const doc1 = await createTestDocument();
    addParagraph(doc1, "(c) changes to the methodology for compounding of the Adjusted Reference Rate;");
    addParagraph(doc1, "(d) changes to the spread adjustment methodology, including rounding conventions; and");
    addParagraph(
      doc1,
      "(e) any other changes that the Calculation Agent determines are consistent with market practice.",
    );
    const v1 = await doc1.save();

    const doc2 = await createTestDocument();
    addParagraph(doc2, "(c) changes to the methodology for compounding of the applicable fallback rate; and");
    addParagraph(
      doc2,
      '(d) any other changes that the "Co-Calculation Agent" determines are consistent with market practice.',
    );
    const v2 = await doc2.save();

    const d1 = await DocumentObject.load(v1);
    const d2 = await DocumentObject.load(v2);
    const { text: t1, structure: s1 } = _extractTextFromDoc(d1, true, false, false, true) as any;
    const { text: t2, structure: s2 } = _extractTextFromDoc(d2, true, false, false, true) as any;

    const { edits } = generate_structured_edits(t1, s1, t2, s2);
    const engDoc = await DocumentObject.load(v1);
    const eng = new RedlineEngine(engDoc, "Reviewer");
    const [applied, skipped] = eng.apply_edits(edits as any);
    expect(skipped).toBe(0);
    expect(applied).toBeGreaterThan(0);

    const out = await engDoc.save();
    expect(await extractTextFromBuffer(out, true)).toBe(await extractTextFromBuffer(v2, true));

    const rejDoc = await DocumentObject.load(out);
    const engRej = new RedlineEngine(rejDoc);
    engRej.reject_all_revisions();
    const rej = await rejDoc.save();
    expect(await extractTextFromBuffer(rej, true)).toBe(await extractTextFromBuffer(v1, true));
  });
});
