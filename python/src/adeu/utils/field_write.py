"""CC-5 — the XML half of `set_field` (spec-set-field.md §4/§5).

The *tracked* half of a fill is not here: `set_field` desugars into an ordinary
`ModifyText` so it inherits atomicity, author resolution, affix trimming,
comment wrapping and the write gates rather than reimplementing them. What
lives here is everything a `ModifyText` cannot express — the untracked
placeholder teardown Word performs, and the attribute syncs (`w:date/@w:fullDate`,
`w14:checked`, `w:dropDownList/@w:lastValue`) that Word writes with no revision
of their own, the URL_RETARGET precedent.
"""

from typing import Any, List, Optional

from docx.oxml.ns import qn

from .content_controls import SdtInfo

#: `w14`, which python-docx does not register. Clark notation, as everywhere
#: else in this family (see utils/content_controls.py).
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def sdt_content(sdt: Any) -> Optional[Any]:
    """The `w:sdtContent` child, whatever the control's block level."""
    for child in sdt:
        if child.tag == qn("w:sdtContent"):
            return child
    return None


def sdt_pr(sdt: Any) -> Optional[Any]:
    for child in sdt:
        if child.tag == qn("w:sdtPr"):
            return child
    return None


def content_runs(sdt: Any) -> List[Any]:
    """Every `w:r` inside the control's content, in document order."""
    content = sdt_content(sdt)
    if content is None:
        return []
    return list(content.iter(qn("w:r")))


def clear_placeholder(info: SdtInfo) -> bool:
    """Take a control out of placeholder state the way Word does (§4.1-4.2).

    Untracked, and deliberately: CC-6(a) filled an empty control in Word and
    got exactly ONE revision, the insertion. The `w:showingPlcHdr` flag and the
    ghost run simply vanish. Emitting a `w:del` for the ghost would put words
    into the document the author never wrote — a reviewer would see "Click here
    to enter text" struck through as if it had been real content.

    Returns True when something changed.
    """
    sdt = info.element
    pr = sdt_pr(sdt)
    if pr is None:
        return False

    flag = pr.find(qn("w:showingPlcHdr"))
    if flag is None:
        return False
    pr.remove(flag)

    # The ghost run(s) go with it. Removing the flag alone would leave the
    # prompt text behind as real content, which is the one outcome worse than
    # not clearing at all: the placeholder would become the value.
    content = sdt_content(sdt)
    if content is not None:
        for run in list(content.iter(qn("w:r"))):
            parent = run.getparent()
            if parent is not None:
                parent.remove(run)
    return True


def placeholder_rpr(info: SdtInfo) -> Optional[Any]:
    """The `rPr` an inserted run should carry, per §4.3.

    Preference order is `sdtPr/w:rPr`, then the ghost run's own `rPr` MINUS
    `rStyle PlaceholderText`, then nothing (paragraph context wins by
    inheritance). The stripping is not optional: CC-6(a) shows Word's own fill
    carries no `rStyle PlaceholderText` at all, and leaving it on would render
    the value in grey placeholder styling — visually indistinguishable from the
    empty control the user just filled.
    """
    import copy

    pr = sdt_pr(info.element)
    if pr is not None:
        rpr = pr.find(qn("w:rPr"))
        if rpr is not None:
            return copy.deepcopy(rpr)

    for run in content_runs(info.element):
        rpr = run.find(qn("w:rPr"))
        if rpr is not None:
            clone = copy.deepcopy(rpr)
            for style in clone.findall(qn("w:rStyle")):
                if style.get(qn("w:val")) == "PlaceholderText":
                    clone.remove(style)
            return clone
    return None


def unwrap_sdt(info: SdtInfo) -> bool:
    """Dissolve the `w:sdt` shell, leaving its content in place (§4.4).

    For `w:temporary` controls, which Word unwraps on ANY content edit —
    tracked or untracked, placeholder or already filled (CC-6(c)). The
    revision outlives the wrapper, so this is one-way: rejecting the fill
    restores the old text but not the control.
    """
    sdt = info.element
    content = sdt_content(sdt)
    parent = sdt.getparent()
    if content is None or parent is None:
        return False
    index = list(parent).index(sdt)
    for child in reversed(list(content)):
        parent.insert(index, child)
    parent.remove(sdt)
    return True
