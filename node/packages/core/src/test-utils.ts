import { existsSync, readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { unzipSync, zipSync, strFromU8, strToU8 } from 'fflate';
import { DocumentObject } from './docx/bridge.js';
import { parseFastXml } from './docx/fast-xml.js';
import { isWordReadableLongHexNumber } from './docx/long-hex-number.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Loads a pristine empty DOCX fixture and clears its body to allow
 * dynamic document construction in tests, mimicking `python-docx`.
 */
export async function createTestDocument(): Promise<DocumentObject> {
  const fixturePath = resolve(__dirname, '../../../../shared/fixtures/initial.docx');
  const buf = readFileSync(fixturePath);
  const doc = await DocumentObject.load(buf);
  
  // Clear the body completely
  const body = doc.element;
  while (body.firstChild) {
    body.removeChild(body.firstChild);
  }
  return doc;
}

export function addParagraph(doc: DocumentObject, text: string): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement('w:p');
  const r = xmlDoc.createElement('w:r');
  const t = xmlDoc.createElement('w:t');
  
  t.textContent = text;
  if (text.includes(' ') || text.includes('\n')) {
    t.setAttribute('xml:space', 'preserve');
  }
  
  r.appendChild(t);
  p.appendChild(r);
  doc.element.appendChild(p);
  return p;
}

export function addTable(doc: DocumentObject, rows: number, cols: number): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const tbl = xmlDoc.createElement('w:tbl');
  
  // Add tblGrid
  const tblGrid = xmlDoc.createElement('w:tblGrid');
  for (let i = 0; i < cols; i++) {
    const gridCol = xmlDoc.createElement('w:gridCol');
    tblGrid.appendChild(gridCol);
  }
  tbl.appendChild(tblGrid);

  for (let r = 0; r < rows; r++) {
    const tr = xmlDoc.createElement('w:tr');
    for (let c = 0; c < cols; c++) {
      const tc = xmlDoc.createElement('w:tc');
      const p = xmlDoc.createElement('w:p');
      tc.appendChild(p);
      tr.appendChild(tc);
    }
    tbl.appendChild(tr);
  }
  
  doc.element.appendChild(tbl);
  return tbl;
}

export function setCellText(table: Element, rowIndex: number, colIndex: number, text: string) {
  const rows = Array.from(table.childNodes).filter(n => (n as Element).tagName === 'w:tr') as Element[];
  const row = rows[rowIndex];
  const cells = Array.from(row.childNodes).filter(n => (n as Element).tagName === 'w:tc') as Element[];
  const cell = cells[colIndex];
  
  // Clear existing cell content
  while (cell.firstChild) cell.removeChild(cell.firstChild);
  
  const xmlDoc = table.ownerDocument!;
  const p = xmlDoc.createElement('w:p');
  const r = xmlDoc.createElement('w:r');
  const t = xmlDoc.createElement('w:t');
  
  t.textContent = text;
  if (text.includes(' ')) t.setAttribute('xml:space', 'preserve');
  
  r.appendChild(t);
  p.appendChild(r);
  cell.appendChild(p);
}

export function addNestedTable(cell: Element, rows: number, cols: number): Element {
  const xmlDoc = cell.ownerDocument!;
  const tbl = xmlDoc.createElement('w:tbl');
  
  const tblGrid = xmlDoc.createElement('w:tblGrid');
  for (let i = 0; i < cols; i++) {
    tblGrid.appendChild(xmlDoc.createElement('w:gridCol'));
  }
  tbl.appendChild(tblGrid);

  for (let r = 0; r < rows; r++) {
    const tr = xmlDoc.createElement('w:tr');
    for (let c = 0; c < cols; c++) {
      const tc = xmlDoc.createElement('w:tc');
      const p = xmlDoc.createElement('w:p');
      tc.appendChild(p);
      tr.appendChild(tc);
    }
    tbl.appendChild(tr);
  }
  
  // A table inside a cell must be followed by an empty paragraph in OOXML
  cell.appendChild(tbl);
  cell.appendChild(xmlDoc.createElement('w:p'));
  
  return tbl;
}

