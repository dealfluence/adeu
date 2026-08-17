import { describe, it, expect } from 'vitest';
import { strFromU8, unzipSync } from 'fflate';
import { createTestDocument, addParagraph } from './test-utils.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine } from './engine.js';

describe('Surgical Word-Level Diffing', () => {
  it('preserves interior unchanged words as bare text', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumped.");
    
    const engine = new RedlineEngine(doc, "Test AI");
    engine.process_batch([{
      type: "modify",
      target_text: "The quick brown fox jumped.",
      new_text: "The slow brown fox leapt."
    } as any]);
    
    const outBuf = await doc.save();
    const resultText = await extractTextFromBuffer(outBuf, false);
    
    expect(resultText).not.toContain("{--The quick brown fox jumped.--}");
    expect(resultText).toContain("{--quick--}{++slow++}");
    expect(resultText).toContain(" brown fox ");
    expect(resultText).toContain("{--jumped--}{++leapt++}");
  });
});

async function applyModify(targetText: string, newText: string) {
  const doc = await createTestDocument();
  addParagraph(doc, targetText);

  const engine = new RedlineEngine(doc, "Test AI");
  engine.process_batch([{
    type: "modify",
    target_text: targetText,
    new_text: newText,
  } as any]);

  const outBuf = await doc.save();
  return {
    raw: await extractTextFromBuffer(outBuf, false),
    clean: await extractTextFromBuffer(outBuf, true),
    paragraphs: Array.from(doc.element.childNodes).filter(
      (n: any) => n.tagName === "w:p",
    ),
    bodyXml: strFromU8(unzipSync(new Uint8Array(outBuf))["word/document.xml"]),
  };
}

describe('Single-line insertion fragments are never block-converted', () => {
  /**
   * Regression: a single-line word-diff insertion fragment beginning with "- "
   * (hyphen + space) must stay an inline tracked insertion.
   *
   * modify("Product" -> "Product - Draft") trims the common prefix and mints an
   * INSERTION sub-edit whose new_text is " - Draft". `_parse_markdown_style`
   * trimStart()s that to "- ", reads it as a bullet marker, and because the
   * anchor supplies a paragraph context the gate at engine.ts:2120
   * (`block_mode = first_style !== null && have_paragraph_context`) fires: the
   * fragment becomes a brand-new numbered ListParagraph, the "- " is eaten as a
   * fabricated list marker, and the edit still reports status "applied".
   * Silent structural corruption, valid OOXML, no warning.
   *
   * A fragment with no line break is by construction part of an existing
   * paragraph and must never enter heading/list-style conversion.
   */
  it('keeps a hyphen-prefixed fragment inline instead of making a ListParagraph', async () => {
    const { raw, clean, paragraphs, bodyXml } = await applyModify(
      "Product",
      "Product - Draft",
    );

    // 1. The insertion is inline and the literal hyphen survives verbatim.
    expect(raw).toContain("Product{++ - Draft++}");

    // 2. No paragraph split and no fabricated bullet marker.
    expect(clean).not.toContain("\n\n");
    expect(raw).not.toContain("* {++Draft++}");
    expect(clean.trim()).toBe("Product - Draft");

    // 3. Structurally: still one paragraph, no list style, no numbering.
    expect(paragraphs.length).toBe(1);
    expect(bodyXml).not.toContain("w:numPr");
    expect(bodyXml).not.toContain("ListParagraph");
  });

  it('matches the em-dash control, which already behaves correctly', async () => {
    const hyphen = await applyModify("Product", "Product - Draft");
    const emDash = await applyModify("Product", "Product \u2014 Draft");

    expect(emDash.raw).toContain("Product{++ \u2014 Draft++}");
    expect(hyphen.raw.replace(" - ", " \u2014 ")).toBe(emDash.raw);
  });

  it('still creates a real list paragraph for genuine multi-line bullet inserts', async () => {
    const { paragraphs, bodyXml } = await applyModify(
      "Intro",
      "Intro\n\n- Bullet one",
    );

    expect(paragraphs.length).toBe(2);
    expect(bodyXml).toContain("w:numPr");
    expect(bodyXml).toContain("ListParagraph");
  });
});
