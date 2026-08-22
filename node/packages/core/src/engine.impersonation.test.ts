import { describe, it, expect } from "vitest";
import { RedlineEngine } from "./engine.js";
import { createTestDocumentWithComments as buildDoc, WORD_XMLNS } from "./test-utils.js";

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

  it("includes author_impersonation_warning on batch stats", async () => {
    const doc = await buildDoc(`
      <w:p w14:paraId="00000001">
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>
    `);
    const engine = new RedlineEngine(doc, "Jane Doe");
    const stats = engine.process_batch([
      { type: "modify", target_text: "INSERTED", new_text: "REPLACED" },
    ]);
    expect(stats.author_impersonation_warning).toBe(
      "[!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.",
    );
  });
});
