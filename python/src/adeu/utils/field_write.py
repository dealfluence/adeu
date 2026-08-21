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


# ---------------------------------------------------------------------------
# Per-class value rules (spec-set-field.md §2, §5)
# ---------------------------------------------------------------------------

#: Classes `set_field` can write in v1.
VALUE_BEARING = frozenset({"text", "richtext", "dropdown", "combobox", "date", "checkbox"})

#: Classes that hold no single value. Refusing these is not a limitation, it
#: is data protection: a group's "content" is the other controls inside it, so
#: replacing it with a string would delete every field it contains.
NON_VALUE = frozenset({"group", "repeating", "repeating-item", "picture", "building-block"})

_NON_VALUE_ADVICE = {
    "group": "Edit the fields nested inside it individually - each has its own CC: id.",
    "repeating": (
        "Fill the fields inside a specific item instead; repeating-section operations "
        "(add/remove item) are not supported in v1."
    ),
    "repeating-item": (
        "Fill the fields inside the item instead; repeating-section operations "
        "(add/remove item) are not supported in v1."
    ),
    "picture": "Picture controls hold an image, which set_field cannot write.",
    "building-block": "Building-block galleries insert document parts, not text.",
}


def is_multiline(info: SdtInfo) -> bool:
    """Does this plain-text control permit `w:br` (a `w:text w:multiLine`)?"""
    pr = sdt_pr(info.element)
    if pr is None:
        return False
    text_el = pr.find(qn("w:text"))
    if text_el is None:
        return False
    val = text_el.get(qn("w:multiLine"))
    return val is not None and val.lower() not in ("0", "false", "off")


def refuse_class(cls: str, ordinal: int) -> Optional[str]:
    """The A4.11 refusal for a control that holds no single value."""
    if cls in VALUE_BEARING:
        return None
    advice = _NON_VALUE_ADVICE.get(cls, "set_field fills value-bearing fields only.")
    return (
        f"CC:{ordinal} is a {cls} and is not a value-bearing field. {advice} "
        "set_field fills text, rich-text, dropdown, combobox, date and checkbox controls."
    )


def refuse_value(info: SdtInfo, ordinal: int, value: str) -> Optional[str]:
    """The A4.7 structure rules: what this class cannot physically hold.

    A `w:text` control has no paragraphs to put a paragraph in. Writing one
    anyway produces a control whose XML Word will not round-trip, so the
    refusal is the only non-destructive answer.
    """
    if info.cls != "text":
        return None
    if "\n\n" in value:
        return (
            f"CC:{ordinal} is a plain-text control and cannot hold paragraphs. "
            "Remove the blank line, or use a rich-text control for multi-paragraph content."
        )
    if "\n" in value and not is_multiline(info):
        return (
            f"CC:{ordinal} is a single-line plain-text control and cannot hold a line break. "
            "Remove the newline, or set the control's multiLine property in Word."
        )
    return None


# ---------------------------------------------------------------------------
# Checkbox (spec-set-field.md §5)
# ---------------------------------------------------------------------------

#: Accepted truthy/falsy spellings. Generous on input because the caller is a
#: language model reading a checkbox rendered as `[x]`, and strict rejection
#: of "checked" would be pedantry rather than safety.
_TRUTHY = frozenset({"true", "x", "[x]", "checked", "1", "yes", "on"})
_FALSY = frozenset({"false", "[ ]", "[]", "unchecked", "0", "no", "off", ""})


def parse_checkbox_value(value: str) -> Optional[bool]:
    """`True`/`False`, or `None` when the string names neither state (G11)."""
    v = value.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return None


