// FILE: node/packages/core/src/repro.comment-reference-style.test.ts
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { unzipSync, zipSync, strFromU8, strToU8 } from 'fflate';
import { DocumentObject } from './docx/bridge.js';
import { findChild, findChildren, parseXml } from './docx/dom.js';
import { CommentsManager } from './comments.js';
import { RedlineEngine } from './engine.js';
import { loadFixtureDoc } from './test-utils.js';
import { _get_style_cache } from './utils/docx.js';

/**
 * Comment reference typography: the styles Adeu's comment XML has always
 * referenced but never defined.
 *
 * `word/document.xml` wraps every `w:commentReference` in
 * `<w:rStyle w:val="CommentReference"/>` (engine.ts `_attach_comment`,
 * `_attach_comment_spanning`, `_anchor_reply_comment`) and every comment
 * paragraph in `word/comments.xml` carries `<w:pStyle w:val="CommentText"/>`
 * (comments.ts `addComment`). When `word/styles.xml` defines neither, Word and
 * LibreOffice silently ignore the dangling reference and render the marker at
 * the surrounding body size (11-12pt) instead of the 8pt superscript Word
 * writes natively, and extraction tools warn that both style ids are
 * referenced but not defined.
 *
 * Twin of python/tests/test_repro_comment_reference_style.py — the emitted
 * definitions must be identical in both engines.
 */

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const FIXTURES = resolve(__dirname, '../../../../shared/fixtures');

// base.docx defines Normal but NOT DefaultParagraphFont and NOT either comment
// style, so it exercises injection AND the conditional w:basedOn at once.
const BASE_EDIT_TARGET = [
  'reasonable skill and care',
  'reasonable skill, care and diligence',
] as const;

// golden.docx is Word-authored and holds none of base.docx's contract wording;
// its whole body is "This is the golden document". The target only has to make
// the engine attach a comment — the assertions are about styles.xml.
const GOLDEN_EDIT_TARGET = ['golden', 'gilded'] as const;

/**
 * A commenting edit, freshly built for one `apply_edits` call.
 *
 * Never a module-level constant. `apply_edits` (engine.ts:5306-5317) resets
 * only some of the per-edit scratch fields it writes onto the edit object, so
 * one shared literal carries a resolved offset from a PREVIOUS document into
 * the next run — the same trap the Python twin documents on `_edit()`.
 */
function makeEdit(target: readonly [string, string]): any {
  return {
    type: 'modify',
    target_text: target[0],
    new_text: target[1],
    comment: 'Aligning this with the SLA wording.',
  };
}

function fixtureBytes(name: string): Uint8Array {
  return new Uint8Array(readFileSync(resolve(FIXTURES, name)));
}

/** Applies the commenting edit and returns the saved package bytes. */
async function runCommentEdit(
  bytes: Uint8Array,
  target: readonly [string, string] = BASE_EDIT_TARGET,
): Promise<Uint8Array> {
  const doc = await DocumentObject.load(Buffer.from(bytes));
  const engine = new RedlineEngine(doc, 'Adeu AI (TS)');
  const [applied, skipped] = engine.apply_edits([makeEdit(target)]);
  expect([applied, skipped]).toEqual([1, 0]);
  return new Uint8Array(await doc.save());
}

function partText(bytes: Uint8Array, path: string): string {
  const entry = unzipSync(bytes)[path];
  if (!entry) throw new Error(`saved package has no ${path}`);
  return strFromU8(entry);
}

/** The `<w:styles>` element of a saved package. */
function stylesRoot(bytes: Uint8Array): Element {
  return parseXml(partText(bytes, 'word/styles.xml')).documentElement;
}

function stylesById(root: Element, styleId: string): Element[] {
  return findChildren(root, 'w:style').filter(
    (s) => s.getAttribute('w:styleId') === styleId,
  );
}

function styleById(root: Element, styleId: string): Element | null {
  return stylesById(root, styleId)[0] ?? null;
}

function childTags(el: Element): string[] {
  const tags: string[] = [];
  for (let i = 0; i < el.childNodes.length; i++) {
    const child = el.childNodes[i] as Element;
    if (child.nodeType === 1) tags.push(child.tagName);
  }
  return tags;
}

