/**
 * Corpus validation against real government documents.
 *
 * Twin of python/tests/test_corpus_validation.py. The corpus documents are real
 * public-sector files that are deliberately NOT committed, so every test here
 * skips cleanly when its document is absent — CI is green without a download.
 */

import { describe, expect, it } from 'vitest';
import { unzipSync, strFromU8 } from 'fflate';
import { DOMParser } from '@xmldom/xmldom';
import { extractTextFromBuffer } from './ingest.js';
import { corpusBuffer, corpusPath, corpusSkipReason } from './test-utils.js';
import { DocumentObject } from './docx/bridge.js';
import { RedlineEngine } from './engine.js';
import { collectFields, readDocumentProtection, renderBanner, renderLine, resolveField } from './fields.js';
import { iter_document_parts_with_kind as iterDocumentPartsWithKind } from './utils/docx.js';

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
 * Single `w:t` nodes, not joined runs — runs split at arbitrary points and each
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

describe('A5 — corpus validation', () => {
  it('corpusPath throws on an unknown key rather than reporting absence', () => {
    // Absent document -> null (normal, fetch-on-demand). Unknown key -> throw.
    // Collapsing the two would make every typo a permanently green test.
    expect(() => corpusPath('no_such_document')).toThrow(/unknown corpus key/);
  });

  it('A5.1 — cell-level SDT content is visible at production scale', async (ctx) => {
    const data = corpusBuffer('fedramp_ssp_rev4');
    if (!data) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));

    const text = await project(data);
    expect(text.length).toBeGreaterThan(400_000);

    const cellTexts = cellLevelSdtTexts(data);
    // ~95% of the 2026-08-21 scan's 371 cell-level controls (spec-corpus §1).
    expect(cellTexts.length).toBeGreaterThanOrEqual(20);

    const missing = cellTexts.filter((value) => !text.includes(value));
    expect(missing.slice(0, 5)).toEqual([]);
  }, CORPUS_TIMEOUT);

  it('A5.1 — no raw OOXML leaks into the text projection', async (ctx) => {
    const data = corpusBuffer('fedramp_ssp_rev4');
    if (!data) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));

    const text = await project(data);
    for (const token of ['<w:sdt', 'sdtContent', 'w:sdtPr', 'showingPlcHdr']) {
      expect(text).not.toContain(token);
    }
  }, CORPUS_TIMEOUT);

  it('A5.7 — a .dotx template opens through the standard path', async (ctx) => {
    // Node passes this; Python cannot open a .dotx at all (python-docx rejects
    // 'template.main+xml'). Filed as CC-11 — this side of the parity gap is the
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

// ---------------------------------------------------------------------------
// CC-3b — the A5 examples that needed CC-1, CC-2, CC-4 and CC-5
//
// Twin of the CC-3b block in python/tests/test_corpus_validation.py. Floors are
// the frozen A5 numbers (~95% of the 2026-08-21 scan), not the counts measured
// today: these are living government templates revised in place, and an
// equality would make this suite a tripwire for upstream editing rather than
// for our own regressions.
// ---------------------------------------------------------------------------

const STOCK_PLACEHOLDER = 'Click or tap here to enter text.';

interface LedgerBundle {
  entries: ReturnType<typeof collectFields>;
  lines: string[];
  protection: ReturnType<typeof readDocumentProtection>;
  text: string;
}

async function ledgerFor(key: string): Promise<LedgerBundle | null> {
  const buf = corpusBuffer(key);
  if (!buf) return null;
  const doc = await DocumentObject.load(buf);
  const text = await extractTextFromBuffer(buf, false, false);
  const entries = collectFields(doc, text, null);
  const width = Math.max(4, ...entries.map((e) => `CC:${e.ordinal}`.length));
  return {
    entries,
    lines: entries.map((e) => renderLine(e, width)),
    protection: readDocumentProtection(doc),
    text,
  };
}

describe('CC-17 — a section break wrapped in a content control', () => {
  it(
    'is still a section, and its header is walked exactly once',
    async (ctx) => {
      const buf = corpusBuffer('fedramp_ssp_rev4');
      if (!buf) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));
      const doc = await DocumentObject.load(buf);

      const names: string[] = [];
      for (const [part, kind] of iterDocumentPartsWithKind(doc)) {
        if (kind === 'header' || kind === 'footer') names.push(String(part.partname));
      }

      // Two of this document's nine sectPr live at
      // body/sdt/sdtContent/p/pPr — a cover page and a title block inserted
      // from the document-part gallery. Walking only direct body children
      // missed both, and with them header1 and header2.
      expect(names.some((n) => n.endsWith('header2.xml'))).toBe(true);
      expect(names.some((n) => n.endsWith('header1.xml'))).toBe(true);

      // The other direction of the same bug: listing every part in the package
      // would also raise the count, while projecting inherited headers and
      // unreferenced orphans Word never renders. This port was already
      // corrected for that once — keep it corrected.
      expect(new Set(names).size, `part projected twice: ${names}`).toBe(names.length);
    },
    CORPUS_TIMEOUT,
  );

  it(
    'makes the five data-bound controls in that header resolvable',
    async (ctx) => {
      const loaded = await ledgerFor('fedramp_ssp_rev4');
      if (!loaded) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));

      expect(loaded.entries.length).toBe(5007);
      expect(loaded.entries.filter((e) => e.bound).length).toBe(94);

      // Each tagged control in that header is a SECOND occurrence of a tag that
      // already existed elsewhere, so the proof is the count: before the fix
      // each of these was one lower, and `set_field` answered "no such field"
      // for a field the user was looking at.
      for (const [tag, expected] of [
        ['cspname', 20],
        ['informationsystemname', 25],
        ['versionnumber', 5],
        ['versiondate', 5],
      ] as const) {
        expect(resolveField(loaded.entries, tag, 'all').length, tag).toBe(expected);
      }
    },
    CORPUS_TIMEOUT,
  );
});

