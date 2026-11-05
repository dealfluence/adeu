/**
 * Edits that KEEP a cross-reference or hyperlink must edit only the text
 * around it (found while reviewing the TechGC fork, 2026-11-05).
 *
 * A cross-reference projects as `[~2.1~](#_Ref123)`; the `_` in the bookmark
 * name routed the edit down the whole-span markdown path, which deleted the REF
 * field's result and inserted the projection markup as literal run text. A
 * bold hyperlink (`[**x**](mailto:x)`) failed the same way.
 *
 * Python twin: python/tests/test_repro_marker_preserving_edits.py
 */
import { describe, it, expect } from "vitest";
import { strToU8, strFromU8, unzipSync, zipSync } from "fflate";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const XREF_SENTENCE = "Except as expressly granted in Section 2.1, nothing else is licensed hereunder.";

const XREF_BODY =
  '<w:p><w:bookmarkStart w:id="1" w:name="_Ref123"/><w:r><w:t>2.1 License Grant.</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>' +
  '<w:p><w:r><w:t xml:space="preserve">Except as expressly granted in Section </w:t></w:r>' +
  '<w:r><w:fldChar w:fldCharType="begin"/></w:r>' +
  '<w:r><w:instrText xml:space="preserve"> REF _Ref123 \\r \\h </w:instrText></w:r>' +
  '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' +
  "<w:r><w:t>2.1</w:t></w:r>" +
  '<w:r><w:fldChar w:fldCharType="end"/></w:r>' +
  "<w:r><w:t>, nothing else is licensed hereunder.</w:t></w:r></w:p>";

const LINK_BODY =
  '<w:p><w:r><w:t xml:space="preserve">Email the team at </w:t></w:r>' +
  '<w:hyperlink r:id="rIdLink"><w:r><w:rPr><w:b/></w:rPr><w:t>legal@adeu.com</w:t></w:r></w:hyperlink>' +
  '<w:r><w:t xml:space="preserve"> before Friday.</w:t></w:r></w:p>';

function buildDocx(bodyXml: string): Buffer {
  const decl = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';
  return Buffer.from(
    zipSync({
      "[Content_Types].xml": strToU8(
        decl +
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
          '<Default Extension="xml" ContentType="application/xml"/>' +
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
          "</Types>",
      ),
      "_rels/.rels": strToU8(
        decl +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
          "</Relationships>",
      ),
      "word/_rels/document.xml.rels": strToU8(
        decl +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="mailto:legal@adeu.com" TargetMode="External"/>' +
          "</Relationships>",
      ),
      "word/document.xml": strToU8(
        decl + `<w:document xmlns:w="${W_NS}" xmlns:r="${R_NS}"><w:body>${bodyXml}</w:body></w:document>`,
      ),
    }),
  );
}

async function apply(body: string, target: string, newText: string, comment?: string) {
  const doc = await DocumentObject.load(buildDocx(body));
  const engine = new RedlineEngine(doc, "QA");
  const change: any = { type: "modify", target_text: target, new_text: newText };
  if (comment) change.comment = comment;
  const result = engine.process_batch([change]);
  const xml = strFromU8(unzipSync(new Uint8Array(await doc.save()))["word/document.xml"]);
  return { result, xml };
}

const texts = (xml: string, re: RegExp) => [...xml.matchAll(re)].map((m) => m[1]).join("");
const insertedText = (xml: string) =>
  [...xml.matchAll(/<w:ins\b[^>]*>([\s\S]*?)<\/w:ins>/g)].map((m) => texts(m[1], /<w:t[^>]*>([^<]*)<\/w:t>/g)).join("");
const deletedText = (xml: string) => texts(xml, /<w:delText[^>]*>([^<]*)<\/w:delText>/g);

/** Word's accept-all (accept=true) or reject-all reading of each paragraph. */
function paragraphTexts(xml: string, accept: boolean): string[] {
  let body = xml;
  body = accept
    ? body.replace(/<w:del\b[^>]*>[\s\S]*?<\/w:del>/g, "")
    : body.replace(/<w:ins\b[^>]*>[\s\S]*?<\/w:ins>/g, "").replace(/<(\/?)w:delText/g, "<$1w:t");
  return [...body.matchAll(/<w:p\b[^>]*>([\s\S]*?)<\/w:p>/g)].map((m) => texts(m[1], /<w:t(?:\s[^>]*)?>([^<]*)<\/w:t>/g));
}

describe("edits that keep a structural marker", () => {
  it("leave a kept cross-reference field intact", async () => {
    const { result, xml } = await apply(
      XREF_BODY,
      "Section [~2.1~](#_Ref123), nothing else is licensed hereunder.",
      "Section [~2.1~](#_Ref123), no other licence is granted hereunder.",
    );
    expect(result.edits_applied).toBe(1);
    expect(insertedText(xml)).not.toContain("[~");
    expect(deletedText(xml)).not.toContain("2.1");
    expect(xml).toContain("REF _Ref123");
    expect(paragraphTexts(xml, true)[1]).toBe(
      "Except as expressly granted in Section 2.1, no other licence is granted hereunder.",
    );
    expect(paragraphTexts(xml, false)[1]).toBe(XREF_SENTENCE);
  });

  it("edit just before a kept cross-reference without touching it", async () => {
    const { result, xml } = await apply(
      XREF_BODY,
      "expressly granted in Section [~2.1~](#_Ref123)",
      "granted in Section [~2.1~](#_Ref123)",
    );
    expect(result.edits_applied).toBe(1);
    expect(insertedText(xml)).not.toContain("[~");
    expect(deletedText(xml).trim()).toBe("expressly");
    expect(paragraphTexts(xml, true)[1]).toBe("Except as granted in Section 2.1, nothing else is licensed hereunder.");
  });

  it("leave a kept bold hyperlink intact", async () => {
    const { result, xml } = await apply(
      LINK_BODY,
      "Email the team at [**legal@adeu.com**](mailto:legal@adeu.com) before Friday.",
      "Please email the team at [**legal@adeu.com**](mailto:legal@adeu.com) by Friday.",
    );
    expect(result.edits_applied).toBe(1);
    expect(insertedText(xml)).not.toMatch(/\[|\]\(mailto/);
    expect(deletedText(xml)).not.toContain("legal@adeu.com");
    expect(texts(xml, /<w:hyperlink\b[^>]*>([\s\S]*?)<\/w:hyperlink>/g)).toContain("legal@adeu.com");
    expect(paragraphTexts(xml, true)[0]).toBe("Please email the team at legal@adeu.com by Friday.");
    expect(paragraphTexts(xml, false)[0]).toBe("Email the team at legal@adeu.com before Friday.");
  });

  it("attach a commented edit's comment once", async () => {
    const { result, xml } = await apply(
      XREF_BODY,
      "expressly granted in Section [~2.1~](#_Ref123), nothing else is licensed",
      "granted in Section [~2.1~](#_Ref123), no other licence is granted",
      "Tighten the grant language.",
    );
    expect(result.edits_applied).toBe(1);
    expect(insertedText(xml)).not.toContain("[~");
    expect(deletedText(xml)).not.toContain("2.1");
    expect((xml.match(/<w:commentReference\b/g) || []).length).toBe(1);
    expect(paragraphTexts(xml, true)[1]).toBe("Except as granted in Section 2.1, no other licence is granted hereunder.");
  });
});
