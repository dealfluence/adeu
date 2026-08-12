import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

// ---------------------------------------------------------------------------
// Inserting a whole new SECTION in front of an existing heading, expressed the
// way an agent naturally writes it:
//
//     target_text: "# SCOPE"
//     new_text:    "# NEW SECTION\n\nBody of the new section.\n\n# SCOPE"
//
// The demo run (Asteria v Northstar, 2026-08-12) turned exactly this into
//
//     # "HIGHLY CONFIDENTIAL - ATTORNEYS' EYES ONLY" MATERIALSCOPE
//     # "Highly Confidential..." material shall include ...
//     #
//
// i.e. the replacement's first paragraph was welded onto the heading's own
// text, the body paragraph was promoted to Heading 1, and an empty heading was
// left behind. The Python twin gets the same input right, so these are Node
// parity regressions, not a shared design limit.
// ---------------------------------------------------------------------------

/** Appends a paragraph carrying an explicit `w:pStyle`. */
function addStyledParagraph(
  doc: DocumentObject,
  text: string,
  styleId: string,
): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  const pPr = xmlDoc.createElement("w:pPr");
  const pStyle = xmlDoc.createElement("w:pStyle");
  pStyle.setAttribute("w:val", styleId);
  pPr.appendChild(pStyle);
  p.appendChild(pPr);
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.textContent = text;
  if (/\s/.test(text)) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  p.appendChild(r);
  doc.element.appendChild(p);
  return p;
}

/**
 * Declares a CUSTOM paragraph style whose heading-ness lives in styles.xml as
 * `<w:outlineLvl>` — the shape real legal templates use ("LegalNum2L1" in the
 * demo document). Word treats it as a heading; a style-NAME test does not.
 */
function declareOutlineStyle(
  doc: DocumentObject,
  styleId: string,
  outlineLvl: number,
): void {
  const part = doc.pkg.getPartByPath("word/styles.xml")!;
  const xmlDoc = part._element.ownerDocument!;
  const style = xmlDoc.createElement("w:style");
  style.setAttribute("w:type", "paragraph");
  style.setAttribute("w:customStyle", "1");
  style.setAttribute("w:styleId", styleId);
  const name = xmlDoc.createElement("w:name");
  name.setAttribute("w:val", styleId.replace(/(\d)/g, "_$1"));
  style.appendChild(name);
  const basedOn = xmlDoc.createElement("w:basedOn");
  basedOn.setAttribute("w:val", "Normal");
  style.appendChild(basedOn);
  const pPr = xmlDoc.createElement("w:pPr");
  const oLvl = xmlDoc.createElement("w:outlineLvl");
  oLvl.setAttribute("w:val", String(outlineLvl));
  pPr.appendChild(oLvl);
  style.appendChild(pPr);
  part._element.appendChild(style);
}

/** The `w:pStyle` value of the paragraph whose text is exactly `text`. */
function styleOfParagraphWithText(
  doc: DocumentObject,
  text: string,
): string | null {
  for (const p of findAllDescendants(doc.element, "w:p")) {
    const runs = findAllDescendants(p, "w:t")
      .map((t) => t.textContent || "")
      .join("");
    if (runs === text) {
      const pPr = findChild(p, "w:pPr");
      const pStyle = pPr ? findChild(pPr, "w:pStyle") : null;
      return pStyle ? pStyle.getAttribute("w:val") : null;
    }
  }
  return null;
}

const NEW_SECTION =
  "# NEW SECTION\n\nBody of the new section.\n\n# SCOPE";

