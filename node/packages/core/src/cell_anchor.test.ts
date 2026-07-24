import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
  addNestedTable,
} from "./test-utils.js";
import { resolve_cell_anchor } from "./docx/cell-anchor.js";
import { _extractTextFromDoc } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { DocumentObject } from "./docx/bridge.js";

/**
 * Verbatim port of the HISTORICAL fallback-id algorithm (the whole-document
 * rescan) — the cached implementation in cell-anchor.ts must be observably
 * indistinguishable from this.
 */
function referenceAnchor(
  cell_element: Element,
  is_empty: boolean,
): string | null {
  let firstP = cell_element.getElementsByTagName("w:p")[0] as
    | Element
    | undefined;
  let paraId = firstP ? firstP.getAttribute("w14:paraId") : null;
  if (!paraId && is_empty) {
    if (!firstP) {
      const xmlDoc = cell_element.ownerDocument!;
      firstP = xmlDoc.createElement("w:p");
      cell_element.appendChild(firstP);
    }
    const allPs = Array.from(
      cell_element.ownerDocument!.getElementsByTagName("w:p"),
    );
    const index = allPs.indexOf(firstP);
    let hash = 2166136261;
    const str = `fallback-paraId-${index}`;
    for (let i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }
    paraId = (hash >>> 0).toString(16).toUpperCase().padStart(8, "0");
    firstP.setAttribute("w14:paraId", paraId);
  }
  return paraId;
}

function cellsOf(table: Element): Element[] {
  return Array.from(table.getElementsByTagName("w:tc")) as Element[];
}

/** Builds the same document shape twice so reference and cached
 * implementations each run on their own pristine instance. */
async function buildDoc() {
  const doc = await createTestDocument();
  addParagraph(doc, "Intro paragraph.");
  const t1 = addTable(doc, 3, 2); // all cells start with one empty w:p
  setCellText(t1, 0, 0, "Filled A1");
  // (0,1) empty w:p — fallback
  // (1,0) empty w:p — fallback
  const t1cells = cellsOf(t1);
  // (1,1): remove the w:p entirely -> creation path
  const c11 = t1cells[3];
  while (c11.firstChild) c11.removeChild(c11.firstChild);
  // (2,0): nested table with its own empty cells
  addNestedTable(t1cells[4], 1, 2);
  addParagraph(doc, "Between tables.");
  const t2 = addTable(doc, 1, 2);
  setCellText(t2, 0, 1, "Filled tail");
  return { doc, t1, t2 };
}

describe("resolve_cell_anchor cache equivalence", () => {
  it("matches the historical rescan implementation cell-for-cell", async () => {
    const a = await buildDoc();
    const b = await buildDoc();

    const aCells = [...cellsOf(a.t1), ...cellsOf(a.t2)];
    const bCells = [...cellsOf(b.t1), ...cellsOf(b.t2)];
    expect(aCells.length).toBe(bCells.length);

    for (let i = 0; i < aCells.length; i++) {
      const cellText = (aCells[i].textContent || "").trim();
      const isEmpty = cellText === "";
      const ref = referenceAnchor(aCells[i], isEmpty);
      const got = resolve_cell_anchor(bCells[i], isEmpty).paraId;
      expect(got, `cell ${i}`).toBe(ref);
    }

    // The stamped DOMs must agree too (same attributes persisted).
    for (let i = 0; i < aCells.length; i++) {
      const aP = aCells[i].getElementsByTagName("w:p")[0] as
        | Element
        | undefined;
      const bP = bCells[i].getElementsByTagName("w:p")[0] as
        | Element
        | undefined;
      expect(bP?.getAttribute("w14:paraId") ?? null).toBe(
        aP?.getAttribute("w14:paraId") ?? null,
      );
    }
  });

  it("invalidates on foreign DOM mutation between resolutions", async () => {
    const { doc, t1 } = await buildDoc();
    const cells = cellsOf(t1);

    // First resolution builds the cache and stamps cell (0,1).
    const first = resolve_cell_anchor(cells[1], true).paraId;
    expect(first).toBeTruthy();

    // Foreign mutation: a new paragraph inserted BEFORE the table shifts the
    // document-order index of every table paragraph that is still unstamped.
    const body = doc.element;
    const xmlDoc = body.ownerDocument!;
    const newP = xmlDoc.createElement("w:p");
    body.insertBefore(newP, body.firstChild);

    // Cell (1,0) resolves AFTER the mutation: a stale cache would hand back
    // the pre-mutation index. The reference recomputes from scratch on an
    // identically mutated twin.
    const twin = await buildDoc();
    resolve_cell_anchor(cellsOf(twin.t1)[1], true); // mirror first stamp
    const twinBody = twin.doc.element;
    const twinNewP = twinBody.ownerDocument!.createElement("w:p");
    twinBody.insertBefore(twinNewP, twinBody.firstChild);
    const expected = referenceAnchor(cellsOf(twin.t1)[2], true);

    const got = resolve_cell_anchor(cells[2], true).paraId;
    expect(got).toBe(expected);
  });

  it("repeat resolution of the same cell returns the stamped id", async () => {
    const { t1 } = await buildDoc();
    const cell = cellsOf(t1)[1];
    const first = resolve_cell_anchor(cell, true).paraId;
    const second = resolve_cell_anchor(cell, true).paraId;
    expect(second).toBe(first);
  });

  it("ingest and mapper twins agree on a fallback-heavy document", async () => {
    const { doc: docA } = await buildDoc();
    const ingestText = _extractTextFromDoc(docA, false, false) as string;

    const { doc: docB } = await buildDoc();
    const mapper = new DocumentMapper(docB);

    expect(mapper.full_text).toBe(ingestText);
    expect(ingestText).toContain("{#cell:");
  });

  it("anchors survive a save/reload round-trip identically", async () => {
    const { doc } = await buildDoc();
    const text1 = _extractTextFromDoc(doc, false, false) as string;
    const saved = await doc.save();
    const reloaded = await DocumentObject.load(saved);
    const text2 = _extractTextFromDoc(reloaded, false, false) as string;
    expect(text2).toBe(text1);
  });
});
