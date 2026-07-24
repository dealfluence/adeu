// FILE: node/packages/core/src/engine.comment-preservation.test.ts
import { describe, it, expect } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { extract_comments_data } from "./comments.js";
import { RedlineEngine } from "./engine.js";

/**
 * Regression test for wrapping-comment handling on accept/reject.
 *
 * Word semantics, matching the Python engine (QA round 3, finding 1.1):
 *
 *  - ACCEPT keeps a wrapping comment — whoever authored it — anchored on the
 *    surviving text. The earlier author-aware design detached the anchors and
 *    kept only foreign BODIES, leaving orphaned, invisible comments in
 *    word/comments.xml (silent data loss in legal review).
 *  - REJECT of an insertion removes the inserted text, so a comment anchored
 *    on it goes with it — and the removal is REPORTED by id in the batch
 *    notes, never silent.
 *
 * The triggering document is built in-memory as a minimal valid .docx so the
 * test doesn't depend on the contents of any golden fixture: a paragraph with a
 * foreign <w:ins id="2"> whose run is wrapped by comment id="1" from that same
 * foreign author.
 */

const WORD_XMLNS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

function xmlDecl(body: string): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body;
}

/**
 * Build a minimal, valid DOCX buffer containing a wrapped foreign insertion:
 *
 *   commentRangeStart(1) → <w:ins id=2 author=insAuthor>run("INSERTED")</w:ins>
 *   → commentRangeEnd(1) → <w:r><w:commentReference id=1/></w:r>
 *
 * plus a comments part with one comment id=1 authored by commentAuthor, body
 * "robust protection". Includes [Content_Types].xml (with the comments
 * Override) and word/_rels/document.xml.rels so `load()` classifies the parts.
 */
async function buildWrappedInsertionDoc(
  insAuthor: string,
  commentAuthor: string,
): Promise<DocumentObject> {
  const contentTypes = xmlDecl(
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>`,
  );

  const rootRels = xmlDecl(
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`,
  );

  const documentRels = xmlDecl(
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>`,
  );

  const documentXml = xmlDecl(
    `<w:document ${WORD_XMLNS}>
  <w:body>
    <w:p w14:paraId="00000001">
      <w:r><w:t xml:space="preserve">Prefix text. </w:t></w:r>
      <w:commentRangeStart w:id="1"/>
      <w:ins w:id="2" w:author="${insAuthor}" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      <w:r><w:t xml:space="preserve"> suffix text.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>`,
  );

  const commentsXml = xmlDecl(
    `<w:comments ${WORD_XMLNS}>
  <w:comment w:id="1" w:author="${commentAuthor}" w:date="2026-01-01T00:00:00Z" w:initials="SC"><w:p><w:r><w:t>robust protection</w:t></w:r></w:p></w:comment>
</w:comments>`,
  );

  const zip: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(contentTypes),
    "_rels/.rels": strToU8(rootRels),
    "word/document.xml": strToU8(documentXml),
    "word/comments.xml": strToU8(commentsXml),
    "word/_rels/document.xml.rels": strToU8(documentRels),
  };

  const buf = Buffer.from(zipSync(zip));
  return DocumentObject.load(buf);
}

describe("author-aware wrapping-comment preservation", () => {
  it("keeps a foreign author's wrapping comment when their change is accepted", async () => {
    const doc = await buildWrappedInsertionDoc(
      "Supplier's Counsel",
      "Supplier's Counsel",
    );

    // Sanity: comment present before we touch anything.
    const before = extract_comments_data(doc.pkg);
    expect(Object.keys(before).length).toBe(1);
    expect(before["1"].text).toContain("robust protection");

    // We ("Authority Counsel") accept the counterparty's insertion Chg:2.
    const engine = new RedlineEngine(doc, "Authority Counsel");
    engine.process_batch(
      [{ type: "accept", target_id: "Chg:2" } as any],
      false,
    );

    // The counterparty's comment body must survive the accept.
    const after = extract_comments_data(doc.pkg);
    expect(after["1"]?.text).toContain("robust protection");

    // The accept must actually have taken effect: the <w:ins> is unwrapped.
    const savedBuf = await doc.save();
    const reloaded = await DocumentObject.load(savedBuf);
    expect(reloaded.element.getElementsByTagName("w:ins").length).toBe(0);

    // Comment survives the roundtrip too.
    const afterRoundtrip = extract_comments_data(reloaded.pkg);
    expect(afterRoundtrip["1"]?.text).toContain("robust protection");
  });

  it("rejecting an insertion removes its wrapping comment AND reports the removal", async () => {
    const doc = await buildWrappedInsertionDoc(
      "Supplier's Counsel",
      "Supplier's Counsel",
    );

    const engine = new RedlineEngine(doc, "Authority Counsel");
    engine.process_batch(
      [{ type: "reject", target_id: "Chg:2" } as any],
      false,
    );

    // Rejecting removes the inserted TEXT; a comment anchored on that text
    // goes with it (Word semantics, Python parity) — keeping the body while
    // stripping the anchors would leave an orphaned, invisible comment.
    const after = extract_comments_data(doc.pkg);
    expect(after["1"]).toBeUndefined();

    // Never silently: the batch notes name the removed comment
    // (QA round 3, finding 3.4).
    expect(
      engine.skipped_details.some((d) => /also removed comment Com:1/.test(d)),
    ).toBe(true);
  });

  it("keeps our OWN wrapping comment when we accept our own change", async () => {
    // Accept preserves the annotation regardless of author — the note
    // documents the very change being finalized (Python parity,
    // QA round 3 finding 1.1).
    const doc = await buildWrappedInsertionDoc(
      "Authority Counsel",
      "Authority Counsel",
    );

    const engine = new RedlineEngine(doc, "Authority Counsel");
    engine.process_batch(
      [{ type: "accept", target_id: "Chg:2" } as any],
      false,
    );

    const after = extract_comments_data(doc.pkg);
    expect(after["1"]?.text).toContain("robust protection");
  });
});