export function mergeCells(table: Element, rowIndex: number, colIndex1: number, colIndex2: number) {
  const rows = Array.from(table.childNodes).filter(n => (n as Element).tagName === 'w:tr') as Element[];
  const row = rows[rowIndex];
  const cells = Array.from(row.childNodes).filter(n => (n as Element).tagName === 'w:tc') as Element[];
  
  const xmlDoc = table.ownerDocument!;
  const tc1 = cells[colIndex1];
  
  let tcPr1 = Array.from(tc1.childNodes).find(n => (n as Element).tagName === 'w:tcPr') as Element;
  if (!tcPr1) {
    tcPr1 = xmlDoc.createElement('w:tcPr');
    tc1.insertBefore(tcPr1, tc1.firstChild);
  }
  
  const gridSpan = xmlDoc.createElement('w:gridSpan');
  gridSpan.setAttribute('w:val', (colIndex2 - colIndex1 + 1).toString());
  tcPr1.appendChild(gridSpan);
  
  // Physically remove the absorbed cells (this is how raw OOXML handles gridSpans)
  for (let i = colIndex1 + 1; i <= colIndex2; i++) {
    row.removeChild(cells[i]);
  }
}
// ---------------------------------------------------------------------------
// ST_LongHexNumber auditing (BUG_paraId_signed_int32_thread_collapse.md)
// ---------------------------------------------------------------------------

/**
 * Every attribute in the WordprocessingML schemas typed `ST_LongHexNumber`
 * that Adeu can end up writing. Word parses ALL of them as SIGNED 32-bit
 * integers, so a value outside (0x00000000, 0x80000000) is discarded and
 * regenerated on load � taking every reference to it with it, and renumbering
 * the rest of the part for good measure.
 *
 * Mirrors `LONG_HEX_NUMBER_ATTRIBUTES` in python/tests/utils.py.
 */
export const LONG_HEX_NUMBER_ATTRIBUTES = [
  "w14:paraId",
  "w14:textId",
  "w15:paraId",
  "w15:paraIdParent",
  "w16cid:paraId",
  "w16cid:durableId",
  "w16cex:durableId",
  "w:rsidR",
  "w:rsidRPr",
  "w:rsidRDefault",
  "w:rsidP",
  "w:rsidDel",
  "w:rsidSect",
  "w:rsidTr",
  "w:rsidRoot",
];

const LONG_HEX_ATTR_RE = new RegExp(
  `\\b(${LONG_HEX_NUMBER_ATTRIBUTES.join("|")})="([0-9A-Fa-f]{1,8})"`,
  "g",
);
// <w:rsid w:val="00FC693F"/> and <w:rsidRoot w:val="..."/> inside <w:rsids>.
const RSIDS_ELEMENT_RE = /<w:(rsid|rsidRoot)\b[^>]*\bw:val="([0-9A-Fa-f]{1,8})"/g;

/**
 * Every `ST_LongHexNumber` in a saved DOCX that Word will refuse to keep, as
 * `[part, attribute, value]`.
 *
 * The general guard for the whole bug class: `w16cid:durableId` (2026-08-11
 * B3), `w14:paraId` (2026-08-12 B5) and anything minted next are all caught by
 * it, because it does not care which attribute is "the special one" � which is
 * exactly how the first two got through. The range predicate is the one the
 * ENGINE mints against, so the guard cannot drift away from the generator;
 * that the predicate matches ECMA-376 is pinned separately, from literals, in
 * repro.para-id-signed-int32.test.ts.
 */
