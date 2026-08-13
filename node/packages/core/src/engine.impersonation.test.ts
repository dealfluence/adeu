import { describe, it, expect } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { formatBatchResult } from "../../mcp-server/src/index.js";

const WORD_XMLNS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

function xmlDecl(body: string): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body;
}

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

describe("Author impersonation warning", () => {
  it("emits warning when acting author matches author with pending revisions", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:r><w:t>Prefix text. </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
        <w:r><w:t> suffix text.</w:t></w:r>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "Jane Doe");
    const stats = engine.process_batch([
      { type: "modify", target_text: "Prefix text.", new_text: "New prefix text." },
    ]);
    expect(stats.author_impersonation_warning).toBe(
      "[!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.",
    );
  });

  it("does not emit warning when acting author is different from pending revision authors", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:r><w:t>Prefix text. </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
        <w:r><w:t> suffix text.</w:t></w:r>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "John Smith");
    const stats = engine.process_batch([
      { type: "modify", target_text: "Prefix text.", new_text: "New prefix text." },
    ]);
    expect(stats.author_impersonation_warning).toBeNull();
  });

  it("does not emit warning on clean document with no pending revisions", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:r><w:t>Plain clean text.</w:t></w:r>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "Jane Doe");
    const stats = engine.process_batch([
      { type: "modify", target_text: "Plain clean text.", new_text: "Updated text." },
    ]);
    expect(stats.author_impersonation_warning).toBeNull();
  });

  it("warns when editing same author's earlier revisions but batch succeeds", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>Jane's old insertion.</w:t></w:r></w:ins>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "Jane Doe");
    const stats = engine.process_batch([
      { type: "modify", target_text: "Jane's old insertion.", new_text: "Jane's updated insertion." },
    ]);
    expect(stats.status).toBe("ok");
    expect(stats.author_impersonation_warning).toBe(
      "[!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.",
    );
  });

  it("counts comment authors as pending-revision authors", async () => {
    const commentsXml = `<w:comments ${WORD_XMLNS}>
      <w:comment w:id="1" w:author="Bob" w:date="2026-01-01T00:00:00Z">
        <w:p><w:r><w:t>Comment by Bob</w:t></w:r></w:p>
      </w:comment>
    </w:comments>`;
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:r><w:t>Text with comment.</w:t></w:r>
      </w:p>
    `, commentsXml);
    const engine = new RedlineEngine(doc, "Bob");
    const stats = engine.process_batch([
      { type: "modify", target_text: "Text with comment.", new_text: "Modified text." },
    ]);
    expect(stats.author_impersonation_warning).toBe(
      "[!] Warning: acting author 'Bob' matches an author with pending revisions in this document.",
    );
  });

  it("renders formatBatchResult with Warning prefix", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "Jane Doe");
    const stats = engine.process_batch([
      { type: "modify", target_text: "INSERTED", new_text: "REPLACED" },
    ]);
    const rendered = formatBatchResult(stats, "output.docx");
    expect(rendered).toContain("*Warning:* [!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.");
  });
});
