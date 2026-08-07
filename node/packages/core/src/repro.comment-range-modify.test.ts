// FILE: node/packages/core/src/repro.comment-range-modify.test.ts
//
// Reproduction for the field report (2026-07-03, PII-Shield Desktop / memo
// review):
//
//   "adeu's modify refuses to touch any text that sits inside another
//   author's comment range, misclassifying it as 'an active insertion from
//   another author.' This holds even when the target is fully inside the
//   span (confirmed via dry-run)."
//
// Scenario: a memo arrives with a colleague's margin comments anchored to
// plain, untracked body text. The agent is asked to amend that body text.
// The engine refuses with "Modification targets an active insertion from
// another author" even though no <w:ins> exists anywhere in the document —
// only a foreign COMMENT_ONLY annotation. The user's forced workaround
// (strip all comments, edit, re-diff) defeats the point of redlining.
//
// Root cause (engine.ts validate_edits): the foreign <w:ins> overlap check
// and the foreign comment-range overlap check feed the same rejection
// branch. Any foreign comment overlap — with zero insertions — is rejected,
// and with the insertion-specific error message.
//
// Word's native behavior: editing text under someone else's comment is a
// normal review workflow. The comment anchor persists and the edit becomes a
// tracked change. Deleting/replying to the COMMENT ITSELF is a different
// operation and stays protected.
//
// STYLE: these tests assert the DESIRED behaviour, so they are RED while the
// bug is present and turn GREEN once the engine is fixed (the "isolate the
// bug before fixing" pattern from AI_CONTEXT.md > Testing). The GREEN
// controls pin down the boundary of the fix: same-author comments must keep
// working, and the foreign tracked-INSERTION straddle protection must NOT be
// loosened. The Python twin is
// python/tests/test_repro_foreign_comment_range_modify.py.

import { describe, it, expect } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";

// ---------------------------------------------------------------------------
// Helpers (fixture builder mirrors comment_dedup.test.ts)
// ---------------------------------------------------------------------------

const NS_W =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
const NS_W14 =
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"';
const NS_R =
  'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';

