import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";
import { split_structural_appendix } from "./pagination.js";

// Local copies of the two helpers in domain.test.ts (test-utils exports no
// bookmark/cross-reference builders), so this file stands alone.
function addBookmark(paragraph: Element, name: string, idVal: string) {
  const doc = paragraph.ownerDocument!;
  const start = doc.createElement("w:bookmarkStart");
  start.setAttribute("w:name", name);
  start.setAttribute("w:id", idVal);
  paragraph.appendChild(start);
  const end = doc.createElement("w:bookmarkEnd");
  end.setAttribute("w:id", idVal);
  paragraph.appendChild(end);
}

function addCrossReference(paragraph: Element, refName: string, text: string) {
  const doc = paragraph.ownerDocument!;
  const fld = doc.createElement("w:fldSimple");
  fld.setAttribute("w:instr", ` REF ${refName} \\h `);
  const r = doc.createElement("w:r");
  const t = doc.createElement("w:t");
  t.textContent = text;
  if (text.includes(" ")) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  fld.appendChild(r);
  paragraph.appendChild(fld);
}

async function appendixOf(doc: any): Promise<string> {
  const buf = await doc.save();
  const full_text = await extractTextFromBuffer(buf, false);
  const [, appendix] = split_structural_appendix(full_text);
  return appendix;
}

describe("domain metadata: bookmark names that collide with Object.prototype keys", () => {
  it("records a referenced bookmark named like a prototype method", async () => {
    const doc = await createTestDocument();
    const anchored = addParagraph(doc, "Section 9. Termination");
    addBookmark(anchored, "toString", "1");
    const referring = addParagraph(doc, "As stated in ");
    addCrossReference(referring, "toString", "Section 9");

    // Before the fix this throws:
    // TypeError: Cannot read properties of undefined (reading 'push')
    const appendix = await appendixOf(doc);

    expect(appendix).toContain("## Named Anchors");
    expect(appendix).toContain(
      '- toString \u2192 Anchored to: "Section 9. Termination"',
    );
    expect(appendix).toContain('Referenced from: "As stated in Section 9"');
  });

  it("records an unreferenced bookmark named 'constructor'", async () => {
    const doc = await createTestDocument();
    const anchored = addParagraph(doc, "Section 4. Payment");
    addBookmark(anchored, "constructor", "1");

    const appendix = await appendixOf(doc);

    expect(appendix).toContain("## Named Anchors");
    expect(appendix).toContain(
      '- constructor \u2192 Anchored to: "Section 4. Payment"',
    );
  });
});