def checkbox_glyph(info: SdtInfo, checked: bool) -> tuple:
    """The (character, font) this control uses for the given state.

    Read from `w14:checkedState` / `w14:uncheckedState` rather than assumed:
    a control may use any character in any symbol font, and hardcoding the
    common Segoe UI Symbol pair would silently change the document's glyph on
    every checkbox that used something else.
    """
    pr = sdt_pr(info.element)
    default = ("\u2612", None) if checked else ("\u2610", None)
    if pr is None:
        return default
    checkbox = pr.find(f"{W14}checkbox")
    if checkbox is None:
        return default
    state = checkbox.find(f"{W14}checkedState" if checked else f"{W14}uncheckedState")
    if state is None:
        return default
    raw = state.get(f"{W14}val")
    font = state.get(f"{W14}font")
    char = chr(int(raw, 16)) if raw else default[0]
    return (char, font)


def set_checkbox_checked(info: SdtInfo, checked: bool) -> None:
    """Flip `w14:checked/@w14:val`.

    SILENTLY, with no revision of its own: this is the URL_RETARGET class of
    change (spec §5). The visible glyph swap carries the redline; a revision
    on the attribute too would show the reviewer two changes for one act.
    """
    pr = sdt_pr(info.element)
    if pr is None:
        return
    checkbox = pr.find(f"{W14}checkbox")
    if checkbox is None:
        return
    node = checkbox.find(f"{W14}checked")
    if node is None:
        from lxml import etree

        node = etree.SubElement(checkbox, f"{W14}checked")
    node.set(f"{W14}val", "1" if checked else "0")


def glyph_run(info: SdtInfo) -> Optional[Any]:
    """The run carrying the checkbox's visible character."""
    runs = content_runs(info.element)
    return runs[0] if runs else None


# ---------------------------------------------------------------------------
# Dropdown / combobox (G10) and date (G12)
# ---------------------------------------------------------------------------


def resolve_option(info: SdtInfo, value: str) -> tuple:
    """Map a caller's string onto a list item: `(display_text, error)`.

    A `displayText` match wins; a `w:value` match resolves to that item's
    displayText, because the display text is what the document shows and what
    the next reader will diff against. Only one of the two can be written, and
    writing the machine value would make the document say `BC` where every
    other row says `British Columbia`.
    """
    options = list(info.options)
    if not options:
        return (value, None)
    for display, _val in options:
        if display == value:
            return (display, None)
    for display, val in options:
        if val and val == value:
            return (display, None)
    if info.cls == "combobox":
        # Free text is legal here; the report says so rather than the engine
        # refusing something Word permits.
        return (value, None)
    listed = " | ".join(display for display, _v in options)
    return (
        None,
        f"'{value}' is not one of this dropdown's options. Choose one of: {listed}.",
    )


def option_is_listed(info: SdtInfo, value: str) -> bool:
    return any(display == value or (val and val == value) for display, val in info.options)


def set_dropdown_last_value(info: SdtInfo, display_text: str) -> None:
    """Update `w:dropDownList/@w:lastValue` to match the written text.

    Silent, like the checkbox attribute: Word records the last selection here
    and a stale value re-selects the old option in the dropdown UI while the
    document text says something else.
    """
    pr = sdt_pr(info.element)
    if pr is None:
        return
    for name in ("w:dropDownList", "w:comboBox"):
        node = pr.find(qn(name))
        if node is not None:
            node.set(qn("w:lastValue"), display_text)
            return


#: The `w:dateFormat` letter-runs this engine renders in v1. Deliberately a
#: set of whole RUNS, not substrings: `dddd` is the day NAME and `MMMM` the
#: month name, and a substring test sees the supported `dd`/`MM` inside them.
#: Testing substrings turned `dddd, MMMM d` into `0101, 0303 1` - a date that
#: is not merely misformatted but unreadable, written silently.
_SUPPORTED_DATE_RUNS = {"yyyy", "MM", "M", "dd", "d"}


def parse_iso_date(value: str) -> Optional[tuple]:
    """`(y, m, d)` for a canonical `YYYY-MM-DD`, else `None`."""
    import re as _re

    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        import datetime

        datetime.date(y, mo, d)
    except ValueError:
        return None
    return (y, mo, d)


