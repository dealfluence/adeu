import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph, attachHeaderFooter } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";

// Twin of python/tests/test_header_block_guard.py. final-2's indenture runs anchored an executive summary (heading +
// table) on the document title, which was the page header's text: it landed in word/header1.xml on every page.

const TITLE = "Pinnacle Health Systems, Inc. Indenture";
const para = (t: string) => `<w:p><w:r><w:t xml:space="preserve">${t}</w:t></w:r></w:p>`;

async function doc(): Promise<Buffer> {
  const d = await createTestDocument();
  addParagraph(d, "ARTICLE 1 DEFINITIONS");
  addParagraph(d, "Consolidated EBITDA means net income plus addbacks.");
  attachHeaderFooter(d, "header", para(TITLE));
  attachHeaderFooter(d, "footer", para("Confidential draft"));
  return d.save();
}

async function batch(changes: any[]) {
  const eng = new RedlineEngine(await DocumentObject.load(await doc()), "Purchaser Counsel");
  return eng.process_batch(changes) as any;
}

async function refusal(changes: any[]): Promise<string> {
  try {
    await batch(changes);
  } catch (e) {
    expect(e).toBeInstanceOf(BatchValidationError);
    return (e as BatchValidationError).errors.join("\n");
  }
  throw new Error("batch was not refused");
}

describe("header/footer block guard and part disclosure", () => {
  it.each([
    `# Executive Summary\n\n${TITLE}`,
    `| # | Section | Position |\n|---|---|---|\n| 1 | Art. 1 | Cap at 25% |\n\n${TITLE}`,
  ])("refuses a heading or table into the page header and points at the first body paragraph (%#)", async (new_text) => {
    const msg = await refusal([{ type: "modify", target_text: TITLE, new_text }]);
    expect(msg).toContain("page header");
    expect(msg).toContain("header1.xml");
    expect(msg).toContain("ARTICLE 1 DEFINITIONS");
  });

  it("says how to target a first body paragraph that recurs later", async () => {
    // The indenture's first body line ("## **INDENTURE**") recurs; a hint that walks into the ambiguity guard is no
    // hint at all.
    const d = await createTestDocument();
    addParagraph(d, "INDENTURE");
    addParagraph(d, "This INDENTURE is dated as of the Issue Date.");
    addParagraph(d, "INDENTURE");
    attachHeaderFooter(d, "header", para(TITLE));
    const eng = new RedlineEngine(await DocumentObject.load(await d.save()), "Purchaser Counsel");
    let msg = "";
    try {
      eng.process_batch([{ type: "modify", target_text: TITLE, new_text: `# Executive Summary\n\n${TITLE}` }]);
    } catch (e) {
      msg = (e as BatchValidationError).errors.join("\n");
    }
    expect(msg).toContain(`'INDENTURE' (it appears `);
    expect(msg).toContain(`times: pass match_mode="first")`);
  });

  it("refuses a table into the footer and points at the last body paragraph", async () => {
    const msg = await refusal([
      { type: "modify", target_text: "Confidential draft", new_text: "Confidential draft\n\n| a | b |\n|---|---|" },
    ]);
    expect(msg).toContain("page footer");
    expect(msg).toContain("Consolidated EBITDA means net income plus addbacks.");
  });

  it("applies a wording change in the header and names the part in the receipt", async () => {
    const stats = await batch([{ type: "modify", target_text: "Indenture", new_text: "Indenture (Purchaser Draft)" }]);
    expect([stats.edits_applied, stats.edits_skipped]).toEqual([1, 0]);
    expect(stats.edits[0].part).toBe("header (word/header1.xml)");
  });

  it("leaves the part label empty for a body edit", async () => {
    const stats = await batch([{ type: "modify", target_text: "net income", new_text: "consolidated net income" }]);
    expect(stats.edits[0].part).toBe("");
  });
});
