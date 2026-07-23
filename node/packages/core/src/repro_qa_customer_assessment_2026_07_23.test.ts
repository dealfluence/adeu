/**
 * Failing repro tests for the customer-facing QA assessment (2026-07-23,
 * "Adeu Document Redlining — QA Assessment for Customers", v1.29.0) — Node
 * side. Python mirrors live in
 * python/tests/test_repro_qa_customer_assessment_2026_07_23.py.
 *
 * Findings covered here:
 *
 * - C3 (the headline Node finding): "Asking the tool to attach a comment to
 *   a sentence WITHOUT supplying replacement text causes the Node server to
 *   delete the sentence (as a tracked change) and hang the comment on the
 *   deletion." Reproduces. The MCP zod schema leaves new_text optional, so
 *   `{type:"modify", target_text, comment}` reaches the core engine with
 *   new_text === undefined; engine.ts coalesces it to "" (`edit.new_text ||
 *   ""`), the COMMENT_ONLY short-circuit is skipped, and
 *   _single_commented_sub_edit classifies whole-sentence-vs-empty as a
 *   DELETION carrying the comment. An ABSENT new_text with a comment is an
 *   annotation — the lossless interpretation is COMMENT_ONLY (new_text ==
 *   target_text), matching the "safe pattern" the assessment describes.
 *   Two intents must NOT be disturbed by the fix and are pinned below:
 *   an EXPLICIT new_text: "" stays a tracked deletion (delete-with-
 *   explanation is legitimate and documented — "empty string deletes"), and
 *   an uncommented modify without new_text must not silently delete either.
 *
 * - C4: "Adeu cannot see text inside floating text boxes or form-style
 *   content controls, and it does not warn you about this." Reproduces.
 *   (a) iter_block_items (utils/docx.ts) matches only w:p/w:tbl direct
 *   children, so a block-level w:sdt content control — which Word renders as
 *   ordinary flowed body text — vanishes from every view; it must be
 *   extracted. (b) w:txbxContent is handled nowhere, and an
 *   mc:AlternateContent-wrapped text box matches none of
 *   w:drawing/w:object/w:pict at the run-child level, so it projects NOTHING
 *   — no text, no image marker, no warning. The read output must either
 *   project the boxed text or disclose the skipped text box (inline marker
 *   or structural appendix).
 *
 * Every test is written test-first: it fails on current main and passes once
 * the finding is fixed.
 */
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";
import { _extractTextFromDoc } from "./ingest.js";

const LIABILITY_SENTENCE =
  "Liability is capped at the fees paid in the preceding twelve months.";

async function docWithSentence() {
  const doc = await createTestDocument();
  addParagraph(doc, LIABILITY_SENTENCE);
  return doc;
}

describe("C3: comment without replacement text must not delete the sentence", () => {
  it("modify + comment with new_text ABSENT anchors a pure comment, not a tracked deletion", async () => {
    const doc = await docWithSentence();
    const engine = new RedlineEngine(doc, "QA Agent");

    // The exact shape the MCP zod schema admits: type given, new_text omitted.
    const edit = {
      type: "modify",
      target_text: LIABILITY_SENTENCE,
      comment: "Please reconsider this cap.",
    } as any;

    const stats = engine.process_batch([edit], false);
    expect(stats.edits_applied).toBe(1);

    const raw = _extractTextFromDoc(doc, false, false) as string;
    expect(raw).toContain("Please reconsider this cap.");
    expect(raw).not.toContain("{--"); // today: the whole sentence is a tracked deletion
    // The sentence must survive intact (a {==highlight==} anchor is fine).
    expect(raw.replace(/\{==|==\}/g, "")).toContain(LIABILITY_SENTENCE);

    const clean = _extractTextFromDoc(doc, true, false) as string;
    expect(clean).toContain(LIABILITY_SENTENCE);
  });

  it("pin: an EXPLICIT new_text of '' stays a delete-with-explanation", async () => {
    const doc = await docWithSentence();
    const engine = new RedlineEngine(doc, "QA Agent");

    const edit = {
      type: "modify",
      target_text: LIABILITY_SENTENCE,
      new_text: "",
      comment: "Removed per our call on 2026-07-20.",
    } as any;

    const stats = engine.process_batch([edit], false);
    expect(stats.edits_applied).toBe(1);

    const raw = _extractTextFromDoc(doc, false, false) as string;
    expect(raw).toContain("{--");
    expect(raw).toContain("Removed per our call on 2026-07-20.");
    const clean = _extractTextFromDoc(doc, true, false) as string;
    expect(clean).not.toContain(LIABILITY_SENTENCE);
  });

  it("pin: the safe pattern (new_text === target_text) stays COMMENT_ONLY", async () => {
    const doc = await docWithSentence();
    const engine = new RedlineEngine(doc, "QA Agent");

    const edit = {
      type: "modify",
      target_text: LIABILITY_SENTENCE,
      new_text: LIABILITY_SENTENCE,
      comment: "Please reconsider this cap.",
    } as any;

    const stats = engine.process_batch([edit], false);
    expect(stats.edits_applied).toBe(1);

    const raw = _extractTextFromDoc(doc, false, false) as string;
    expect(raw).not.toContain("{--");
    expect(raw).not.toContain("{++");
    expect(raw).toContain("Please reconsider this cap.");
  });

  it("an uncommented modify with new_text ABSENT must not silently delete the sentence", async () => {
    // Sibling trap to the headline finding: with no comment and no new_text
    // there is nothing to interpret — deleting a clause because a field was
    // forgotten is the worst available outcome. Reject (BatchValidationError)
    // or skip with an error report; anything but a silent tracked deletion.
    const doc = await docWithSentence();
    const engine = new RedlineEngine(doc, "QA Agent");

    const edit = { type: "modify", target_text: LIABILITY_SENTENCE } as any;

    let deleted = false;
    try {
      const stats = engine.process_batch([edit], false);
      const raw = _extractTextFromDoc(doc, false, false) as string;
      deleted = stats.edits_applied === 1 && raw.includes("{--");
    } catch {
      // A validation rejection is the expected fixed behavior.
    }
    expect(deleted, "omitting new_text (no comment) silently deleted the clause").toBe(false);
  });
});

