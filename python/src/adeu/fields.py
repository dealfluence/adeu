"""Content-control discovery: the fields ledger and the protection banner.

Implements the fields ledger discovery mode and protection banner.

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
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
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
from .utils.protection import DocumentProtection, read_document_protection

#: Ledger lines per response (spec §4). FedRAMP rev4 projects 5,007 controls;
#: the cap keeps one response inside the same budget philosophy the changes
#: ledger already applies at 300 entries.
FIELDS_PAGE_SIZE = 100

#: Value/placeholder previews (spec §3 segments 7 and 8).
PREVIEW_CAP = 80

#: Dropdown/combobox options listed before the overflow marker (spec §3 §9).
OPTIONS_SHOWN = 8

#: ``w:documentProtection/@w:edit`` -> the BANNER's word (spec-projection §7).
#: Deliberately not ``DocumentProtection.describe()``: that phrasing serves
#: gate errors and A3.4 pins "read-only, enforced" as substrings of one. The
#: banner is a different surface with its own frozen wording, so the parse is
#: shared and only the rendering differs.
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


def protection_label(protection: DocumentProtection) -> str:
    """The banner/ledger phrasing for a parsed protection state (spec §7)."""
    if protection.edit is None:
        return "none"
    word = _PROTECTION_WORDS.get(protection.edit, protection.edit)
    return f"{word} (enforced)" if protection.enforced else word


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


def _has_text(sdt_element: Any) -> bool:
    content = sdt_element.find(QN_W_SDTCONTENT)
    if content is None:
        return False
    return any((t.text or "").strip() for t in content.iter(_W + "t"))


def field_summary(doc: Any) -> Tuple[int, int, int, int]:
    """``(total, empty, locked, bound)`` from the DOM alone.

    The banner and the appendix summary need only these four numbers, and
    paying for the full ledger to get them is what made this expensive: on
    FedRAMP rev4 the appendix would have carried 115ms of value previews,
    breadcrumbs and page lookups that nothing rendered. This walks the ordinal
    pre-pass and stops.

    ``empty`` is derived structurally here (placeholder shown, or no text in
    the content) rather than from the projection. A test pins that it agrees
    with the ledger's own count, because a banner that disagrees with the
    ledger it advertises is worse than no banner.
    """
    infos = assign_ordinals(part_element(p) for p, _kind in iter_document_parts_with_kind(doc))
    total = empty = locked = bound = 0
    for info in infos.values():
        total += 1
        if info.cls in _CONTAINER_CLASSES or info.cls == "checkbox":
            pass  # containers and checkboxes are never "empty" for the count
        elif info.showing_placeholder or not _has_text(info.element):
            empty += 1
        if info.cls == "group" or info.content_locked:
            locked += 1
        if info.bound:
            bound += 1
    return total, empty, locked, bound


def banner_for_document(doc: Any, hint: str = "") -> Optional[str]:
    """The full-view banner, computed without projecting values (spec §7)."""
    counts = field_summary(doc)
    protection = read_document_protection(doc)
    if counts[0] == 0 and protection.edit is None:
        return None
    line = f"> **Protection:** {protection_label(protection)} \u00b7 **Fields:** {_summary_text(counts)}"
    return f"{line}{hint}" if hint else line


#: (path, mtime_ns, size) -> banner. The banner is a pure function of the file
#: bytes, and the agent loop is read → edit → read, so the same version is
#: asked for repeatedly. Bounded because a long-lived server must not grow a
#: map keyed by every file it has ever seen.
_BANNER_MEMO: "OrderedDict[Tuple[str, int, int], Optional[str]]" = OrderedDict()
_BANNER_MEMO_MAX = 32


def banner_for_path(path: str, hint: str = "", loader: Any = None) -> Optional[str]:
    """:func:`banner_for_document` for a file, memoised on its stat key.

    Measured on fedramp_ssp_rev4 (5,007 controls): 68ms to load the package and
    82ms to classify every control — 150ms that a full-view read would
    otherwise repeat on every call, for four numbers that cannot change while
    the bytes do not. Typical documents are far below this; the memo exists for
    the outlier, which is exactly the document an agent pages through most.
    """
    try:
        st = os.stat(path)
        key = (str(Path(path).resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return None

    if key in _BANNER_MEMO:
        _BANNER_MEMO.move_to_end(key)
        cached = _BANNER_MEMO[key]
        return f"{cached}{hint}" if cached and hint else cached

    try:
        if loader is None:
            from .utils.opc import load_document

            loader = load_document
        banner = banner_for_document(loader(path))
    except Exception:
        # Advisory chrome. A malformed settings part, or a package python-docx
        # refuses, must not fail the read it decorates.
        banner = None

    _BANNER_MEMO[key] = banner
    if len(_BANNER_MEMO) > _BANNER_MEMO_MAX:
        _BANNER_MEMO.popitem(last=False)
    return f"{banner}{hint}" if banner and hint else banner


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


def _summary_text(counts: Tuple[int, int, int, int]) -> str:
    total, empty, locked, bound = counts
    if total == 0:
        return "no content controls"
    return f"{total} content controls \u2014 {empty} empty \u00b7 {locked} locked \u00b7 {bound} bound"


def _fields_summary(entries: Sequence[FieldEntry]) -> str:
    return _summary_text(summary_counts(entries))


def render_banner(
    entries: Sequence[FieldEntry],
    protection: DocumentProtection,
    hint: str = "",
) -> Optional[str]:
    """The full-view banner line (spec-projection §7), or None when unwarranted.

    A plain document — no controls, no protection — gains zero noise. That is
    the rule that keeps this from taxing every ordinary read.
    """
    if not entries and protection.edit is None:
        return None
    line = f"> **Protection:** {protection_label(protection)} \u00b7 **Fields:** {_fields_summary(entries)}"
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
        f"Protection: {protection_label(protection)} \u00b7 {_fields_summary(entries)}",
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
    counts: Tuple[int, int, int, int],
    protection: DocumentProtection,
    hint: str = "",
) -> List[str]:
    """The appendix's ``## Content Controls`` block (spec §5).

    Header lines only: the full ledger never renders here, because the appendix
    is bounded and a 5,007-line ledger would swallow it.
    """
    if counts[0] == 0 and protection.edit is None:
        return []
    lines = [
        "## Content Controls",
        "",
        f"Protection: {protection_label(protection)} \u00b7 {_summary_text(counts)}",
    ]
    if hint:
        lines.append(hint)
    return lines


# ---------------------------------------------------------------------------
# Resolution (CC-5)
# ---------------------------------------------------------------------------


class FieldResolutionError(ValueError):
    """A `set_field` target that could not be resolved to exactly one control.

    Carries the teaching text rather than a bare message: every one of these
    is recoverable by the caller, but only if the error says what the valid
    answers are (the invalid-action-id error class, spec-set-field §1).
    """


#: How many tags/aliases an unresolvable-field error lists before truncating.
#: A 5,000-control document would otherwise emit an error larger than the
#: document, and past ~30 the list stops being readable anyway.
_FIELD_SUGGESTION_CAP = 30


def _field_names(entry: "FieldEntry") -> Tuple[str, ...]:
    """Every string that resolves to this entry, in resolution order."""
    names = [f"CC:{entry.ordinal}"]
    if entry.tag:
        names.append(entry.tag)
    if entry.alias:
        names.append(entry.alias)
    return tuple(names)


def resolve_field(
    entries: Sequence["FieldEntry"],
    field: str,
    match_mode: str = "strict",
) -> List["FieldEntry"]:
    """Resolve a `set_field` target to the entries it names (spec §1).

    Order is ordinal, then exact `w:tag`, then exact `w:alias`, and it is an
    order rather than a merged lookup on purpose: tags and aliases are author
    strings, so a document may legally use `CC:2` as someone's tag. The
    documented id has to win, or the addressing scheme this engine publishes
    could be shadowed by the document it addresses.

    Matching is case-sensitive per spec — these are identifiers, and a
    case-insensitive match would make `Total` and `total` the same field in a
    document that deliberately uses both.
    """
    if not field or not field.strip():
        raise FieldResolutionError(
            "set_field requires 'field': the 'CC:<N>' id, tag, or alias of the control to fill. "
            "Run read_docx with mode='fields' to list them."
        )

    m = re.fullmatch(r"CC:(\d+)", field.strip())
    if m:
        ordinal = int(m.group(1))
        hits = [e for e in entries if e.ordinal == ordinal]
        if hits:
            return hits
        raise FieldResolutionError(f"No content control with id 'CC:{ordinal}'. {_available_summary(entries)}")

    hits = [e for e in entries if e.tag == field]
    if not hits:
        hits = [e for e in entries if e.alias == field]
    if not hits:
        raise FieldResolutionError(f"No content control matches field {field!r}. {_available_summary(entries)}")

    if len(hits) == 1 or match_mode == "all":
        return hits
    if match_mode == "first":
        return hits[:1]

    ids = ", ".join(f"CC:{e.ordinal}" for e in hits)
    raise FieldResolutionError(
        f"Field {field!r} matches {len(hits)} controls ({ids}). "
        "Target one by its 'CC:<N>' id, or set match_mode='first' to take the first "
        "or match_mode='all' to fill every occurrence."
    )


def _available_summary(entries: Sequence["FieldEntry"]) -> str:
    """The self-service tail of an unresolvable-field error."""
    if not entries:
        return "This document has no content controls."
    names: List[str] = []
    for entry in entries:
        for name in _field_names(entry)[1:]:  # skip the CC: id; shown separately
            if name not in names:
                names.append(name)
    shown = names[:_FIELD_SUGGESTION_CAP]
    tail = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    if not shown:
        return (
            f"This document's {len(entries)} controls have no tags or aliases; "
            "target them by id, CC:1 .. CC:%d. Run read_docx with mode='fields' for the list."
            % max(e.ordinal for e in entries)
        )
    return "Available: " + ", ".join(shown) + tail + ". Run read_docx with mode='fields' for the full list with ids."
