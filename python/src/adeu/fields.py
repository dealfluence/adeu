"""Content-control discovery: the fields ledger and the protection banner.

Spec: ``specs/content-controls/spec-fields-ledger.md`` (frozen v1) plus
``spec-projection.md`` §7 for the banner. Acceptance: A2, and A1.9 for the
banner.

This lives in the ENGINE, not in a surface, because three surfaces render the
same text — the CLI (``adeu extract --mode fields``), both MCP servers
(``read_docx(mode="fields")``) and the appendix summary. The line format is an
output contract that spec §7 explicitly asks callers to parse, so a second
implementation is a second dialect.

The ledger reads the *raw projection*, not the DOM, for every value it shows.
That is deliberate: a control's rendered value has already survived table
flattening (a row-level control's value is the markdown row ``A | B``),
CriticMarkup, and the placeholder-bubble rules. Re-deriving it from ``w:t``
would produce a ledger that quietly disagrees with the document text the agent
is editing.
"""

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .outline import clean_breadcrumb, offset_to_page
from .utils.content_controls import (
    QN_W_SDT,
    QN_W_SDTCONTENT,
    SdtInfo,
    assign_ordinals,
    part_element,
)
from .utils.docx import iter_document_parts_with_kind

#: Ledger lines per response (spec §4). FedRAMP rev4 projects 5,007 controls;
#: the cap keeps one response inside the same budget philosophy the changes
#: ledger already applies at 300 entries.
FIELDS_PAGE_SIZE = 100

#: Value/placeholder previews (spec §3 segments 7 and 8).
PREVIEW_CAP = 80

#: Dropdown/combobox options listed before the overflow marker (spec §3 §9).
OPTIONS_SHOWN = 8

#: ``w:documentProtection/@w:edit`` → the banner's word (spec-projection §7).
_PROTECTION_WORDS: Dict[str, str] = {
    "readOnly": "read-only",
    "forms": "fill-in-forms only",
    "comments": "comments only",
    "trackedChanges": "tracked-changes only",
}

#: Internal class name → the ledger's class word (spec §3 segment 2). Only
#: ``repeating-item`` differs; the rest are already the spec's vocabulary.
_CLASS_WORDS: Dict[str, str] = {"repeating-item": "item"}

#: Classes that describe their EXTENT instead of previewing a value. A group's
#: value would be every nested paragraph, which is the document, not a preview.
_CONTAINER_CLASSES = frozenset({"group", "repeating", "repeating-item"})

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_QN_W_P = _W + "p"
_QN_W_TBL = _W + "tbl"
_QN_W_TR = _W + "tr"
_QN_W_TC = _W + "tc"


@dataclass(frozen=True)
class DocumentProtection:
    """``w:documentProtection`` as the banner and ledger report it."""

    mode: str = "none"
    enforced: bool = False

    @property
    def label(self) -> str:
        if self.mode == "none":
            return "none"
        return f"{self.mode} (enforced)" if self.enforced else self.mode


@dataclass(frozen=True)
class FieldEntry:
    """One rendered ledger row."""

    ordinal: int
    cls_word: str
    alias: Optional[str] = None
    tag: Optional[str] = None
    page: int = 1
    heading_path: str = ""
    container_kind: Optional[str] = None  # "table cell" | "table row"
    parent_ordinal: Optional[int] = None
    states: Tuple[str, ...] = field(default_factory=tuple)
    value: Optional[str] = None
    checkbox_state: Optional[str] = None
    placeholder: Optional[str] = None
    options: Tuple[str, ...] = field(default_factory=tuple)
    date_format: Optional[str] = None
    extent: Optional[str] = None
    empty: bool = False
    locked: bool = False
    bound: bool = False


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------