export function findOutOfRangeLongHexNumbers(
  pkg: Buffer,
): [string, string, string][] {
  const offenders: [string, string, string][] = [];
  const unzipped = unzipSync(new Uint8Array(pkg));
  for (const [name, bytes] of Object.entries(unzipped)) {
    if (!name.endsWith(".xml")) continue;
    const xml = strFromU8(bytes);
    for (const [, attr, value] of xml.matchAll(LONG_HEX_ATTR_RE)) {
      if (!isWordReadableLongHexNumber(value)) offenders.push([name, attr, value]);
    }
    for (const [, tag, value] of xml.matchAll(RSIDS_ELEMENT_RE)) {
      if (!isWordReadableLongHexNumber(value)) {
        offenders.push([name, `w:${tag}/@w:val`, value]);
      }
    }
  }
  return offenders;
}

// ---------------------------------------------------------------------------
// The two paraId rules that are not about range ([MS-DOCX] 2.6.2.4 / 2.6.2.6)
// ---------------------------------------------------------------------------

/** `<w:p ...>` / `<w15:commentEx ...>` - any start tag, captured whole so the
 *  attributes on ONE element can be examined together. */
const ELEMENT_RE = /<[A-Za-z][-\w:.]*\b[^>]*\/?>/g;

/**
 * Every `w14:paraId` used more than once within a single part.
 *
 * [MS-DOCX] 2.6.2.4: the value "specifies an identifier for a paragraph that is
 * unique within the document part". Stated in prose, not in the schema, so a
 * duplicate validates exactly like the out-of-range values do.
 *
 * Scoped PER PART because that is the scope the specification gives: the same
 * paraId in document.xml and in comments.xml is legal and common.
 *
 * Mirrors `find_duplicate_para_ids` in python/tests/utils.py.
 */
