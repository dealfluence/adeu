/**
 * Restores ECMA-376 child order inside property containers (w:tblPr, w:pPr,
 * w:rPr). Opt-in only: ordinary saves are surgical (AI_CONTEXT.md §2).
 * `build_redline_comparison` applies it because its output is a new document;
 * an out-of-order element inherited from the original (compare-v1-v2's v1
 * lists `w:tblBorders` after `w:tblLook`) would otherwise count as a new
 * validation error against the revised version.
 *
 * Stable: only children whose tag is in the container's sequence move; unknown
 * children keep their slots. Python twin: python/src/adeu/utils/schema_order.py.
 */
import { findAllDescendants } from "./docx/dom.js";

// CT_TblPr / CT_TblPrBase sequence.
const TBL_PR_ORDER = [
  "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize",
  "w:tblStyleColBandSize", "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders",
  "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription",
  "w:tblPrChange",
];

// CT_PPr / CT_PPrBase sequence.
const P_PR_ORDER = [
  "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl",
  "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens",
  "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
  "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
  "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
  "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
];

// CT_RPr / CT_ParaRPr sequence (revision marks first, then EG_RPrBase).
const R_PR_ORDER = [
  "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i",
  "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike", "w:outline", "w:shadow", "w:emboss",
  "w:imprint", "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing",
  "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect", "w:bdr",
  "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
  "w:specVanish", "w:oMath", "w:rPrChange",
];

const ORDERS: [string, Map<string, number>][] = [
  ["w:tblPr", new Map(TBL_PR_ORDER.map((t, i) => [t, i]))],
  ["w:pPr", new Map(P_PR_ORDER.map((t, i) => [t, i]))],
  ["w:rPr", new Map(R_PR_ORDER.map((t, i) => [t, i]))],
];

function reorder(container: Element, ranks: Map<string, number>): boolean {
  const children = Array.from(container.childNodes);
  const isKnown = (n: Node) => n.nodeType === 1 && ranks.has((n as Element).tagName);
  const known = children.filter(isKnown) as Element[];
  const ordered = [...known].sort((a, b) => ranks.get(a.tagName)! - ranks.get(b.tagName)!);
  if (ordered.every((el, i) => el === known[i])) return false;
  let k = 0;
  const result = children.map((n) => (isKnown(n) ? ordered[k++] : n));
  for (const n of children) container.removeChild(n);
  for (const n of result) container.appendChild(n);
  return true;
}

/** Reorders out-of-sequence children of every tblPr/pPr/rPr under `root`; returns how many containers moved. */
export function repair_schema_order(root: Element): number {
  let moved = 0;
  for (const [tag, ranks] of ORDERS) {
    for (const container of findAllDescendants(root, tag)) {
      if (reorder(container, ranks)) moved++;
    }
  }
  return moved;
}
