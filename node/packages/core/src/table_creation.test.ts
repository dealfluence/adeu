import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

describe("Markdown pipe table creation in ModifyText.new_text", () => {
  it("creates a schema-valid w:tbl when new_text contains a Markdown pipe table", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "INDENTURE");
    addParagraph(doc, "Section 1.01. Definitions.");
    const origBytes = await doc.save();

    const tableMd =
      "## Executive Summary\n\n" +
      "| Issue / Section | Draft Provision | Proposed Position | Priority |\n" +
      "|---|---|---|---|\n" +
      "| Section 4.01 | 30-day grace | **15-day grace** | High |\n" +
      "| Section 4.07 | 2.0x ratio | **3.5x ratio** | Medium |\n\n" +
      "INDENTURE";

    const engDoc = await DocumentObject.load(origBytes);
    const eng = new RedlineEngine(engDoc, "Counsel");
    const [applied, skipped] = eng.apply_edits([
      {
        type: "modify",
        target_text: "INDENTURE",
        new_text: tableMd,
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);

    const outBytes = await engDoc.save();
    const reloaded = await DocumentObject.load(outBytes);
    const tables = findAllDescendants(reloaded.part._element, "w:tbl");
    expect(tables.length).toBe(1);

    const rows = findAllDescendants(tables[0], "w:tr");
    expect(rows.length).toBe(3);
    for (const r of rows) {
      expect(findAllDescendants(r, "w:tc").length).toBe(4);
    }

    for (const shd of findAllDescendants(tables[0], "w:shd")) {
      expect(shd.getAttribute("w:val")).toBe("clear");
    }

    const cleanTxt = await extractTextFromBuffer(outBytes, true, undefined, false);
    expect(cleanTxt).toContain(
      "**Issue / Section** | **Draft Provision** | **Proposed Position** | **Priority**",
    );
    expect(cleanTxt).toContain("Section 4.01 | 30-day grace | **15-day grace** | High");
  });
});
