// FILE: node/packages/core/src/engine.review-comment.test.ts
import { describe, it, expect } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { extract_comments_data } from "./comments.js";
import { RedlineEngine } from "./engine.js";

/**
 * B4 (docs/improvement_spec.md §4): `comment` on an accept/reject action is a
 * rationale the reviewer wants to SEE in the margin, not a field the engine may
 * drop. The original reproduction (case 1 below) asserted the drop; it is kept
 * here inverted, as the regression assertion that the comment IS written.
 *
 * The hard part is the anchor: by the time the action has been applied the text
 * the rationale is about may be gone (rejecting an insertion deletes it). The
 * comment then lands on the nearest surviving run of the SAME paragraph, and
 * when the paragraph has no surviving run at all it is not written anywhere —
 * a rationale on unrelated text is worse than no rationale, so the batch says
 * so in a note instead.
 *
 * ⚠ Node leads Python here; see the release gate in the plan (Task 10).
 */

const WORD_XMLNS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

const AUTHOR = "Authority Counsel";

function xmlDecl(body: string): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body;
}

/**
 * Minimal, valid DOCX built from a body fragment, optionally with a comments
 * part. No fixture dependency: every case here is about one paragraph's shape.
 */
async function buildDoc(
  bodyXml: string,
  commentsXml?: string,
): Promise<DocumentObject> {
  const contentTypes = xmlDecl(
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>${
    commentsXml
      ? `
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>`
      : ""
  }
</Types>`,
  );

  const rootRels = xmlDecl(
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`,
  );

  const documentRels = xmlDecl(
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${
      commentsXml
        ? `
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>`
        : ""
    }
</Relationships>`,
  );

  const documentXml = xmlDecl(
    `<w:document ${WORD_XMLNS}>
  <w:body>
${bodyXml}
  </w:body>
</w:document>`,
  );

  const zip: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(contentTypes),
    "_rels/.rels": strToU8(rootRels),
    "word/document.xml": strToU8(documentXml),
    "word/_rels/document.xml.rels": strToU8(documentRels),
  };
  if (commentsXml) zip["word/comments.xml"] = strToU8(xmlDecl(commentsXml));

  return DocumentObject.load(Buffer.from(zipSync(zip)));
}

/** A tracked insertion by "Jane Doe" between two plain runs of one paragraph. */
const WRAPPED_INSERTION = `    <w:p w14:paraId="00000001">
      <w:r><w:t xml:space="preserve">Prefix text. </w:t></w:r>
      <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      <w:r><w:t xml:space="preserve"> suffix text.</w:t></w:r>
    </w:p>`;

/** The same insertion, alone in its paragraph: rejecting it leaves no run. */
const ISOLATED_INSERTION = `    <w:p w14:paraId="00000001">
      <w:r><w:t>Untouched neighbour.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="00000002">
      <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
    </w:p>`;

async function reload(doc: DocumentObject): Promise<DocumentObject> {
  return DocumentObject.load(await doc.save());
}

/** Paragraphs of the main story, in document order. */
function paragraphs(doc: DocumentObject): Element[] {
  return Array.from(doc.element.getElementsByTagName("w:p"));
}

function hasCommentRangeStart(p: Element): boolean {
  return p.getElementsByTagName("w:commentRangeStart").length > 0;
}

describe("B4: `comment` on accept/reject becomes a margin comment", () => {
  it("writes the rationale of a reject as a comment that survives a save/reload", async () => {
    // Reproduction, inverted: this batch used to discard `comment` silently.
    const doc = await buildDoc(WRAPPED_INSERTION);
    const engine = new RedlineEngine(doc, AUTHOR);
    engine.process_batch([
      { type: "reject", target_id: "Chg:1", comment: "out of scope" } as any,
    ]);

    const data = extract_comments_data((await reload(doc)).pkg);
    expect(Object.keys(data).length).toBe(1);
    expect(Object.values(data)[0].text).toContain("out of scope");
  });

  it("anchors an accept rationale inside the paragraph that held the change", async () => {
    const doc = await buildDoc(WRAPPED_INSERTION);
    const engine = new RedlineEngine(doc, AUTHOR);
    engine.process_batch([
      {
        type: "accept",
        target_id: "Chg:1",
        comment: "agreed, this clarifies the scope",
      } as any,
    ]);

    const reloaded = await reload(doc);
    const data = extract_comments_data(reloaded.pkg);
    expect(Object.keys(data).length).toBe(1);
    const only = Object.values(data)[0];
    expect(only.author).toBe(AUTHOR);
    expect(only.text).toContain("agreed, this clarifies the scope");

    // The accept took effect, and the bubble sits in the host paragraph.
    expect(reloaded.element.getElementsByTagName("w:ins").length).toBe(0);
    expect(paragraphs(reloaded).filter(hasCommentRangeStart).length).toBe(1);
    expect(hasCommentRangeStart(paragraphs(reloaded)[0])).toBe(true);
  });

  it("anchors a reject rationale on a surviving run after the inserted text is gone", async () => {
    const doc = await buildDoc(WRAPPED_INSERTION);
    const engine = new RedlineEngine(doc, AUTHOR);
    engine.process_batch([
      { type: "reject", target_id: "Chg:1", comment: "out of scope" } as any,
    ]);

    const reloaded = await reload(doc);
    expect(Object.keys(extract_comments_data(reloaded.pkg)).length).toBe(1);

    // The inserted text is gone; the rationale hangs off a run that is not.
    const host = paragraphs(reloaded)[0];
    expect(host.getElementsByTagName("w:ins").length).toBe(0);
    expect(hasCommentRangeStart(host)).toBe(true);
    const start = host.getElementsByTagName("w:commentRangeStart")[0];
    let next = start.nextSibling;
    while (next && next.nodeType !== 1) next = next.nextSibling;
    expect((next as Element).tagName).toBe("w:r");
  });

  it("writes no comment, and says why, when the host paragraph keeps no run", async () => {
    const doc = await buildDoc(ISOLATED_INSERTION);
    const engine = new RedlineEngine(doc, AUTHOR);
    const stats = engine.process_batch([
      { type: "reject", target_id: "Chg:1", comment: "out of scope" } as any,
    ]);

    // The reject itself succeeded.
    expect(stats.actions_applied).toBe(1);

    const reloaded = await reload(doc);
    expect(extract_comments_data(reloaded.pkg)).toEqual({});
    // Never on an unrelated paragraph.
    expect(paragraphs(reloaded).some(hasCommentRangeStart)).toBe(false);

    expect(stats.skipped_details).toContain(
      "- Note: Action 1 ('reject' on Chg:1) — the rationale could not be anchored " +
        "(the resolved text left no surviving run); the reject itself succeeded.",
    );
  });

  it("reports the comment id in a note the batch report files under 'Notes:'", async () => {
    const doc = await buildDoc(WRAPPED_INSERTION);
    const engine = new RedlineEngine(doc, AUTHOR);
    const stats = engine.process_batch([
      { type: "reject", target_id: "Chg:1", comment: "out of scope" } as any,
    ]);

    const cid = Object.keys(extract_comments_data(doc.pkg))[0];
    expect(cid).toBeDefined();
    expect(stats.skipped_details).toContain(
      `- Note: Action 1 ('reject' on Chg:1) — rationale recorded as Com:${cid}.`,
    );
    // formatBatchResult (mcp-server/src/index.ts) files a batch under "Notes:"
    // only when every detail line starts with "- Note:" — an informational
    // line must never turn a clean batch into "Skipped Details".
    expect(
      stats.skipped_details.every((d: string) => d.startsWith("- Note:")),
    ).toBe(true);
    expect(stats.actions_skipped).toBe(0);
    expect(stats.actions_applied).toBe(1);
  });

  it("adds nothing when the action carries no comment", async () => {
    const doc = await buildDoc(WRAPPED_INSERTION);
    const parts_before = doc.pkg.parts.length;
    const engine = new RedlineEngine(doc, AUTHOR);
    const stats = engine.process_batch([
      { type: "accept", target_id: "Chg:1" } as any,
    ]);

    expect(stats.skipped_details).toEqual([]);
    expect(doc.pkg.parts.length).toBe(parts_before);
    const reloaded = await reload(doc);
    expect(extract_comments_data(reloaded.pkg)).toEqual({});
    expect(paragraphs(reloaded).some(hasCommentRangeStart)).toBe(false);
  });

  it("leaves `reply` actions alone", async () => {
    const doc = await buildDoc(
      `    <w:p w14:paraId="00000001">
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Reviewed text.</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
    </w:p>`,
      `<w:comments ${WORD_XMLNS}>
  <w:comment w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z" w:initials="JD"><w:p><w:r><w:t>please confirm</w:t></w:r></w:p></w:comment>
</w:comments>`,
    );

    const engine = new RedlineEngine(doc, AUTHOR);
    // The stray `comment` field must not mint a second bubble: a reply's
    // content is its `text`, and the reply branch returns before B4 runs.
    const stats = engine.process_batch([
      {
        type: "reply",
        target_id: "Com:1",
        text: "confirmed",
        comment: "should be ignored",
      } as any,
    ]);

    expect(stats.actions_applied).toBe(1);
    expect(stats.skipped_details).toEqual([]);
    const data = extract_comments_data((await reload(doc)).pkg);
    expect(Object.keys(data).length).toBe(2);
    expect(
      Object.values(data).some((c: any) => c.text.includes("should be ignored")),
    ).toBe(false);
  });
});
