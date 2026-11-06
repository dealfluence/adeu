import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

describe("Manual / inline-formatting markup mode", () => {
  it("renders deletions as strikethrough and insertions as bold underlined #C00000 runs with native comments", async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      "The Notes will mature on December 15, 2030 and bear interest at 6.50% per annum.",
    );
    const origBytes = await doc.save();

    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Counsel", { markup_mode: "manual" });
    const [applied, skipped] = eng.apply_edits([
      {
        type: "modify",
        target_text: "December 15, 2030",
        new_text: "June 15, 2032",
        comment: "Extended maturity date per term sheet.",
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);

    const savedBytes = await engDoc.save();
    const reloaded = await DocumentObject.load(savedBytes);
    const root = reloaded.part._element;

    expect(findAllDescendants(root, "w:ins").length).toBe(0);
    expect(findAllDescendants(root, "w:del").length).toBe(0);
    expect(findAllDescendants(root, "w:delText").length).toBe(0);

    const runs = findAllDescendants(root, "w:r");
    const struckTexts: string[] = [];
    const insertedTexts: string[] = [];
    for (const r of runs) {
      const tEl = findChild(r, "w:t");
      if (!tEl || !tEl.textContent) continue;
      const rPr = findChild(r, "w:rPr");
      if (rPr && findChild(rPr, "w:strike")) {
        struckTexts.push(tEl.textContent);
      }
      const colorEl = rPr ? findChild(rPr, "w:color") : null;
      if (
        rPr &&
        findChild(rPr, "w:b") &&
        findChild(rPr, "w:u") &&
        colorEl &&
        colorEl.getAttribute("w:val") === "C00000"
      ) {
        insertedTexts.push(tEl.textContent);
      }
    }

    expect(struckTexts.join("")).toBe("December 15, 2030");
    expect(insertedTexts.join("")).toBe("June 15, 2032");
    expect(findAllDescendants(root, "w:commentRangeStart").length).toBe(1);
    expect(findAllDescendants(root, "w:commentRangeEnd").length).toBe(1);
  });
});
