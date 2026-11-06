import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { findAllDescendants } from "./docx/dom.js";

describe("Fuzzy whitespace context normalization", () => {
  it("does not split a single-line paragraph when caller repeats newlines in unchanged context", async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      "Atlas Securities LLC 610 Lexington Avenue, 28th Floor New York, NY 10022",
    );
    const origBytes = await doc.save();

    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Counsel");
    const [applied, skipped] = eng.apply_edits([
      {
        type: "modify",
        target_text: "Atlas Securities LLC\n\n610 Lexington Avenue, 28th Floor",
        new_text: "Atlas Securities LLC\n\n600 Lexington Avenue, 28th Floor",
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);

    const pNodes = findAllDescendants(engDoc.part._element, "w:p");
    expect(pNodes.length).toBe(1);

    const outBytes = await engDoc.save();
    const cleanTxt = (
      await extractTextFromBuffer(outBytes, true, undefined, false)
    ).trim();
    expect(cleanTxt).toBe(
      "Atlas Securities LLC 600 Lexington Avenue, 28th Floor New York, NY 10022",
    );
  });
});