export function findDuplicateParaIds(pkg: Buffer): [string, string, string][] {
  const duplicates: [string, string, string][] = [];
  const unzipped = unzipSync(new Uint8Array(pkg));
  for (const [name, bytes] of Object.entries(unzipped)) {
    if (!name.endsWith(".xml")) continue;
    const seen = new Map<string, number>();
    for (const [, value] of strFromU8(bytes).matchAll(
      /w14:paraId="([0-9A-Fa-f]{1,8})"/g,
    )) {
      const key = value.toUpperCase();
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    for (const [value, count] of seen) {
      if (count > 1) duplicates.push([name, "w14:paraId", value]);
    }
  }
  return duplicates;
}

/**
 * Every element carrying `w14:textId` but no `w14:paraId`.
 *
 * [MS-DOCX] 2.6.2.6: "Any element having this attribute MUST also have the
 * paraId attribute." textId is a version stamp for the paragraph's TEXT and is
 * meaningless without the identity it versions, so Word treats the pair as one
 * unit - which makes it a constraint on any pass that rewrites ids.
 *
 * Mirrors `find_text_ids_without_para_id` in python/tests/utils.py.
 */
export function findTextIdsWithoutParaId(pkg: Buffer): [string, string][] {
  const orphans: [string, string][] = [];
  const unzipped = unzipSync(new Uint8Array(pkg));
  for (const [name, bytes] of Object.entries(unzipped)) {
    if (!name.endsWith(".xml")) continue;
    for (const [tag] of strFromU8(bytes).matchAll(ELEMENT_RE)) {
      if (tag.includes("w14:textId=") && !tag.includes("w14:paraId=")) {
        orphans.push([name, tag]);
      }
    }
  }
  return orphans;
}

/** The failure message that tells the next reader what actually broke. */
export function outOfRangeIdReport(
  offenders: [string, string, string][],
  context: string,
): string {
  return (
    `${context}: the saved package carries ${offenders.length} ST_LongHexNumber value(s) ` +
    `outside (0x00000000, 0x80000000): ${JSON.stringify(offenders.slice(0, 8))}. Word parses ` +
    `these as signed 32-bit integers, discards them on load and regenerates the whole part's ` +
    `ids � collapsing comment threads and invalidating every {#cell:paraId} anchor. See ` +
    `BUG_paraId_signed_int32_thread_collapse.md.`
  );
}

// ---------------------------------------------------------------------------
// Real-document corpus (specs/content-controls/spec-corpus.md, task CC-3)
// ---------------------------------------------------------------------------

/**
 * Manifest of the fetch-on-demand corpus. The documents are real government
 * files that are deliberately NOT committed, so every corpus test has to
 * tolerate their absence: CI runs green without ever downloading one.
 *
 * Twin of `corpus_path()` / `corpus_dir()` in python/tests/utils.py. The two
 * engines must resolve the same file for the same key, or an "identical
 * counts" parity assertion is comparing two different documents.
 */
const CORPUS_MANIFEST = resolve(__dirname, '../../../../shared/corpus/manifest.json');

let manifestCache: Record<string, { file: string }> | undefined;

function corpusManifest(): Record<string, { file: string }> {
  if (!manifestCache) {
    manifestCache = JSON.parse(readFileSync(CORPUS_MANIFEST, 'utf-8')).documents;
  }
  return manifestCache!;
}

/** Where corpus documents live. `ADEU_CORPUS_DIR` relocates it. */
export function corpusDir(): string {
  return process.env.ADEU_CORPUS_DIR ?? dirname(CORPUS_MANIFEST);
}

/**
 * Path to corpus document `key`, or `null` when it is not on disk.
 *
 * Returns null rather than skipping so the caller can decide: vitest's skip is
 * only reachable from inside a test body (`ctx.skip()`), while the decision to
 * emit a test at all often has to happen at collection time.
 *
 * An **unknown key** throws instead of returning null. The two are not the same
 * failure: an absent document is normal (fetch-on-demand), but a typo'd key
 * that quietly returned null would make the test vacuously green forever.
 *
 * Never downloads — a test that fetched would depend on government web servers.
 */
export function corpusPath(key: string): string | null {
  const documents = corpusManifest();
  if (!(key in documents)) {
    throw new Error(
      `unknown corpus key '${key}'; manifest defines: ${Object.keys(documents).sort().join(', ')}`,
    );
  }
  const path = resolve(corpusDir(), documents[key].file);
  return existsSync(path) ? path : null;
}

/** The message a skipped corpus test should carry: it names the fix. */
export function corpusSkipReason(key: string): string {
  return `corpus document '${key}' absent - run \`python scripts/fetch_corpus.py --only ${key}\``;
}

/** Bytes of corpus document `key`, or null when absent. */
export function corpusBuffer(key: string): Buffer | null {
  const path = corpusPath(key);
  return path ? readFileSync(path) : null;
}

const W_NS_URI = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const REL_BASE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const CT_BASE = 'application/vnd.openxmlformats-officedocument.wordprocessingml';

/**
 * Attaches a header or footer the way Word does — and the way python-docx's
 * `section.header` does, which is what the python test builders use.
 *
 * Three things must exist, not one: the part, a relationship from
 * word/document.xml, and a `w:headerReference`/`w:footerReference` inside the
 * body's `w:sectPr`. Dropping an orphan part into the package is not enough:
 * `iter_document_parts_with_kind` walks section references (mirroring python),
 * so an unreferenced part is invisible — exactly as it is invisible in Word.
 *
 * @param type "default" | "first" | "even". "first" additionally sets
 *             `w:titlePg` on the section, and "even" requires
 *             `w:evenAndOddHeaders` in settings.xml, since the engine honours
 *             both toggles.
 */
export function attachHeaderFooter(
  doc: DocumentObject,
  kind: 'header' | 'footer',
  innerXml: string,
  opts: { type?: 'default' | 'first' | 'even'; path?: string } = {},
): any {
  const type = opts.type ?? 'default';
  const root = kind === 'header' ? 'w:hdr' : 'w:ftr';
  const n = doc.pkg.parts.filter((p: any) =>
    p.partname.startsWith(`word/${kind}`) || p.partname.startsWith(`/word/${kind}`),
  ).length + 1;
  const path = opts.path ?? `/word/${kind}${n}.xml`;

  const part = doc.pkg.addPart(
    path,
    `${CT_BASE}.${kind}+xml`,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<${root} xmlns:w="${W_NS_URI}">${innerXml}</${root}>`,
  );

  const rId = doc.relateTo(part, `${REL_BASE}/${kind}`);

  const body = doc.element;
  const owner = body.ownerDocument!;
  let sectPr = null as any;
  for (let i = body.childNodes.length - 1; i >= 0; i--) {
    const c = body.childNodes[i] as any;
    if (c.nodeType === 1 && c.tagName === 'w:sectPr') {
      sectPr = c;
      break;
    }
  }
  if (!sectPr) {
    sectPr = owner.createElement('w:sectPr');
    body.appendChild(sectPr);
  }

  const ref = owner.createElement(`w:${kind}Reference`);
  ref.setAttribute('w:type', type);
  ref.setAttribute('r:id', rId);
  // References must precede the geometry elements inside w:sectPr.
  sectPr.insertBefore(ref, sectPr.firstChild ?? null);

  if (type === 'first' && !findChildTag(sectPr, 'w:titlePg')) {
    sectPr.appendChild(owner.createElement('w:titlePg'));
  }
  if (type === 'even') enableEvenAndOddHeaders(doc);

  return part;
}

function findChildTag(el: any, tag: string): any {
  for (let i = 0; i < el.childNodes.length; i++) {
    const c = el.childNodes[i];
    if (c.nodeType === 1 && c.tagName === tag) return c;
  }
  return null;
}

/** Sets `w:evenAndOddHeaders` in word/settings.xml, creating the part if absent. */
export function enableEvenAndOddHeaders(doc: DocumentObject): void {
  let settings = doc.pkg.getPartByPath('word/settings.xml');
  if (!settings) {
    settings = doc.pkg.addPart(
      '/word/settings.xml',
      `${CT_BASE}.settings+xml`,
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="${W_NS_URI}"/>`,
    );
    doc.relateTo(settings, `${REL_BASE}/settings`);
  }
  if (!findChildTag(settings._element, 'w:evenAndOddHeaders')) {
    settings._element.appendChild(
      settings._element.ownerDocument!.createElement('w:evenAndOddHeaders'),
    );
  }
}