describe("inserting a section in front of an existing heading", () => {
  it("does not weld the replacement onto the heading's own text (Heading1)", async () => {
    const doc = await createTestDocument();
    addStyledParagraph(doc, "SCOPE", "Heading1");
    addParagraph(doc, "The protections conferred by this agreement are broad.");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    const stats = engine.process_batch([
      { type: "modify", target_text: "# SCOPE", new_text: NEW_SECTION } as any,
    ]);
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);

    // The heading text must survive as its own heading, not as a suffix of the
    // inserted one ("# NEW SECTIONSCOPE") and not as a stray empty "# ".
    expect(finalText).toBe(
      "# NEW SECTION\n\nBody of the new section.\n\n# SCOPE\n\n" +
        "The protections conferred by this agreement are broad.",
    );
  });

  it("does not promote the inserted body paragraph to a heading (Heading1)", async () => {
    const doc = await createTestDocument();
    addStyledParagraph(doc, "SCOPE", "Heading1");
    addParagraph(doc, "Tail.");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    engine.process_batch([
      { type: "modify", target_text: "# SCOPE", new_text: NEW_SECTION } as any,
    ]);
    engine.accept_all_revisions(true);

    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    expect(finalText).not.toContain("# Body of the new section.");
    expect(styleOfParagraphWithText(workDoc, "Body of the new section.")).not.toBe(
      "Heading1",
    );
  });

  it("keeps a CUSTOM outline-level heading intact and its body plain", async () => {
    // The demo document's headings are "LegalNum2L1": a custom style whose
    // outline level is declared in styles.xml, so the style NAME says nothing.
    const doc = await createTestDocument();
    declareOutlineStyle(doc, "LegalNum2L1", 0);
    addStyledParagraph(doc, "SCOPE", "LegalNum2L1");
    addParagraph(doc, "Tail.");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    const stats = engine.process_batch([
      { type: "modify", target_text: "# SCOPE", new_text: NEW_SECTION } as any,
    ]);
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    expect(finalText).toBe(
      "# NEW SECTION\n\nBody of the new section.\n\n# SCOPE\n\nTail.",
    );

    // The inserted body must not inherit the heading style (which in a real
    // template also carries the section's automatic numbering).
    expect(
      styleOfParagraphWithText(workDoc, "Body of the new section."),
    ).not.toBe("LegalNum2L1");
  });

  it("leaves the existing heading paragraph itself untouched", async () => {
    const doc = await createTestDocument();
    declareOutlineStyle(doc, "LegalNum2L1", 0);
    addStyledParagraph(doc, "SCOPE", "LegalNum2L1");
    addParagraph(doc, "Tail.");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    engine.process_batch([
      { type: "modify", target_text: "# SCOPE", new_text: NEW_SECTION } as any,
    ]);
    engine.accept_all_revisions(true);

    // Inserting a section BEFORE a heading must not re-home the heading's text
    // into a freshly minted paragraph: that silently drops the template style
    // (and with it the legal auto-numbering) the heading was carrying.
    expect(styleOfParagraphWithText(workDoc, "SCOPE")).toBe("LegalNum2L1");
  });

  it("still cancels matching heading hashes on a single-paragraph rewrite", async () => {
    // The guard added for the cases above must not disable the hash-cancelling
    // that keeps a plain heading rewrite out of the redline (QA report v3 TC4).
    const doc = await createTestDocument();
    addStyledParagraph(doc, "3. Pending Review", "Heading1");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "# 3. Pending Review",
        new_text: "# 3. Final Review",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);
    expect(res.edits[0].critic_markup).not.toContain("{--#");
    expect(res.edits[0].critic_markup).not.toContain("{++#");
  });

  it("relocates the host paragraph's text when inserting at its start", async () => {
    // The suffix-relocation contract of repro_qa_report_v8's "0." case, but for
    // a paragraph that is NOT the first in the document: the insertion point is
    // still a paragraph START, so the host paragraph's own text belongs in the
    // LAST inserted paragraph.
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph.");
    addParagraph(doc, "00.");
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "Reviewer");
    const stats = engine.process_batch([
      { type: "modify", target_text: "00.", new_text: "0.\n\n0 00." } as any,
    ]);
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    expect(finalText).toBe("First paragraph.\n\n0.\n\n0 00.");
  });
});