function buildDocx(bodyXml: string, commentsListXml: string = ""): Buffer {
  const documentXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document ${NS_W} ${NS_W14} ${NS_R}>
  <w:body>${bodyXml}</w:body>
</w:document>`;

  const commentsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments ${NS_W} ${NS_W14}>${commentsListXml}</w:comments>`;

  const hasComments = commentsListXml.length > 0;

  const contentTypesXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  ${hasComments ? '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>' : ""}
</Types>`;

  const rootRelsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`;

  const docRelsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  ${hasComments ? '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>' : ""}
</Relationships>`;

  const files: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(contentTypesXml),
    "_rels/.rels": strToU8(rootRelsXml),
    "word/document.xml": strToU8(documentXml),
    "word/_rels/document.xml.rels": strToU8(docRelsXml),
  };
  if (hasComments) {
    files["word/comments.xml"] = strToU8(commentsXml);
  }

  return Buffer.from(zipSync(files));
}

/**
 * The reported document state: clean memo body text, one comment by
 * "Colleague" anchored to 'the budget is 40,000 EUR'. Zero tracked changes.
 */
function buildMemoWithColleagueComment(): Buffer {
  const body = `
    <w:p><w:r><w:t>MEMO</w:t></w:r></w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">The project deadline is 15 September 2026 and </w:t></w:r>
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t xml:space="preserve">the budget is 40,000 EUR</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Please review the terms above.</w:t></w:r></w:p>`;

  const comments = `<w:comment w:id="1" w:author="Colleague" w:date="2026-07-01T10:00:00Z" w:initials="CO">
    <w:p><w:r><w:t>Is this figure still correct?</w:t></w:r></w:p>
  </w:comment>`;

  return buildDocx(body, comments);
}

function countTag(el: any, tag: string): number {
  let n = 0;
  const walk = (node: any) => {
    if (node.tagName === tag) n++;
    let c = node.firstChild;
    while (c) {
      if (c.nodeType === 1) walk(c);
      c = c.nextSibling;
    }
  };
  walk(el);
  return n;
}

describe("Foreign comment-range modify repro — Node engine", () => {
  // ─────────────────────────────────────────────────────────────────────────
  // RED — the reported bug. Desired: the edit applies and the comment
  // survives.
  // ─────────────────────────────────────────────────────────────────────────
  it("RED: modifying text strictly inside a foreign comment range applies as a tracked change", async () => {
    const buf = buildMemoWithColleagueComment();

    // Fixture sanity: one comment, zero redlines.
    const before = await extractTextFromBuffer(buf, false);
    expect(before).toContain("[Com:1]");
    expect(before).not.toContain("{++");
    expect(before).not.toContain("{--");

    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Agent");

    // Target '40,000 EUR' sits strictly inside Colleague's comment span.
    // There is no <w:ins> anywhere in the document, so refusing this as "an
    // active insertion from another author" is a misclassification.
    const res = engine.process_batch(
      [{ type: "modify", target_text: "40,000 EUR", new_text: "45,000 EUR" }] as any[]);
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    const outBuf = await doc.save();
    const clean = await extractTextFromBuffer(outBuf, true);
    expect(clean).toContain("45,000 EUR");

    const marked = await extractTextFromBuffer(outBuf, false);
    // Edit must land as a tracked change, not a silent rewrite.
    expect(marked).toContain("{++");
    expect(marked).toContain("{--");
    expect(marked).toContain("Agent");

    // The colleague's annotation must survive the edit underneath it.
    expect(marked).toContain("Is this figure still correct?");
    expect(countTag(doc.element, "w:commentRangeStart")).toBe(1);
    expect(countTag(doc.element, "w:commentRangeEnd")).toBe(1);
    expect(countTag(doc.element, "w:commentReference")).toBe(1);
  });

  it("RED: modifying the entire foreign-commented span applies", async () => {
    // Boundary variant: the target coincides exactly with the commented
    // span, so the edit's edges touch the commentRangeStart/End markers.
    const doc = await DocumentObject.load(buildMemoWithColleagueComment());
    const engine = new RedlineEngine(doc, "Agent");

    const res = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "the budget is 40,000 EUR",
          new_text: "the budget is 45,000 EUR excluding VAT",
        },
      ] as any[]);
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    const outBuf = await doc.save();
    const clean = await extractTextFromBuffer(outBuf, true);
    expect(clean).toContain("45,000 EUR excluding VAT");

    const marked = await extractTextFromBuffer(outBuf, false);
    expect(marked).toContain("Is this figure still correct?");
    expect(countTag(doc.element, "w:commentRangeStart")).toBe(1);
    expect(countTag(doc.element, "w:commentRangeEnd")).toBe(1);
    expect(countTag(doc.element, "w:commentReference")).toBe(1);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // GREEN controls — pin the boundary of the fix.
  // ─────────────────────────────────────────────────────────────────────────
  it("GREEN control: the comment author editing their own commented text is not blocked", async () => {
    // Proves the rejection above is keyed purely on foreign authorship of
    // the comment, not on comment ranges per se.
    const doc = await DocumentObject.load(buildMemoWithColleagueComment());
    const engine = new RedlineEngine(doc, "Colleague");

    const res = engine.process_batch(
      [{ type: "modify", target_text: "40,000 EUR", new_text: "45,000 EUR" }] as any[]);
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);
  });

  it("GREEN control: a straddle of a foreign tracked INSERTION stays refused", async () => {
    // Must stay green after the fix: an edit partially straddling a foreign
    // author's real <w:ins> is still refused — that protection is legitimate
    // and must not be loosened while enabling comment-range edits.
    const body = `
      <w:p>
        <w:r><w:t xml:space="preserve">The quick </w:t></w:r>
        <w:ins w:id="100" w:author="Colleague" w:date="2026-07-01T10:00:00Z">
          <w:r><w:t>red</w:t></w:r>
        </w:ins>
        <w:r><w:t xml:space="preserve"> fox jumps.</w:t></w:r>
      </w:p>`;
    const doc = await DocumentObject.load(buildDocx(body));
    const engine = new RedlineEngine(doc, "Agent");

    // 'red fox' = foreign inserted 'red' + untracked body ' fox' -> straddle.
    expect(() =>
      engine.process_batch(
        [{ type: "modify", target_text: "red fox", new_text: "crimson wolf" }] as any[]),
    ).toThrow(/active insertion from another author/);
  });
});
