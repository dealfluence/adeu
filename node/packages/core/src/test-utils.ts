import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DocumentObject } from './docx/bridge.js';

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