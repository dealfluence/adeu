import { describe, it, expect } from "vitest";
import { createTestDocumentWithComments, WORD_XMLNS } from "./test-utils.js";
import { extract_comments_data } from "./comments.js";

describe("extract_comments_data: comment ids that collide with Object.prototype keys", () => {
  it('keeps a comment whose w:id is "__proto__"', async () => {
    const bodyXml = `
    <w:p w14:paraId="00000001">
      <w:r><w:t xml:space="preserve">Body text. </w:t></w:r>
      <w:commentRangeStart w:id="__proto__"/>
      <w:r><w:t>anchored</w:t></w:r>
      <w:commentRangeEnd w:id="__proto__"/>
      <w:r><w:commentReference w:id="__proto__"/></w:r>
    </w:p>`;

    const commentsXml = `
<w:comments ${WORD_XMLNS}>
  <w:comment w:id="__proto__" w:author="QA" w:date="2026-01-01T00:00:00Z" w:initials="QA"><w:p><w:r><w:t>must not be dropped</w:t></w:r></w:p></w:comment>
</w:comments>`;

    const doc = await createTestDocumentWithComments(bodyXml, commentsXml);

    const data = extract_comments_data(doc.pkg);

    // On a `{}` literal, `data["__proto__"] = {...}` invokes the __proto__
    // setter: no own key is created and the comment vanishes.
    expect(Object.keys(data)).toEqual(["__proto__"]);
    expect(data["__proto__"].text).toContain("must not be dropped");
    expect(data["__proto__"].author).toBe("QA");
  });
});
