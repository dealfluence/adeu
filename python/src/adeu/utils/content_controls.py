"""Content-control (``w:sdt``) classification and ordinal assignment.

Twin of ``node/packages/core/src/utils/content-controls.ts`` — every rule here
must hold identically in both engines (Virtual Text contract).

Spec: ``specs/content-controls/spec-projection.md`` §1-§5.

This lives in its own module rather than in ``utils/docx.py`` for two reasons:
``utils/docx.py`` is the most contended file in the tree (both agents touch it),
and keeping the classification rules in one small pair of files makes the
python/node diff reviewable by eye — which is how the twins are kept honest.

Namespaces are spelled in Clark notation directly instead of going through
``docx.oxml.ns.qn``: python-docx does not register ``w15``, and the only thing
that currently registers it is an import side effect in
``adeu.redline.comments``. Depending on import order for correctness of a
projection rule is not acceptable, so ``mapper.py``'s explicit-constant
precedent is followed here.
"""

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Tuple

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _w14(tag: str) -> str:
    return f"{{{W14_NS}}}{tag}"


def _w15(tag: str) -> str:
    return f"{{{W15_NS}}}{tag}"


QN_W_SDT = _w("sdt")
QN_W_SDTPR = _w("sdtPr")
QN_W_SDTCONTENT = _w("sdtContent")
QN_W_VAL = _w("val")

# Classification probes, in the order spec-projection.md §1 lists them.
# FIRST MATCH WINS — the order is normative, not incidental: a checkbox also
# carries no w:text, and a repeating-section item nested in a group would
# otherwise classify as its container.
_CLASS_PROBES: Tuple[Tuple[str, str], ...] = (
    ("checkbox", _w14("checkbox")),
    ("dropdown", _w("dropDownList")),
    ("combobox", _w("comboBox")),
    ("date", _w("date")),
    ("picture", _w("picture")),
    ("building-block", _w("docPartObj")),
    ("building-block", _w("docPartList")),
    ("group", _w("group")),
    ("repeating", _w15("repeatingSection")),
    ("repeating-item", _w15("repeatingSectionItem")),
    ("text", _w("text")),
)

#: Classes that never carry inline ``{#cc:N}`` anchors (spec §1). They still
#: consume an ordinal (A1.3) and still appear in the ledger.
_UNANCHORED_CLASSES = frozenset({"checkbox", "picture", "building-block", "repeating", "repeating-item"})

#: Content-lock values that make a control's CONTENTS read-only. ``sdtLocked``
#: is deliberately absent: it forbids deleting the control but leaves the
#: contents editable, so it is a ledger detail and never an inline flag
#: (spec §2).
_CONTENT_LOCK_VALUES = frozenset({"sdtContentLocked", "contentLocked"})


@dataclass(frozen=True)
class SdtInfo:
    """Everything projection and the ledger need about one ``w:sdt``.

    Built once per control by :func:`classify_sdt` and consumed by ingest, the
    mapper and the fields ledger alike, so the three cannot disagree.
    """

    element: Any
    cls: str
    alias: Optional[str] = None
    tag: Optional[str] = None
    sdt_id: Optional[str] = None
    content_locked: bool = False
    delete_locked: bool = False
    bound: bool = False
    binding_xpath: Optional[str] = None
    showing_placeholder: bool = False
    options: Tuple[Tuple[str, str], ...] = ()
    checked: Optional[bool] = None
    date_format: Optional[str] = None
    has_nested_sdt: bool = False
    ordinal: int = 0
    flags: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def anchored(self) -> bool:
        """True when this control projects ``{#cc:N}`` / ``{#/cc:N}``.

        A rich-text control containing another control is NOT anchored: its
        contents project normally and it is ledger-only (spec §1), because
        anchoring it would nest anchor pairs and make the empty-pair edit
        surface ambiguous.
        """
        if self.cls in _UNANCHORED_CLASSES:
            return False
        if self.cls == "richtext" and self.has_nested_sdt:
            return False
        return True

    @property
    def open_token(self) -> str:
        flags = "".join(f" {f}" for f in self.flags)
        return f"{{#cc:{self.ordinal}{flags}}}"

    @property
    def close_token(self) -> str:
        return f"{{#/cc:{self.ordinal}}}"


def _first_child(parent: Any, qname: str) -> Any:
    if parent is None:
        return None
    return parent.find(qname)


def _val(element: Any) -> Optional[str]:
    """Read ``w:val``, falling back to ``w14:val``.

    The w14 elements (``w14:checked``, ``w14:checkedState``) carry their value
    in the w14 namespace, not w. Reading only ``w:val`` silently reports every
    checkbox as unchecked — which is worse than failing, because the projection
    would render a confident ``[ ]`` over a ticked box.
    """
    if element is None:
        return None
    value = element.get(QN_W_VAL)
    if value is None:
        value = element.get(_w14("val"))
    return value


