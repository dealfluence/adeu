/**
 * Markdown block markers restyle a paragraph only when they govern the whole
 * paragraph — and when they do, the marker is consumed rather than leaked.
 *
 * Two defects are pinned here, both found while closing a dual-engine parity
 * gap. Mirror of python/tests/test_repro_block_marker_paragraph_parity.py.
 *
 * 1. Python leaked the marker. Its `_maybe_paragraph_replace` admitted HEADINGS
 *    ONLY, so a whole-paragraph replace like modify("Alpha", "- Beta") fell
 *    through to the inline path and wrote a literal "- Beta" with no style,
 *    while this engine consumed the "- " and applied ListParagraph + numPr.
 *    Same edit, same input, two different documents.
 *
 * 2. This engine corrupted mid-paragraph edits. The restyle trigger at
 *    _pre_resolve_heuristic_edit was a bare `target_style !== new_style` with
 *    no paragraph-boundary test, so modify("Gamma", "- Delta") against
 *    "Alpha Gamma" bulleted the entire host paragraph AND emitted an EMPTY
 *    <w:del> — "Gamma" survived, projecting "* Alpha DeltaGamma". The
 *    _match_spans_whole_paragraph gate fixes it; Python was already correct
 *    here via the equivalent bounds test.
 *
 * The unifying rule, now enforced in both engines: a block marker means "block"
 * only when the edit spans the whole block. Same principle as the line-break
 * gate in _track_insert_multiline (see repro_surgical_word_diff.test.ts).
 */
import { describe, it, expect } from 'vitest';
import { strFromU8, unzipSync } from 'fflate';
import { createTestDocument, addParagraph } from './test-utils.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine } from './engine.js';

async function apply(paragraphText: string, targetText: string, newText: string) {
  const doc = await createTestDocument();
  addParagraph(doc, paragraphText);

  const engine = new RedlineEngine(doc, 'Test AI');
  engine.process_batch([{
    type: 'modify',
    target_text: targetText,
    new_text: newText,
  } as any]);

  const outBuf = await doc.save();
  const xml = strFromU8(unzipSync(new Uint8Array(outBuf))['word/document.xml']);
  return {
    raw: await extractTextFromBuffer(outBuf, false),
    clean: await extractTextFromBuffer(outBuf, true),
    styles: (xml.match(/w:pStyle w:val="([^"]*)"/g) || []).join(','),
    hasNumPr: xml.includes('w:numPr'),
    // The TS defect produced <w:delText/> with no content.
    hasEmptyDelText: /<w:delText[^>]*\/>|<w:delText[^>]*><\/w:delText>/.test(xml),
  };
}

describe('Block markers restyle only whole paragraphs', () => {
  it('consumes a bullet marker and restyles on a whole-paragraph replace', async () => {
    const { raw, clean, styles, hasNumPr } = await apply('Alpha', 'Alpha', '- Beta');

    expect(styles).toContain('ListParagraph');
    expect(hasNumPr).toBe(true);
    expect(clean).toBe('* Beta');
    expect(raw).not.toContain('{++- Beta++}');
    expect(raw).toContain('{++Beta++}');
  });

  it('treats "* " and "- " identically', async () => {
    const hyphen = await apply('Alpha', 'Alpha', '- Beta');
    const asterisk = await apply('Alpha', 'Alpha', '* Beta');
    expect(hyphen).toEqual(asterisk);
  });

  it('still restyles for a heading marker', async () => {
    const { raw, clean, styles } = await apply('Alpha', 'Alpha', '# Beta');

    expect(styles).toContain('Heading1');
    expect(clean).toBe('# Beta');
    expect(raw).not.toContain('{++# Beta++}');
  });

  it('does not restyle from a mid-paragraph fragment, and keeps the deletion intact', async () => {
    const { raw, clean, styles, hasNumPr, hasEmptyDelText } = await apply(
      'Alpha Gamma',
      'Gamma',
      '- Delta',
    );

    expect(clean).toBe('Alpha - Delta');
    expect(styles).not.toContain('ListParagraph');
    expect(hasNumPr).toBe(false);
    // Regression: the deletion must carry text, and the target must be gone.
    expect(hasEmptyDelText).toBe(false);
    expect(raw).toContain('{--Gamma--}');
    expect(raw).toContain('{++- Delta++}');
  });

  it('keeps a mid-paragraph heading marker inline too', async () => {
    const { raw, clean, styles, hasEmptyDelText } = await apply(
      'Alpha Gamma',
      'Gamma',
      '# Delta',
    );

    expect(clean).toBe('Alpha # Delta');
    expect(styles).not.toContain('Heading');
    expect(hasEmptyDelText).toBe(false);
    expect(raw).toContain('{--Gamma--}');
  });

  it('leaves an unmarked replacement alone', async () => {
    const { raw, clean, styles, hasNumPr } = await apply('Alpha Gamma', 'Gamma', 'Delta');

    expect(clean).toBe('Alpha Delta');
    expect(styles).toBe('');
    expect(hasNumPr).toBe(false);
    expect(raw).toContain('{--Gamma--}{++Delta++}');
  });
});