// ---------------------------------------------------------------------------
// Content-control fixture (CC-1)
// ---------------------------------------------------------------------------

/**
 * The normative 16-control fixture body.
 *
 * The XML lives in ONE place — `shared/fixtures/cc_fixture.body.xml` — read by
 * `scripts/make_cc_fixture.py`, by python's `tests/cc_fixture.py`, and here. It
 * is deliberately NOT transcribed into either engine's tests: hand-copied OOXML
 * is precisely how the two engines drift apart (PROGRESS.md 2026-08-21, the
 * duplicated table XML in the two `repro_sdt_table_row_cell_invisibility`
 * suites).
 *
 * Canonical listing and normative goldens:
 * `specs/content-controls/acceptance/fixture-standard.md`.
 */
const CC_FIXTURE_BODY = resolve(
  __dirname,
  '../../../../shared/fixtures/cc_fixture.body.xml',
);

const CC_FIXTURE_HEADER =
  '<w:document xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" ' +
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" ' +
  'mc:Ignorable="w14 w15"><w:body>';
const CC_FIXTURE_FOOTER = '<w:sectPr/></w:body></w:document>';

export function ccFixtureBodyXml(): string {
  if (!existsSync(CC_FIXTURE_BODY)) {
    throw new Error(`shared content-control fixture missing: ${CC_FIXTURE_BODY}`);
  }
  // Normalise EOLs before trimming: python reads this same file through
  // `Path.read_text()`, which translates CRLF to LF, so on a Windows checkout
  // the two engines would otherwise build their fixture from DIFFERENT bytes.
  // Today the file is one line and `trim()` hides it; the day someone
  // reformats it to multi-line, that becomes a silent cross-engine parity
  // divergence visible only on Windows. Cheaper to close now than to diagnose.
  return readFileSync(CC_FIXTURE_BODY, 'utf-8').replace(/\r\n/g, '\n').trim();
}

export function ccFixtureDocumentXml(): string {
  return CC_FIXTURE_HEADER + ccFixtureBodyXml() + CC_FIXTURE_FOOTER;
}

