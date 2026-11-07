"""
Restores ECMA-376 child order inside property containers (w:tblPr, w:pPr,
w:rPr) of the WordprocessingML parts.

Opt-in only: ordinary saves are surgical (AI_CONTEXT.md §2) and leave a
document's existing element order alone. `adeu.compare` applies it because its
output is a new document; an out-of-order element inherited from the original
(compare-v1-v2's v1 lists `w:tblBorders` after `w:tblLook` in every table)
would otherwise count as a new validation error against the revised version.

The sort is stable and only moves children whose tag is in the container's
sequence; an unknown child (extension, mc:AlternateContent) keeps its position
relative to the next known sibling. Node twin: node/packages/core/src/schema-order.ts.
"""

from typing import Dict, List

from docx.oxml.ns import qn

# CT_TblPr / CT_TblPrBase sequence.
TBL_PR_ORDER = [
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize",
    "w:tblStyleColBandSize", "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders",
    "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription",
    "w:tblPrChange",
]  # fmt: skip

# CT_PPr / CT_PPrBase sequence.
P_PR_ORDER = [
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl",
    "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens",
    "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
    "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
]  # fmt: skip

# CT_RPr / CT_ParaRPr sequence (revision marks first, then EG_RPrBase).
R_PR_ORDER = [
    "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i",
    "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike", "w:outline", "w:shadow", "w:emboss",
    "w:imprint", "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing",
    "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect", "w:bdr",
    "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
    "w:specVanish", "w:oMath", "w:rPrChange",
]  # fmt: skip

_ORDERS: Dict[str, Dict[str, int]] = {
    qn(container): {qn(tag): rank for rank, tag in enumerate(order)}
    for container, order in (("w:tblPr", TBL_PR_ORDER), ("w:pPr", P_PR_ORDER), ("w:rPr", R_PR_ORDER))
}


def _reorder(container, ranks: Dict[str, int]) -> bool:
    children = list(container)
    known = [c for c in children if isinstance(c.tag, str) and c.tag in ranks]
    ordered = sorted(known, key=lambda c: ranks[c.tag])
    if ordered == known:
        return False
    slots: List[int] = [i for i, c in enumerate(children) if isinstance(c.tag, str) and c.tag in ranks]
    for slot, child in zip(slots, ordered, strict=True):
        children[slot] = child
    for child in children:
        container.remove(child)
    for child in children:
        container.append(child)
    return True


def repair_schema_order(root) -> int:
    """Reorders out-of-sequence children of every tblPr/pPr/rPr under `root`; returns how many containers moved."""
    moved = 0
    for container_tag, ranks in _ORDERS.items():
        for container in root.iter(container_tag):
            if _reorder(container, ranks):
                moved += 1
    return moved