const SDT_SENTENCE =
  "The Supplier shall indemnify the Client against third-party claims.";
const BOXED_SENTENCE =
  "Notice: delivery obligations are suspended during force majeure.";

function el(doc: any, tag: string, attrs: Record<string, string> = {}): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const node = xmlDoc.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function paragraphWithText(doc: any, text: string): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.setAttribute("xml:space", "preserve");
  t.textContent = text;
  r.appendChild(t);
  p.appendChild(r);
  return p;
}

describe("C4: invisible containers — block-level w:sdt and floating text boxes", () => {
  it("text inside a block-level content control (w:sdt) is extracted", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Intro paragraph before the content control.");

    const sdt = el(doc, "w:sdt");
    const sdtPr = el(doc, "w:sdtPr");
    sdtPr.appendChild(el(doc, "w:alias", { "w:val": "Indemnity" }));
    const sdtContent = el(doc, "w:sdtContent");
    sdtContent.appendChild(paragraphWithText(doc, SDT_SENTENCE));
    sdt.appendChild(sdtPr);
    sdt.appendChild(sdtContent);
    doc.element.appendChild(sdt);

    addParagraph(doc, "Tail paragraph after the content control.");

    const clean = _extractTextFromDoc(doc, true, false) as string;
    expect(clean).toContain("Intro paragraph before the content control.");
    expect(
      clean,
      "text inside a block-level content control (w:sdt) is invisible — " +
        "the indemnity obligation vanished from the read view",
    ).toContain(SDT_SENTENCE);
  });

  it("text inside a floating text box is disclosed in the read output", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text before the floating shape.");

    // The modern Word shape: run > mc:AlternateContent > mc:Choice(w:drawing
    // > … > wps:txbx > w:txbxContent) with an mc:Fallback(w:pict > v:shape >
    // v:textbox > w:txbxContent). Today this projects NOTHING — the run-child
    // dispatch only knows w:drawing/w:object/w:pict at the top level.
    const host = paragraphWithText(doc, "Anchor paragraph. ");
    const run = el(doc, "w:r");

    const alternate = el(doc, "mc:AlternateContent");
    const choice = el(doc, "mc:Choice", { Requires: "wps" });
    const drawing = el(doc, "w:drawing");
    const anchor = el(doc, "wp:anchor", { behindDoc: "0", relativeHeight: "2" });
    anchor.appendChild(el(doc, "wp:docPr", { id: "7", name: "Text Box 7" }));
    const graphic = el(doc, "a:graphic");
    const graphicData = el(doc, "a:graphicData", {
      uri: "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    });
    const wsp = el(doc, "wps:wsp");
    const txbx = el(doc, "wps:txbx");
    const txbxContent = el(doc, "w:txbxContent");
    txbxContent.appendChild(paragraphWithText(doc, BOXED_SENTENCE));
    txbx.appendChild(txbxContent);
    wsp.appendChild(txbx);
    graphicData.appendChild(wsp);
    graphic.appendChild(graphicData);
    anchor.appendChild(graphic);
    drawing.appendChild(anchor);
    choice.appendChild(drawing);
    alternate.appendChild(choice);

    const fallback = el(doc, "mc:Fallback");
    const pict = el(doc, "w:pict");
    const shape = el(doc, "v:shape", { id: "Text Box 7" });
    const vTextbox = el(doc, "v:textbox");
    const fallbackContent = el(doc, "w:txbxContent");
    fallbackContent.appendChild(paragraphWithText(doc, BOXED_SENTENCE));
    vTextbox.appendChild(fallbackContent);
    shape.appendChild(vTextbox);
    pict.appendChild(shape);
    fallback.appendChild(pict);
    alternate.appendChild(fallback);

    run.appendChild(alternate);
    host.appendChild(run);
    doc.element.appendChild(host);

    addParagraph(doc, "Body text after the floating shape.");

    const full = _extractTextFromDoc(doc, false, true) as string;
    const disclosed =
      full.includes(BOXED_SENTENCE) || /text\s*box/i.test(full);
    expect(
      disclosed,
      "a floating text box containing an obligation is completely invisible " +
        `in the read output, with no disclosure:\n${full}`,
    ).toBe(true);
  });
});