def render_date(parts: tuple, date_format: Optional[str]) -> tuple:
    """`(text, unsupported_format)` for the control's own `w:dateFormat`.

    ISO when the control declares no format, or when it declares one this
    engine cannot render faithfully - with the flag set so the caller's report
    says so. Writing an approximation of a format the document asked for is
    worse than writing the canonical form and admitting it.
    """
    import re as _re

    y, mo, d = parts
    iso = f"{y:04d}-{mo:02d}-{d:02d}"
    if not date_format:
        return (iso, False)

    runs = _re.findall(r"[A-Za-z]+", date_format)
    if not runs or any(run not in _SUPPORTED_DATE_RUNS for run in runs):
        return (iso, True)

    values = {
        "yyyy": f"{y:04d}",
        "MM": f"{mo:02d}",
        "M": str(mo),
        "dd": f"{d:02d}",
        "d": str(d),
    }
    return (_re.sub(r"[A-Za-z]+", lambda m: values[m.group(0)], date_format), False)


def set_full_date(info: SdtInfo, parts: tuple) -> None:
    """Sync `w:date/@w:fullDate`, silently (spec §5, URL_RETARGET class)."""
    pr = sdt_pr(info.element)
    if pr is None:
        return
    node = pr.find(qn("w:date"))
    if node is None:
        return
    y, mo, d = parts
    node.set(qn("w:fullDate"), f"{y:04d}-{mo:02d}-{d:02d}T00:00:00Z")


# ---------------------------------------------------------------------------
# Bound controls (spec-set-field.md §6)
# ---------------------------------------------------------------------------

_DS_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/customXml}"


def find_bound_store(doc: Any, store_item_id: Optional[str]) -> Optional[Any]:
    """The CustomXML part element for `store_item_id`, or `None`.

    Resolved by item id rather than by trying each store in turn: a package
    may carry several, and writing the caller's value into whichever one
    happened to match the xpath would corrupt an unrelated data island.
    """
    if not store_item_id:
        return None
    want = store_item_id.strip("{}").lower()
    try:
        parts = list(doc.part.package.parts)
    except Exception:
        return None

    props: dict = {}
    items: dict = {}
    for part in parts:
        name = str(part.partname)
        if "/customXml/itemProps" in name:
            props[name] = part
        elif "/customXml/item" in name:
            items[name] = part

    from lxml import etree

    for name, part in props.items():
        try:
            root = etree.fromstring(part.blob)
        except Exception:
            continue
        item_id = root.get(f"{_DS_NS}itemID") or root.get("itemID")
        if not item_id or item_id.strip("{}").lower() != want:
            continue
        # itemProps1.xml describes item1.xml: the trailing digits pair them,
        # which is the convention every producer follows and is cheaper than
        # walking relationships for a part that may not expose them.
        digits = "".join(ch for ch in name.rsplit("/", 1)[-1] if ch.isdigit())
        for iname, ipart in items.items():
            if "".join(ch for ch in iname.rsplit("/", 1)[-1] if ch.isdigit()) == digits:
                return ipart
    return None


def write_bound_value(part: Any, xpath: Optional[str], value: str) -> bool:
    """Set the bound node's text to `value`. True when the node was found.

    Mandatory rather than tidy (CC-6(e)): when `sdtContent` and the bound node
    disagree, Word rewrites the CONTENT from the store on open, with no
    revision. A tracked edit written to the content alone is not merely
    inconsistent - it is destroyed the next time anyone opens the document.
    """
    if not xpath:
        return False
    from lxml import etree

    try:
        root = etree.fromstring(part.blob)
        nodes = root.xpath(xpath)
    except Exception:
        return False
    if not nodes:
        return False
    node = nodes[0]
    if isinstance(node, str):
        return False
    for child in list(node):
        node.remove(child)
    node.text = value
    try:
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    except Exception:
        return False
    return True
