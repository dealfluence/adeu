/**
 * A5 â€” corpus validation against real government documents (task CC-3).
 *
 * Spec: specs/content-controls/spec-corpus.md Â·
 * Acceptance: specs/content-controls/acceptance/A5-corpus-validation.md
 *
 * Twin of python/tests/test_corpus_validation.py. The corpus documents are real
 * public-sector files that are deliberately NOT committed, so every test here
 * skips cleanly when its document is absent â€” CI is green without a download.
 *
 * Only the pre-CC-1 subset is implemented, as A5 itself instructs: without CC-1
 * there is no ledger, no `{#cc:N}` anchors, no `set_field` and no gates to
 * assert on. Deferred examples are tracked in PROGRESS.md against the task that
 * unblocks each, rather than stubbed here â€” a test that can never run looks
 * exactly like a passing one in a summary line.
 */

import { describe, expect, it } from 'vitest';
import { unzipSync, strFromU8 } from 'fflate';
import { DOMParser } from '@xmldom/xmldom';
import { extractTextFromBuffer } from './ingest.js';
import { corpusBuffer, corpusPath, corpusSkipReason } from './test-utils.js';

const W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';

/**
 * Corpus documents are two orders of magnitude larger than any fixture in this
 * suite — projecting `fedramp_ssp_rev4` takes ~2s alone and ~6s when the rest of
 * the suite is competing for the machine. Vitest's default 5s timeout therefore
 * makes these tests pass in isolation and fail in a full run, which is the worst
 * kind of red: it looks like a real regression and it is not reproducible.
 */
const CORPUS_TIMEOUT = 60_000;

function project(data: Buffer): Promise<string> {
  return extractTextFromBuffer(data, { cleanView: true, includeAppendix: false });
}

/**
 * Every distinct text (>= 20 chars) living inside a cell-level SDT
 * (`sdtContent > w:tc`). Derived from the document, never hardcoded: upstream
 * revises these templates in place, and literal strings would rot.
 *
 * Single `w:t` nodes, not joined runs â€” runs split at arbitrary points and each
 * engine reassembles them with its own whitespace rules, so a joined string is
 * not a substring of the output even when nothing is wrong.
 */
function cellLevelSdtTexts(data: Buffer): string[] {
  const unzipped = unzipSync(new Uint8Array(data));
  const xml = strFromU8(unzipped['word/document.xml']);
  const doc = new DOMParser().parseFromString(xml, 'text/xml');

  const texts = new Set<string>();
  const sdts = doc.getElementsByTagNameNS(W_NS, 'sdt');
  for (let i = 0; i < sdts.length; i++) {
    const contents = sdts[i].getElementsByTagNameNS(W_NS, 'sdtContent');
    const content = contents.length ? contents[0] : null;
    if (!content || content.parentNode !== sdts[i]) continue;

    let hasCellChild = false;
    for (let c = content.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 1 && (c as Element).localName === 'tc') hasCellChild = true;
    }
    if (!hasCellChild) continue;

    const nodes = content.getElementsByTagNameNS(W_NS, 't');
    for (let n = 0; n < nodes.length; n++) {
      const value = (nodes[n].textContent ?? '').trim();
      if (value.length >= 20) texts.add(value);
    }
  }
  return [...texts].sort();
}

describe('A5 â€” corpus validation', () => {
  it('corpusPath throws on an unknown key rather than reporting absence', () => {
    // Absent document -> null (normal, fetch-on-demand). Unknown key -> throw.
    // Collapsing the two would make every typo a permanently green test.
    expect(() => corpusPath('no_such_document')).toThrow(/unknown corpus key/);
  });

  it('A5.1 â€” cell-level SDT content is visible at production scale', async (ctx) => {
    const data = corpusBuffer('fedramp_ssp_rev4');
    if (!data) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));

    const text = await project(data);
    expect(text.length).toBeGreaterThan(400_000);

    const cellTexts = cellLevelSdtTexts(data);
    // ~95% of the 2026-08-21 scan's 371 cell-level controls (spec-corpus Â§1).
    expect(cellTexts.length).toBeGreaterThanOrEqual(20);

    const missing = cellTexts.filter((value) => !text.includes(value));
    expect(missing.slice(0, 5)).toEqual([]);
  }, CORPUS_TIMEOUT);

  it('A5.1 â€” no raw OOXML leaks into the text projection', async (ctx) => {
    const data = corpusBuffer('fedramp_ssp_rev4');
    if (!data) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));

    const text = await project(data);
    for (const token of ['<w:sdt', 'sdtContent', 'w:sdtPr', 'showingPlcHdr']) {
      expect(text).not.toContain(token);
    }
  }, CORPUS_TIMEOUT);

  it('A5.7 â€” a .dotx template opens through the standard path', async (ctx) => {
    // Node passes this; Python cannot open a .dotx at all (python-docx rejects
    // 'template.main+xml'). Filed as CC-11 â€” this side of the parity gap is the
    // evidence that the file is fine and the Python reader is not.
    const data = corpusBuffer('odot_uic_drywell');
    if (!data) return ctx.skip(corpusSkipReason('odot_uic_drywell'));

    const contentTypes = strFromU8(unzipSync(new Uint8Array(data))['[Content_Types].xml']);
    expect(contentTypes).toContain('template.main+xml');

    const text = await project(data);
    expect(text.trim().length).toBeGreaterThan(0);
    expect(text).not.toContain('<w:sdt');
  }, CORPUS_TIMEOUT);
});
