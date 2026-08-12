import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
  addNestedTable,
} from "./test-utils.js";
import { resolve_cell_anchor } from "./docx/cell-anchor.js";
import { toLongHexNumber } from "./docx/long-hex-number.js";
import { FastNode, parseFastXml } from "./docx/fast-xml.js";
import { _extractTextFromDoc } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { DocumentObject } from "./docx/bridge.js";

/**
 * Verbatim port of the HISTORICAL fallback-id algorithm (the whole-document
 * rescan) — the cached implementation in cell-anchor.ts must be observably
 * indistinguishable from this.
 *
 * The final fold into the ST_LongHexNumber range is part of the derivation,
 * not part of the caching: the unmasked `(hash >>> 0)` this used to end with
 * put 95 of the first 128 paragraph indices in the high half, where Word
 * discards the id and renumbers the whole part
 * (BUG_paraId_signed_int32_thread_collapse.md). Range coverage is asserted in
 * repro.para-id-signed-int32.test.ts; this oracle only has to agree.
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
    paraId = toLongHexNumber(hash);
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

  // The wp-index walk must stay linear. fast-xml implements nextSibling as
  // parentNode.childNodes.indexOf(this), so driving a preorder walk from
  // sibling pointers is O(paragraphs^2) over a wide w:body — measured ~n^1.96,
  // extrapolating to ~35s per build on the 45MB document this index exists to
  // make fast. Asserting the traversal never touches those accessors pins the
  // mechanism deterministically, where a wall-clock budget would be flaky.
  it("builds the paragraph index without sibling-pointer traversal", () => {
    const paras = Array.from(
      { length: 3000 },
      (_, i) => `<w:p><w:r><w:t>p${i}</w:t></w:r></w:p>`,
    ).join("");
    const doc = parseFastXml(
      `<w:document xmlns:w="urn:w"><w:body>${paras}` +
        `<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl></w:body></w:document>`,
    ) as any;
    const cell = doc.getElementsByTagName("w:tc")[0];

    const proto = FastNode.prototype as any;
    const original = {
      next: Object.getOwnPropertyDescriptor(proto, "nextSibling")!,
      prev: Object.getOwnPropertyDescriptor(proto, "previousSibling")!,
    };
    let siblingReads = 0;
    try {
      for (const [key, desc] of [
        ["nextSibling", original.next],
        ["previousSibling", original.prev],
      ] as const) {
        Object.defineProperty(proto, key, {
          ...desc,
          get(this: any) {
            siblingReads++;
            return desc.get!.call(this);
          },
        });
      }
      const { paraId } = resolve_cell_anchor(cell, true);
      expect(paraId).toBeTruthy();
    } finally {
      Object.defineProperty(proto, "nextSibling", original.next);
      Object.defineProperty(proto, "previousSibling", original.prev);
    }

    expect(
      siblingReads,
      `resolve_cell_anchor made ${siblingReads} O(siblings) nextSibling/` +
        "previousSibling reads; the paragraph index must walk childNodes arrays",
    ).toBe(0);
  });
});