/**
 * The parsed `w:body` element — enough for classification tests.
 *
 * Returns a FRESH tree per call: the ordinal-stability test needs two
 * independent loads, and a cached element would make it assert nothing.
 */
export function ccFixtureBodyElement(): any {
  const doc = parseFastXml(ccFixtureDocumentXml());
  return findChildTag(doc.documentElement, 'w:body');
}

/**
 * A complete minimal package for the content-control fixture.
 *
 * Mirrors `python/tests/cc_fixture.py::cc_fixture_bytes` and
 * `scripts/make_cc_fixture.py` part-for-part, so the two engines are handed
 * byte-comparable input. `protection` selects the `cc_fixture_forms` variant.
 *
 * `bodyXml` swaps the 16-control body for a synthetic one, which is how the
 * node twin of `python/tests/sdt_fixtures.py::build_sdt_docx` is spelled: the
 * checkbox suite needs shapes the shared fixture deliberately does not carry
 * (a control whose `w14:checked` contradicts its glyph, a control with no
 * glyph run at all). The package around it stays identical, so a synthetic
 * body is exercised through exactly the same load path as the real fixture.
 */
export function ccFixtureBytes(
  protection?: 'forms' | 'readOnly' | 'comments' | 'trackedChanges',
  bodyXml?: string,
  // `null` omits `w:enforcement` entirely, which is a real Word shape and NOT
  // the same as '0': the OOXML boolean rule makes an absent attribute mean
  // true. CC-4's protection reader is tested against all three states.
  enforcement: string | null = '1',
): Uint8Array {
  const w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
  const enforcementAttr = enforcement === null ? '' : ` w:enforcement="${enforcement}"`;
  const prot = protection
    ? `<w:documentProtection w:edit="${protection}"${enforcementAttr}/>`
    : '';
  const decl = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
  const files: Record<string, Uint8Array> = {
    '[Content_Types].xml': strToU8(
      decl +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/word/document.xml" ContentType="' +
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
        '<Override PartName="/word/settings.xml" ContentType="' +
        'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>' +
        '</Types>',
    ),
    '_rels/.rels': strToU8(
      decl +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        '<Relationship Id="rId1" Type="' +
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"' +
        ' Target="word/document.xml"/></Relationships>',
    ),
    'word/document.xml': strToU8(
      decl +
        '\n' +
        (bodyXml === undefined
          ? ccFixtureDocumentXml()
          : CC_FIXTURE_HEADER + bodyXml + CC_FIXTURE_FOOTER),
    ),
    'word/_rels/document.xml.rels': strToU8(
      decl +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        '<Relationship Id="rId1" Type="' +
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"' +
        ' Target="settings.xml"/></Relationships>',
    ),
    'word/settings.xml': strToU8(decl + `<w:settings ${w}>${prot}</w:settings>`),
  };
  return zipSync(files);
}

/**
 * Extract a fenced golden block from the frozen acceptance fixture document.
 *
 * `fixture-standard.md` is normative down to its spacing, so the goldens are
 * read from it rather than copied into tests, where a copy would drift.
 *
 * Normalising CRLF is load-bearing, not cosmetic: git's autocrlf can hand
 * `readFileSync` CRLF, the fence in `\n```\n` then never matches, `exec`
 * returns null, and the golden tests die in `m![1]` with a bare TypeError that
 * looks like a projection bug and is not one. The python twin has no such
 * problem because `Path.read_text()` does universal-newline translation.
 */
export function ccGolden(section: string): string {
  const md = readFileSync(
    resolve(
      __dirname,
      "../../../../specs/content-controls/acceptance/fixture-standard.md",
    ),
    "utf-8",
  ).replace(/\r\n/g, "\n");
  const m = new RegExp(
    `## ${section}[\\s\\S]*?\\n\`\`\`\\n([\\s\\S]*?)\`\`\``,
  ).exec(md);
  if (!m) throw new Error(`golden section not found: ${section}`);
  return m[1].replace(/\n+$/, "");
}