def _is_true(value: Optional[str]) -> bool:
    """OOXML boolean: absent attribute means true for on/off toggles."""
    return value is None or value in ("1", "true", "on")


def classify_sdt(sdt_element: Any, ordinal: int = 0) -> SdtInfo:
    """Classify one ``w:sdt`` from its ``w:sdtPr``. Never mutates the element."""
    sdtPr = _first_child(sdt_element, QN_W_SDTPR)

    cls = "richtext"
    if sdtPr is not None:
        for name, qname in _CLASS_PROBES:
            if sdtPr.find(qname) is not None:
                cls = name
                break

    alias = _val(_first_child(sdtPr, _w("alias")))
    tag = _val(_first_child(sdtPr, _w("tag")))
    sdt_id = _val(_first_child(sdtPr, _w("id")))

    lock_val = _val(_first_child(sdtPr, _w("lock")))
    content_locked = lock_val in _CONTENT_LOCK_VALUES
    # sdtContentLocked implies the control cannot be deleted either.
    delete_locked = lock_val in ("sdtLocked", "sdtContentLocked")

    binding = _first_child(sdtPr, _w("dataBinding"))
    bound = binding is not None
    binding_xpath = binding.get(_w("xpath")) if binding is not None else None

    showing_placeholder = _first_child(sdtPr, _w("showingPlcHdr")) is not None

    options: Tuple[Tuple[str, str], ...] = ()
    if cls in ("dropdown", "combobox"):
        list_el = _first_child(sdtPr, _w("dropDownList") if cls == "dropdown" else _w("comboBox"))
        if list_el is not None:
            options = tuple(
                (
                    item.get(_w("displayText")) or item.get(_w("value")) or "",
                    item.get(_w("value")) or item.get(_w("displayText")) or "",
                )
                for item in list_el.findall(_w("listItem"))
            )

    checked: Optional[bool] = None
    if cls == "checkbox":
        cb = _first_child(sdtPr, _w14("checkbox"))
        checked = _val(_first_child(cb, _w14("checked"))) in ("1", "true")

    date_format: Optional[str] = None
    if cls == "date":
        date_el = _first_child(sdtPr, _w("date"))
        date_format = _val(_first_child(date_el, _w("dateFormat")))

    content = _first_child(sdt_element, QN_W_SDTCONTENT)
    has_nested_sdt = content is not None and content.find(f".//{QN_W_SDT}") is not None

    # Flag order is normative (spec §2): locked, bound, group. A group is an
    # inherently locked region, so it never also emits `locked`.
    flags = []
    if content_locked and cls != "group":
        flags.append("locked")
    if bound:
        flags.append("bound")
    if cls == "group":
        flags.append("group")

    return SdtInfo(
        element=sdt_element,
        cls=cls,
        alias=alias,
        tag=tag,
        sdt_id=sdt_id,
        content_locked=content_locked,
        delete_locked=delete_locked,
        bound=bound,
        binding_xpath=binding_xpath,
        showing_placeholder=showing_placeholder,
        options=options,
        checked=checked,
        date_format=date_format,
        has_nested_sdt=has_nested_sdt,
        ordinal=ordinal,
        flags=tuple(flags),
    )


def iter_sdt_elements_in_order(part_element: Any) -> Iterator[Any]:
    """Yield every ``w:sdt`` under ``part_element`` in document order.

    Document order is exactly projection order WITHIN a part, including nested
    controls: lxml's ``.iter()`` is a pre-order walk, so a container is yielded
    before the controls it wraps — which is what spec §1 requires ("1-based in
    projection order across ALL classes").
    """
    if part_element is None:
        return
    for el in part_element.iter(QN_W_SDT):
        yield el


def assign_ordinals(part_elements: Any) -> "dict[int, SdtInfo]":
    """Build the id(element) -> SdtInfo map for a whole document.

    ``part_elements`` is the ordered sequence of projected part roots (headers,
    body, footers, notes — the flattened projection order used by
    ``iter_document_parts_with_kind``). Ordinals run 1..N across ALL parts and
    ALL classes, so an un-anchored control still consumes its number (A1.3).

    This is the single pre-pass mandated by spec §9: ingest and the mapper both
    consume THIS map rather than counting controls themselves, so the two
    cannot drift the way they did over block separators (PROGRESS.md
    2026-08-21).
    """
    infos: "dict[int, SdtInfo]" = {}
    ordinal = 0
    for part_element in part_elements:
        for el in iter_sdt_elements_in_order(part_element):
            ordinal += 1
            infos[id(el)] = classify_sdt(el, ordinal)
    return infos