/** `<tag w:val="…"/>`'s value, direct children only. */
function val(el: Element, tag: string): string | null {
  const child = findChild(el, tag);
  return child ? child.getAttribute('w:val') : null;
}

describe('CommentReference / CommentText are defined, not just referenced', () => {
  it('injects both styles when word/styles.xml has neither', async () => {
    const root = stylesRoot(await runCommentEdit(fixtureBytes('base.docx')));

    const ref = styleById(root, 'CommentReference');
    expect(ref, 'CommentReference must be defined in word/styles.xml').not.toBeNull();
    expect(ref!.getAttribute('w:type')).toBe('character');
    expect(val(ref!, 'w:name')).toBe('annotation reference');
    expect(val(ref!, 'w:uiPriority')).toBe('99');
    expect(findChild(ref!, 'w:semiHidden')).not.toBeNull();
    expect(findChild(ref!, 'w:unhideWhenUsed')).not.toBeNull();
    const refRpr = findChild(ref!, 'w:rPr')!;
    expect(val(refRpr, 'w:sz')).toBe('16');
    expect(val(refRpr, 'w:szCs')).toBe('16');

    const txt = styleById(root, 'CommentText');
    expect(txt, 'CommentText must be defined in word/styles.xml').not.toBeNull();
    expect(txt!.getAttribute('w:type')).toBe('paragraph');
    expect(val(txt!, 'w:name')).toBe('annotation text');
    expect(val(txt!, 'w:uiPriority')).toBe('99');
    expect(findChild(txt!, 'w:semiHidden')).not.toBeNull();
    expect(findChild(txt!, 'w:unhideWhenUsed')).not.toBeNull();
    const txtRpr = findChild(txt!, 'w:rPr')!;
    expect(val(txtRpr, 'w:sz')).toBe('20');
    expect(val(txtRpr, 'w:szCs')).toBe('20');
  });

  it('emits CT_Style children in schema sequence', async () => {
    // ISO/IEC 29500 CT_Style sequence: name, basedOn, uiPriority, semiHidden,
    // unhideWhenUsed, rPr. Word tolerates a lot; the schema does not.
    const root = stylesRoot(await runCommentEdit(fixtureBytes('base.docx')));

    // base.docx has no DefaultParagraphFont, so CommentReference gets no basedOn.
    expect(childTags(styleById(root, 'CommentReference')!)).toEqual([
      'w:name',
      'w:uiPriority',
      'w:semiHidden',
      'w:unhideWhenUsed',
      'w:rPr',
    ]);
    // base.docx DOES define Normal, so CommentText keeps its basedOn.
    expect(childTags(styleById(root, 'CommentText')!)).toEqual([
      'w:name',
      'w:basedOn',
      'w:uiPriority',
      'w:semiHidden',
      'w:unhideWhenUsed',
      'w:rPr',
    ]);
    expect(val(styleById(root, 'CommentText')!, 'w:basedOn')).toBe('Normal');
  });

  it('writes w:basedOn when the base style exists (initial.docx)', async () => {
    // initial.docx DOES define DefaultParagraphFont, so the full requirement
    // XML applies there.
    const doc = await loadFixtureDoc('initial.docx');
    new CommentsManager(doc).addComment('Adeu AI', 'Typography check.');

    const root = doc.pkg.getPartByPath('word/styles.xml')!._element;
    expect(val(styleById(root, 'CommentReference')!, 'w:basedOn')).toBe(
      'DefaultParagraphFont',
    );
    expect(val(styleById(root, 'CommentText')!, 'w:basedOn')).toBe('Normal');
  });

  it('neither duplicates nor rewrites styles the document already has', async () => {
    // golden.docx already carries Word's own definitions, complete with the
    // w:rsid / w:link children this engine never writes.
    const before = stylesRoot(fixtureBytes('golden.docx'));
    expect(findChild(styleById(before, 'CommentReference')!, 'w:rsid')).not.toBeNull();
    expect(findChild(styleById(before, 'CommentText')!, 'w:link')).not.toBeNull();

    const root = stylesRoot(
      await runCommentEdit(fixtureBytes('golden.docx'), GOLDEN_EDIT_TARGET),
    );

    expect(stylesById(root, 'CommentReference')).toHaveLength(1);
    expect(stylesById(root, 'CommentText')).toHaveLength(1);
    // Word's own definitions survive untouched: an injected copy would have no
    // w:rsid and no w:link.
    expect(findChild(styleById(root, 'CommentReference')!, 'w:rsid')).not.toBeNull();
    expect(findChild(styleById(root, 'CommentText')!, 'w:link')).not.toBeNull();
  });

  it('does not inject when the annotation names exist under other style ids', async () => {
    const doc = await loadFixtureDoc('base.docx');
    const stylesEl = doc.pkg.getPartByPath('word/styles.xml')!._element;
    const xmlDoc = stylesEl.ownerDocument!;
    // Deliberately mixed case: the name match is case-insensitive.
    const others = [
      ['AnnotRef', 'character', 'Annotation Reference'],
      ['AnnotTxt', 'paragraph', 'ANNOTATION TEXT'],
    ] as const;
    for (const [styleId, sType, name] of others) {
      const style = xmlDoc.createElement('w:style');
      style.setAttribute('w:type', sType);
      style.setAttribute('w:styleId', styleId);
      const nameEl = xmlDoc.createElement('w:name');
      nameEl.setAttribute('w:val', name);
      style.appendChild(nameEl);
      stylesEl.appendChild(style);
    }
    const prepared = new Uint8Array(await doc.save());

    const root = stylesRoot(await runCommentEdit(prepared));

    expect(styleById(root, 'CommentReference')).toBeNull();
    expect(styleById(root, 'CommentText')).toBeNull();
  });

  it('creates word/styles.xml, its override and its relationship when absent', async () => {
    const files = unzipSync(fixtureBytes('base.docx'));
    delete files['word/styles.xml'];
    files['[Content_Types].xml'] = strToU8(
      strFromU8(files['[Content_Types].xml']).replace(
        /<Override[^>]*PartName="\/word\/styles\.xml"[^>]*\/>/,
        '',
      ),
    );
    files['word/_rels/document.xml.rels'] = strToU8(
      strFromU8(files['word/_rels/document.xml.rels']).replace(
        /<Relationship[^>]*Target="styles\.xml"[^>]*\/>/,
        '',
      ),
    );

    const saved = await runCommentEdit(zipSync(files));

    expect(Object.keys(unzipSync(saved))).toContain('word/styles.xml');
    const contentTypes = partText(saved, '[Content_Types].xml');
    expect(
      contentTypes.match(/PartName="\/word\/styles\.xml"/g) ?? [],
      'exactly one [Content_Types].xml override for the styles part',
    ).toHaveLength(1);
    expect(contentTypes).toContain(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml',
    );
    const rels = partText(saved, 'word/_rels/document.xml.rels');
    expect(
      rels.match(/Target="styles\.xml"/g) ?? [],
      'exactly one styles relationship',
    ).toHaveLength(1);

    const root = stylesRoot(saved);
    expect(styleById(root, 'CommentReference')).not.toBeNull();
    expect(styleById(root, 'CommentText')).not.toBeNull();
    // A part Node just created defines nothing, so there is no base style to
    // point at and w:basedOn is correctly omitted from BOTH styles. This is the
    // one place the two engines differ (Task 3 Parity note): python-docx
    // materialises its default template, which DOES define Normal and
    // DefaultParagraphFont. Neither engine leaves a dangling reference.
    expect(findChild(styleById(root, 'CommentReference')!, 'w:basedOn')).toBeNull();
    expect(findChild(styleById(root, 'CommentText')!, 'w:basedOn')).toBeNull();
  });

  it('invalidates the package style cache after injecting', async () => {
    const doc = await loadFixtureDoc('base.docx');
    const pkg = doc.pkg as any;

    const [stale] = _get_style_cache(doc.part);
    expect(stale['CommentReference']).toBeUndefined();
    expect(pkg._adeu_style_cache).toBeDefined();

    new CommentsManager(doc).addComment('Adeu AI', 'Check this figure.');

    expect(
      pkg._adeu_style_cache,
      "the projection's style cache was built from the styles.xml we just changed",
    ).toBeUndefined();
    const [fresh] = _get_style_cache(doc.part);
    expect(fresh['CommentReference'].name).toBe('annotation reference');
    expect(fresh['CommentText'].name).toBe('annotation text');
  });
});
