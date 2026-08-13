import { describe, it, expect } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";

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

describe("RedlineEngine id_discovery_hint and _action_not_found_error (E3)", () => {
  it("default hint: accept on stale ID yields error containing generic default sentence when no hint passed", async () => {
    const doc = await buildDoc(`<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>`);
    const engine = new RedlineEngine(doc);
    expect(engine.id_discovery_hint).toBeNull();

    try {
      engine.process_batch([{ type: "accept", target_id: "Chg:999" }], undefined, false);
      expect.fail("Should have thrown BatchValidationError");
    } catch (err: any) {
      expect(err).toBeInstanceOf(BatchValidationError);
      expect(err.message).toContain(
        "Call extract_changes or list_changes on the document again to list current ids.",
      );
    }
  });

  it("custom hint: uses passed id_discovery_hint in error message when action ID not found", async () => {
    const customHint =
      "Call `read_docx` with `mode='changes'` on the document again to list the current change (Chg:) and comment (Com:) ids — ids shift between document states.";
    const doc = await buildDoc(`<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>`);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)", { id_discovery_hint: customHint });
    expect(engine.id_discovery_hint).toBe(customHint);

    try {
      engine.process_batch([{ type: "accept", target_id: "Chg:999" }], undefined, false);
      expect.fail("Should have thrown BatchValidationError");
    } catch (err: any) {
      expect(err).toBeInstanceOf(BatchValidationError);
      expect(err.message).toContain(customHint);
    }
  });

  it("change/comment ID mix-up branches keep wording and append custom hint", async () => {
    const customHint = "CUSTOM_DISCOVERY_HINT_12345";
    const doc = await buildDoc(
      `<w:p>
        <w:ins w:id="1" w:author="Author"><w:r><w:t>Inserted</w:t></w:r></w:ins>
      </w:p>`,
      `<w:comments ${WORD_XMLNS}>
        <w:comment w:id="2" w:author="Author"><w:p><w:r><w:t>Comment text</w:t></w:r></w:p></w:comment>
      </w:comments>`,
    );

    const engine = new RedlineEngine(doc, "Author", { id_discovery_hint: customHint });

    // 1) reply on a tracked change ID (Chg:1)
    try {
      engine.process_batch([{ type: "reply", target_id: "Chg:1", text: "Reply text" }], undefined, false);
      expect.fail("Should have thrown");
    } catch (err: any) {
      expect(err.message).toContain("Chg:1 is a tracked-change id, not a comment.");
      expect(err.message).toContain(customHint);
    }

    // 2) accept on a comment ID (Com:2)
    try {
      engine.process_batch([{ type: "accept", target_id: "Com:2" }], undefined, false);
      expect.fail("Should have thrown");
    } catch (err: any) {
      expect(err.message).toContain("Com:2 is a comment id, not a tracked change.");
      expect(err.message).toContain(customHint);
    }

    // 3) reply on non-existent comment ID (Com:99)
    try {
      engine.process_batch([{ type: "reply", target_id: "Com:99", text: "Reply text" }], undefined, false);
      expect.fail("Should have thrown");
    } catch (err: any) {
      expect(err.message).toContain("no comment with that id exists.");
      expect(err.message).toContain(customHint);
    }

    // 4) accept on non-existent change ID (Chg:99)
    try {
      engine.process_batch([{ type: "accept", target_id: "Chg:99" }], undefined, false);
      expect.fail("Should have thrown");
    } catch (err: any) {
      expect(err.message).toContain("no tracked change with that id exists");
      expect(err.message).toContain(customHint);
    }
  });

  it("ID list capping at 20 (_format_id_list) remains unchanged", () => {
    const ids = Array.from({ length: 25 }, (_, i) => String(i + 1));
    const formatted = (RedlineEngine as any)._format_id_list(ids, "Chg:");
    expect(formatted).toContain("Chg:1");
    expect(formatted).toContain("Chg:20");
    expect(formatted).toContain("(+5 more)");
  });
});