describe('A5 (CC-3b) — ledger, gates and set_field on real documents', () => {
  it(
    'A5.1 — ledger class and state floors',
    async (ctx) => {
      const loaded = await ledgerFor('fedramp_ssp_rev4');
      if (!loaded) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));
      const bundle = loaded;

      expect(bundle.entries.length).toBeGreaterThanOrEqual(4750);

      const classes = new Map<string, number>();
      for (const e of bundle.entries) {
        classes.set(e.cls_word, (classes.get(e.cls_word) ?? 0) + 1);
      }
      for (const [cls, floor] of [
        ['checkbox', 3690],
        ['text', 430],
        ['date', 315],
        ['richtext', 260],
        ['combobox', 25],
        ['dropdown', 19],
        ['picture', 4],
      ] as const) {
        expect(classes.get(cls) ?? 0, `${cls} (${JSON.stringify([...classes])})`).toBeGreaterThanOrEqual(floor);
      }

      // 94, not 89: the five missing controls lived in a header referenced by a
      // section break wrapped in a content control (CC-17).
      expect(bundle.entries.filter((e) => e.bound).length).toBeGreaterThanOrEqual(94);
      expect(bundle.entries.filter((e) => e.empty).length).toBeGreaterThanOrEqual(680);
      expect(bundle.entries.filter((e) => e.container_kind === 'table cell').length).toBeGreaterThanOrEqual(350);
      expect(bundle.lines.filter((l) => l.includes('TEMPORARY')).length).toBeGreaterThanOrEqual(2);
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.2 — locks and anonymity on dau_acquisition_plan',
    async (ctx) => {
      const loaded = await ledgerFor('dau_acquisition_plan');
      if (!loaded) return ctx.skip(corpusSkipReason('dau_acquisition_plan'));
      const { entries, lines } = loaded;

      expect(entries.length).toBeGreaterThanOrEqual(154);
      expect(lines.filter((l) => l.includes('LOCKED (contents)')).length).toBeGreaterThanOrEqual(45);
      expect(entries.filter((e) => !e.alias && !e.tag).length).toBeGreaterThanOrEqual(150);

      const emptyWithPlaceholder = entries.filter((e) => e.empty && e.placeholder);
      expect(emptyWithPlaceholder.length).toBeGreaterThanOrEqual(38);
      expect(
        emptyWithPlaceholder.some((e) => (e.placeholder ?? '').trim() !== STOCK_PLACEHOLDER),
        'every placeholder is the stock string; the prose case is untested',
      ).toBe(true);
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.2 — a modify into a locked control is refused',
    async (ctx) => {
      const loaded = await ledgerFor('dau_acquisition_plan');
      if (!loaded) return ctx.skip(corpusSkipReason('dau_acquisition_plan'));
      const { entries, text } = loaded;

      // Unique in the projection: this document's FIRST locked control reads
      // "ACQUISITION PLAN for", which appears twice, and an ambiguous target is
      // refused by the matcher before any gate runs — so the naive pick passes
      // while proving nothing about locks.
      const locked = entries.filter(
        (e) => e.locked && e.value && e.value.trim() && text.split(e.value.trim()).length - 1 === 1,
      );
      expect(locked.length, 'no locked control with a unique text value').toBeGreaterThan(0);
      const target = locked[0];

      const doc = await DocumentObject.load(corpusBuffer('dau_acquisition_plan')!);
      const engine = new RedlineEngine(doc, 'A5 Corpus');
      const errors = engine.validate_edits([
        { type: 'modify', target_text: target.value!.trim(), new_text: 'REPLACED BY A TEST' },
      ]);

      expect(errors.length, `the edit into locked CC:${target.ordinal} was allowed`).toBeGreaterThan(0);
      const joined = errors.join('\n');
      expect(joined).toContain(`CC:${target.ordinal}`);
      expect(joined.toLowerCase()).toContain('content-locked');
      expect(joined, 'the refusal must name its override').toContain('ignore_control_locks');
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.3 — bound court fields are empty and bound',
    async (ctx) => {
      const loaded = await ledgerFor('wawd_esi_agreement');
      if (!loaded) return ctx.skip(corpusSkipReason('wawd_esi_agreement'));
      const { entries, lines } = loaded;

      const byTag = new Map(entries.filter((e) => e.tag).map((e) => [e.tag as string, e]));
      for (const tag of ['Plaintiff', 'Defendant', 'Case #']) {
        const entry = byTag.get(tag);
        expect(entry, `tag ${tag} missing from ${JSON.stringify([...byTag.keys()])}`).toBeDefined();
        expect(entry!.cls_word).toBe('text');
        expect(entry!.empty, `${tag} is not EMPTY`).toBe(true);
        expect(entry!.bound, `${tag} is not BOUND`).toBe(true);
      }
      expect(lines.every((l) => l.includes('BOUND'))).toBe(true);
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.3 — placeholders render as bubbles, never as bare body text',
    async (ctx) => {
      const loaded = await ledgerFor('wawd_esi_agreement');
      if (!loaded) return ctx.skip(corpusSkipReason('wawd_esi_agreement'));
      const { text } = loaded;

      // The discriminating form. A projection that dropped the control and
      // emitted its placeholder run as ordinary text would still contain the
      // string, and would read as a caption already filled in with a literal
      // "[Plaintiff]".
      for (const token of ['[Plaintiff]', '[Defendant]', '[Case #]']) {
        const total = text.split(token).length - 1;
        const bubbled = text.split(`{>>placeholder: ${token}<<}`).length - 1;
        expect(total, `${token} vanished from the projection`).toBeGreaterThan(0);
        expect(bubbled, `${token} also appears as bare body text`).toBe(total);
      }
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.4 — enforced forms protection: banner, gate, and both overrides',
    async (ctx) => {
      const loaded = await ledgerFor('on_juries_form1');
      if (!loaded) return ctx.skip(corpusSkipReason('on_juries_form1'));
      const { entries, protection, text } = loaded;

      expect(entries.length, 'document gained content controls').toBe(0);
      expect(protection.edit).toBe('forms');
      expect(protection.enforced).toBe(true);

      const banner = renderBanner(entries, protection);
      expect(banner).not.toBeNull();
      expect(banner!).toContain('fill-in-forms only (enforced)');
      expect(banner!).toContain('no content controls');

      const target = text
        .split('\n')
        .map((l) => l.trim())
        .find(
          (l) =>
            l.length >= 25 &&
            l.length <= 90 &&
            !l.includes('docx-image') &&
            !l.includes('{#') &&
            text.split(l).length - 1 === 1,
        );
      expect(target, 'no unique plain body line to target').toBeDefined();
      const edit = { type: 'modify', target_text: target!, new_text: `${target!} (edited)` };

      const buf = corpusBuffer('on_juries_form1')!;
      const refused = new RedlineEngine(await DocumentObject.load(buf), 'A5 Corpus').validate_edits([edit]);
      expect(refused.length, 'an edit into an enforced fill-in-forms document was allowed').toBeGreaterThan(0);
      expect(refused.join('\n')).toContain('ignore_document_protection');

      // A5.4 as frozen says the first override alone makes this apply. It does
      // not, and spec-gates §1a governs: Word records writes to a
      // forms-protected document as UNTRACKED, so a second, deliberately
      // separate gate stands behind the first. "I know it is protected" is not
      // "I accept an untracked write".
      const half = new RedlineEngine(await DocumentObject.load(buf), 'A5 Corpus', {
        ignore_document_protection: true,
      }).validate_edits([edit]);
      expect(half.length, 'the untracked-write gate did not fire').toBeGreaterThan(0);
      expect(half.join('\n')).toContain('allow_untracked_writes');

      const allowed = new RedlineEngine(await DocumentObject.load(buf), 'A5 Corpus', {
        ignore_document_protection: true,
        allow_untracked_writes: true,
      }).validate_edits([edit]);
      expect(allowed, 'both overrides did not clear the gates').toEqual([]);
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.5 — the dropdown prompt is carried as a real option',
    async (ctx) => {
      const loaded = await ledgerFor('ca_talent_recruitment');
      if (!loaded) return ctx.skip(corpusSkipReason('ca_talent_recruitment'));
      const { entries, lines } = loaded;

      // "Choose a type." is Word's prompt text but a genuine w:listItem, so the
      // ledger must list it rather than filtering it out as chrome — otherwise
      // the ledger disagrees with what Word offers the user.
      const dropdowns = entries.filter((e) => e.cls_word === 'dropdown');
      expect(dropdowns.length).toBe(1);
      expect([...dropdowns[0].options].slice(0, 3)).toEqual(['Choose a type.', 'Internal', 'External']);

      const line = lines.find((l) => l.includes('dropdown'))!;
      expect(line).toContain('Choose a type. | Internal | External');
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.5 — dropdown membership is caught during validation',
    async (ctx) => {
      const loaded = await ledgerFor('ca_talent_recruitment');
      if (!loaded) return ctx.skip(corpusSkipReason('ca_talent_recruitment'));
      const ordinal = loaded.entries.find((e) => e.cls_word === 'dropdown')!.ordinal;

      // G10 refuses before anything is written, like every CC-4 gate. CC-5
      // shipped it firing only at apply time; Mikko's 2026-08-22 ruling moved
      // it into validation so the gate contract is uniform — a caller learns
      // about a bad dropdown value at the same point it learns about locks and
      // protection, rather than one round trip later mid-write.
      const doc = await DocumentObject.load(corpusBuffer('ca_talent_recruitment')!);
      const errors = new RedlineEngine(doc, 'A5 Corpus').validate_edits([
        { type: 'set_field', field: `CC:${ordinal}`, value: 'External Hire' },
      ]);

      expect(errors.length, 'G10 did not fire during validation').toBeGreaterThan(0);
      expect(errors.join('\n')).toContain('Internal');
    },
    CORPUS_TIMEOUT,
  );

  it(
    'A5.6 — projection chrome stays within budget',
    async (ctx) => {
      const loaded = await ledgerFor('fedramp_ssp_rev4');
      if (!loaded) return ctx.skip(corpusSkipReason('fedramp_ssp_rev4'));
      const { text } = loaded;

      // Counts everything the projection adds — anchors, flags and placeholder
      // bubbles IN FULL, placeholder text included — against 7%. Mikko's
      // 2026-08-22 ruling: the same document measures 3.69% counting anchors
      // alone and 6.04% counting bubbles whole, so a 5% bound was a claim about
      // the measurement convention rather than about read cost.
      const opens = text.match(/\{#cc:\d+[^}]*\}/g) ?? [];
      const closes = text.match(/\{#\/cc:\d+\}/g) ?? [];
      const bubbles = text.match(/\{>>[^<]*<<\}/g) ?? [];
      const chrome = [...opens, ...closes, ...bubbles].reduce((n, t) => n + t.length, 0);

      // A ceiling passes hardest when the feature is broken, so pin the floor
      // that makes it meaningful.
      expect(opens.length).toBeGreaterThanOrEqual(1000);
      expect(closes.length).toBeGreaterThanOrEqual(1000);

      expect(chrome / text.length).toBeLessThanOrEqual(0.07);
    },
    CORPUS_TIMEOUT,
  );
});
