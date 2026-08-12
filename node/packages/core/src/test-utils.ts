import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { unzipSync, strFromU8 } from 'fflate';
import { DocumentObject } from './docx/bridge.js';
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