def read_document_protection(doc: Any) -> DocumentProtection:
    """Read ``w:documentProtection`` from ``word/settings.xml``.

    Mirrors :func:`adeu.domain.extract_document_settings_warnings` in how it
    reaches the part: settings may load as a generic ``Part`` rather than an
    ``XmlPart``, so the blob is parsed directly rather than assumed to expose
    an element tree.
    """
    settings_part = None
    try:
        for part in doc.part.package.parts:
            if str(part.partname) == "/word/settings.xml":
                settings_part = part
                break
    except Exception:
        return DocumentProtection()
    if settings_part is None:
        return DocumentProtection()

    from docx.oxml import parse_xml

    try:
        root = parse_xml(settings_part.blob)
    except Exception:
        return DocumentProtection()

    node = None
    for el in root.iter():
        tag = el.tag
        if isinstance(tag, str) and tag.endswith("}documentProtection"):
            node = el
            break
    if node is None:
        return DocumentProtection()

    edit = node.get(_W + "edit")
    # An enforcement flag with no edit mode protects nothing in Word, and
    # reporting "none (enforced)" would be a contradiction the agent has to
    # resolve. Treat it as unprotected.
    if not edit or edit not in _PROTECTION_WORDS:
        return DocumentProtection()
    enforcement = node.get(_W + "enforcement")
    return DocumentProtection(
        mode=_PROTECTION_WORDS[edit],
        enforced=enforcement in ("1", "true"),
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


_ANCHOR_SCAN_RE = re.compile(r"\{#(/?)cc:(\d+)(?: [^}]*)?\}")


def _scan_anchors(raw_text: str) -> Dict[int, Tuple[int, int, int]]:
    """``ordinal -> (open_start, open_end, close_start)`` in ONE pass.

    Searching per control instead cost 8.8 seconds on FedRAMP rev4 — twenty
    times the cost of the whole projection — because each of 5,007 controls
    scanned 600 KB of text. The ledger is a read-path feature; it must not be
    the slowest thing in the read.
    """
    opens: Dict[int, Tuple[int, int]] = {}
    closes: Dict[int, int] = {}
    for m in _ANCHOR_SCAN_RE.finditer(raw_text):
        ordinal = int(m.group(2))
        if m.group(1):
            closes.setdefault(ordinal, m.start())
        else:
            opens.setdefault(ordinal, (m.start(), m.end()))
    bounds: Dict[int, Tuple[int, int, int]] = {}
    for ordinal, (open_start, open_end) in opens.items():
        close = closes.get(ordinal)
        if close is not None and close >= open_end:
            bounds[ordinal] = (open_start, open_end, close)
    return bounds


class _HeadingIndex:
    """Answers "which heading path contains this offset?" in O(log H).

    :func:`adeu.outline.heading_path_at` re-splits the whole projection on every
    call — fine for a handful of search hits, quadratic for a ledger with
    thousands of rows. This precomputes every heading's full breadcrumb once and
    binary-searches it; a test pins that the two agree line for line.
    """

    __slots__ = ("_starts", "_paths")

    def __init__(self, text: str) -> None:
        self._starts: List[int] = []
        self._paths: List[str] = []
        stack: List[Tuple[int, List[str]]] = []
        offset = 0
        for line in text.split("\n"):
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                heading = clean_breadcrumb(m.group(2))
                if len(heading) > 80:
                    heading = heading[:80] + "..."
                while stack and stack[-1][0] >= level:
                    stack.pop()
                path = (stack[-1][1] if stack else []) + [heading]
                stack.append((level, path))
                self._starts.append(offset)
                self._paths.append(" > ".join(path))
            offset += len(line) + 1

    def path_at(self, offset: int) -> str:
        if not self._starts:
            return ""
        # heading_path_at scans back from the END of the line containing the
        # offset, so a heading ON that line counts as containing it.
        i = bisect.bisect_right(self._starts, offset) - 1
        return self._paths[i] if i >= 0 else ""


def _preview(text: str, cap: int = PREVIEW_CAP) -> str:
    """Whitespace-collapsed, anchor-free, markup-free preview (spec §3.7)."""
    # clean_breadcrumb is the projection's existing "render this fragment as
    # plain prose" rule: it unwraps insertions, drops deletions and bubbles,
    # strips emphasis and removes {#…} tokens — including the anchors of any
    # nested control, which a container's span would otherwise carry.
    collapsed = re.sub(r"\s+", " ", clean_breadcrumb(text)).strip()
    if len(collapsed) > cap:
        return collapsed[:cap] + "\u2026"
    return collapsed


def _block_children(sdt_element: Any) -> List[Any]:
    content = sdt_element.find(QN_W_SDTCONTENT)
    if content is None:
        return []
    return [c for c in content if c.tag in (_QN_W_P, _QN_W_TBL)]


def _direct_child_sdts(sdt_element: Any) -> List[Any]:
    content = sdt_element.find(QN_W_SDTCONTENT)
    if content is None:
        return []
    return [c for c in content if c.tag == QN_W_SDT]


def _nested_sdt_count(sdt_element: Any) -> int:
    content = sdt_element.find(QN_W_SDTCONTENT)
    if content is None:
        return 0
    return sum(1 for _ in content.iter(QN_W_SDT))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _extent_for(info: SdtInfo) -> Optional[str]:
    """Spec §3 segment 11."""
    if info.cls == "group":
        blocks = len(_block_children(info.element))
        nested = _nested_sdt_count(info.element)
        return f"wraps {_plural(blocks, 'block')}, {_plural(nested, 'nested field')}"
    if info.cls == "repeating":
        items = len(_direct_child_sdts(info.element))
        return _plural(items, "item")
    if info.cls == "repeating-item":
        return f"wraps {_plural(len(_block_children(info.element)), 'block')}"
    return None


def _container_kind(info: SdtInfo) -> Optional[str]:
    """``table row`` / ``table cell`` for row- and cell-level controls.

    The inverse of ``wrapping_sdt``: rather than asking a row which control
    encloses it, ask a control what it encloses.
    """
    content = info.element.find(QN_W_SDTCONTENT)
    if content is None:
        return None
    for child in content:
        if child.tag == _QN_W_TR:
            return "table row"
        if child.tag == _QN_W_TC:
            return "table cell"
        break
    return None


def _states_for(info: SdtInfo, empty: bool) -> Tuple[str, ...]:
    """Spec §3 segment 6 — upper-case state tokens, in the spec's order."""
    states: List[str] = []
    if empty:
        states.append("EMPTY")
    # Order is the spec's: contents, then group, then no-delete. The fixture
    # pins the precedence — its group carries a bare `sdtLocked`, and the
    # golden calls it LOCKED (group), not LOCKED (no-delete).
    if info.content_locked:
        states.append("LOCKED (contents)")
    elif info.cls == "group":
        states.append("LOCKED (group)")
    elif info.delete_locked:
        states.append("LOCKED (no-delete)")
    if info.bound:
        states.append(f"BOUND \u2192 {info.binding_xpath or ''}".rstrip())
    if info.temporary:
        states.append("TEMPORARY")
    return tuple(states)


def collect_fields(
    doc: Any,
    raw_text: str,
    page_offsets: Optional[Sequence[int]] = None,
) -> List[FieldEntry]:
    """Build every ledger row for ``doc``, in ordinal order.

    ``raw_text`` is the RAW projection (anchors present); ``page_offsets`` the
    pagination result's ``body_page_offsets``.
    """
    infos = assign_ordinals(part_element(p) for p, _kind in iter_document_parts_with_kind(doc))
    ordered = sorted(infos.values(), key=lambda i: i.ordinal)

    # Nearest enclosing control, for the `in CC:<M>` segment. Walking up from
    # each control and looking the ancestor up in the SAME ordinal map keeps
    # the relation consistent with the numbering by construction.
    def parent_ordinal(info: SdtInfo) -> Optional[int]:
        el = info.element
        getparent = getattr(el, "getparent", None)
        if getparent is None:
            return None
        node = getparent()
        while node is not None:
            if node.tag == QN_W_SDT:
                parent = infos.get(id(node))
                if parent is not None:
                    return parent.ordinal
            node = node.getparent()
        return None

    anchors = _scan_anchors(raw_text)
    headings = _HeadingIndex(raw_text)

    entries: List[FieldEntry] = []
    last_known_offset = 0
    for info in ordered:
        bounds = anchors.get(info.ordinal)

        # Location. An anchored control reports its own offset exactly. An
        # un-anchored one (checkbox, picture, building block, repeating
        # section and its items — spec §1) has no token to find, so it inherits
        # the last offset established in document order. Ordinals ARE document
        # order, so this is monotone and never reports a control before its
        # predecessor; it is an approximation only in that an un-anchored
        # control sitting exactly on a page boundary can be attributed to the
        # page its predecessor ended on.
        if bounds is not None:
            last_known_offset = bounds[0]
        offset = last_known_offset

        page = offset_to_page(offset, page_offsets) if page_offsets else 1
        crumb = headings.path_at(offset)

        value: Optional[str] = None
        checkbox_state: Optional[str] = None
        placeholder: Optional[str] = None
        empty = info.showing_placeholder

        if info.cls == "checkbox":
            # Spec §3.7: checkboxes render their state where a value would go.
            checkbox_state = "checked" if info.checked else "unchecked"
        elif info.cls in _CONTAINER_CLASSES:
            pass  # extent instead of a value
        elif bounds is not None:
            raw_value = raw_text[bounds[1] : bounds[2]]
            preview = _preview(raw_value)
            if preview:
                value = preview
            else:
                empty = True

        if empty and info.placeholder_text:
            placeholder = _preview(info.placeholder_text)

        entries.append(
            FieldEntry(
                ordinal=info.ordinal,
                cls_word=_CLASS_WORDS.get(info.cls, info.cls),
                alias=info.alias,
                tag=info.tag,
                page=page,
                heading_path=crumb,
                container_kind=_container_kind(info),
                parent_ordinal=parent_ordinal(info),
                states=_states_for(info, empty),
                value=value,
                checkbox_state=checkbox_state,
                placeholder=placeholder,
                options=tuple(display for display, _value in info.options),
                date_format=info.date_format,
                extent=_extent_for(info),
                empty=empty,
                locked=info.cls == "group" or info.content_locked,
                bound=info.bound,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def summary_counts(entries: Sequence[FieldEntry]) -> Tuple[int, int, int, int]:
    """``(total, empty, locked, bound)`` — the banner/header counts (spec §7).

    ``locked`` is content-locked leaves plus group containers; a bare
    ``sdtLocked`` forbids deleting the control but leaves its contents
    editable, so it is a ledger detail and not a lock for counting purposes.
    """
    total = len(entries)
    return (
        total,
        sum(1 for e in entries if e.empty),
        sum(1 for e in entries if e.locked),
        sum(1 for e in entries if e.bound),
    )


def _fields_summary(entries: Sequence[FieldEntry]) -> str:
    total, empty, locked, bound = summary_counts(entries)
    if total == 0:
        return "no content controls"
    return f"{total} content controls \u2014 {empty} empty \u00b7 {locked} locked \u00b7 {bound} bound"


def render_banner(
    entries: Sequence[FieldEntry],
    protection: DocumentProtection,
    hint: str = "",
) -> Optional[str]:
    """The full-view banner line (spec-projection §7), or None when unwarranted.

    A plain document — no controls, no protection — gains zero noise. That is
    the rule that keeps this from taxing every ordinary read.
    """
    if not entries and protection.mode == "none":
        return None
    line = f"> **Protection:** {protection.label} \u00b7 **Fields:** {_fields_summary(entries)}"
    return f"{line}{hint}" if hint else line


def render_ledger(
    basename: str,
    entries: Sequence[FieldEntry],
    protection: DocumentProtection,
    offset: int = 0,
    page_size: int = FIELDS_PAGE_SIZE,
) -> str:
    """The ``mode="fields"`` body (spec §2-§4)."""
    header = [
        f"# Fields: {basename}",
        f"Protection: {protection.label} \u00b7 {_fields_summary(entries)}",
    ]
    if not entries:
        return "\n".join(header + ["", "No content controls."])

    total = len(entries)
    start = max(0, min(offset, total))
    window = entries[start : start + page_size]
    width = max(len(f"CC:{e.ordinal}") for e in entries)
    lines = [render_line(e, width) for e in window]

    remaining = total - (start + len(window))
    if remaining > 0:
        next_offset = start + len(window)
        lines.append(f"\u2026 {remaining} more \u2014 pass fields_offset={next_offset} to continue.")
    return "\n".join(header + [""] + lines)


def render_line(entry: FieldEntry, width: int) -> str:
    """One ledger line. The format is an output contract — see spec §3."""
    head = f"CC:{entry.ordinal}".ljust(width) + "  " + entry.cls_word

    name_parts: List[str] = []
    if entry.alias:
        name_parts.append(f'"{entry.alias}"')
    if entry.tag:
        name_parts.append(f"(tag: {entry.tag})")
    if name_parts:
        # Two spaces between the class word and the name group; an anonymous
        # control shows neither empty quotes nor an empty tag (A2.5).
        head += "  " + " ".join(name_parts)

    segments: List[str] = [f"p{entry.page}" + (f" \u00b7 {entry.heading_path}" if entry.heading_path else "")]
    if entry.container_kind:
        segments.append(entry.container_kind)
    if entry.parent_ordinal is not None:
        segments.append(f"in CC:{entry.parent_ordinal}")
    segments.extend(entry.states)
    if entry.checkbox_state:
        segments.append(entry.checkbox_state)
    elif entry.value is not None:
        segments.append(f'value: "{entry.value}"')
    if entry.placeholder:
        segments.append(f'placeholder: "{entry.placeholder}"')
    if entry.options:
        shown = list(entry.options[:OPTIONS_SHOWN])
        rendered = " | ".join(shown)
        extra = len(entry.options) - len(shown)
        if extra > 0:
            rendered += f" | \u2026 (+{extra} more)"
        segments.append(f"options: {rendered}")
    if entry.date_format:
        segments.append(f"format: {entry.date_format}")
    if entry.extent:
        segments.append(entry.extent)

    return head + "".join(f" \u2014 {s}" for s in segments)


def render_appendix_section(
    entries: Sequence[FieldEntry],
    protection: DocumentProtection,
    hint: str = "",
) -> List[str]:
    """The appendix's ``## Content Controls`` block (spec §5).

    Header lines only: the full ledger never renders here, because the appendix
    is bounded and a 5,007-line ledger would swallow it.
    """
    if not entries and protection.mode == "none":
        return []
    lines = [
        "## Content Controls",
        "",
        f"Protection: {protection.label} \u00b7 {_fields_summary(entries)}",
    ]
    if hint:
        lines.append(hint)
    return lines
