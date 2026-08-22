# FILE: src/adeu/mcp_components/_response_builders.py
"""Shared response builders for read_docx mode dispatch across disk and Live Word paths."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, List, Tuple

from adeu.fields import collect_fields, read_document_protection, render_ledger
from adeu.outline import _offset_to_page, extract_outline, heading_path_at
from adeu.pagination import (
    PAGE_RANGE_MAX_PAGES,
    PaginationResult,
    build_appendix_pointer,
    build_page_banner,
    build_page_footer,
    paginate,
    parse_page_arg,
    split_structural_appendix,
)
from adeu.utils.safe_regex import RegexTimeoutError, user_finditer

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject


class BuilderError(Exception):
    """
    User-facing validation failure from a response builder (bad page number,
    invalid search pattern). Framework-free on purpose: these builders serve
    both the MCP server and the CLI, and importing fastmcp costs ~0.7 s —
    more than the rest of an `adeu extract` invocation combined. The MCP
    tool layer converts this to ToolError; the CLI reports it as a usage
    error.
    """


@dataclass
class BuilderResult:
    """
    Framework-free response payload: `content` is the LLM/CLI-facing
    markdown, `structured_content` the machine-facing JSON. The MCP tool
    layer lifts this into a fastmcp ToolResult.
    """

    content: Any
    structured_content: "dict | None" = None


# Projection style markers: `**bold**` always; `_italic_` only where the
# underscore is not intra-word (identifiers like snake_case are literal text —
# the projection's italics markers always hug non-whitespace at a word edge).
_STYLE_MARKER_RE = re.compile(r"\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])")

# Regions the marker stripper must never touch: `{#anchor}` tokens (their
# leading underscore is document identity, not emphasis — copying
# `{#Ref444615940}` out of a snippet targets a nonexistent anchor) and literal
# underscore runs of 3+ (fill-in placeholders like `[_________]`) —
# QA 2026-07-23 F4b.
_PROTECTED_UNDERSCORE_RE = re.compile(r"\{#[^}]+\}|_{3,}")


def _emphasized_snippet(region: str, spans: List[Tuple[int, int]]) -> str:
    """
    Renders `region` with every matched span wrapped in `**…**` and the
    document's own bold/italic projection markers stripped first, so the
    highlight cannot collide with markers already present — a regex match
    crossing styled runs used to render as `**The **Supplier** _shall
    provide**_` (QA 2026-07-19 v8 F-10). Markers are detected over the WHOLE
    region (a match boundary can cut a marker away from its word-edge
    context), then each part is rebuilt from the surviving characters.
    Characters inside `{#anchor}` tokens or literal underscore runs are
    protected — they are content, not markers (F4b). Accepts MULTIPLE spans
    so one paragraph with several hits renders as one entry with every hit
    highlighted (QA round 3, finding 3.10).
    """
    keep = [True] * len(region)
    protected = [False] * len(region)
    for m in _PROTECTED_UNDERSCORE_RE.finditer(region):
        for i in range(m.start(), m.end()):
            protected[i] = True
    for m in _STYLE_MARKER_RE.finditer(region):
        if any(protected[i] for i in range(m.start(), m.end())):
            continue
        for i in range(m.start(), m.end()):
            keep[i] = False

    def _stripped(a: int, b: int) -> str:
        return "".join(c for i, c in enumerate(region[a:b], start=a) if keep[i])

    parts: List[str] = []
    cursor = 0
    for s, e in sorted(spans):
        parts.append(_stripped(cursor, s))
        parts.append(f"**{_stripped(s, e)}**")
        cursor = e
    parts.append(_stripped(cursor, len(region)))
    return "".join(parts)


SEARCH_TOKENS_PER_MATCH = 60
CHARS_PER_TOKEN = 4
SNIPPET_RADIUS_LADDER = (120, 60, 30, 16)

SEARCH_FIXED_CHROME_TOKENS = 120
SEARCH_ENTRY_CHROME_TOKENS = 22
SEARCH_MIN_SNIPPET_TOKENS = 13


def search_budget_tokens(max_matches: int, rendered_count: int | None = None) -> int:
    """Approximate-token ceiling for a search response."""
    if max_matches < 1:
        return SEARCH_FIXED_CHROME_TOKENS
    rendered = max_matches if rendered_count is None else min(max_matches, max(rendered_count, 0))
    return max(
        max_matches * SEARCH_TOKENS_PER_MATCH,
        SEARCH_FIXED_CHROME_TOKENS + rendered * (SEARCH_ENTRY_CHROME_TOKENS + SEARCH_MIN_SNIPPET_TOKENS),
    )


_SNIPPET_MARKUP_PAIRS = (("{>>", "<<}"), ("{--", "--}"), ("{++", "++}"), ("{==", "==}"))
_SNIPPET_CLOSER_OF = dict(_SNIPPET_MARKUP_PAIRS)
_SNIPPET_OPENER_OF = {closer: opener for opener, closer in _SNIPPET_MARKUP_PAIRS}
_SNIPPET_MARKUP_TOKEN_RE = re.compile("|".join(re.escape(t) for t in (*_SNIPPET_CLOSER_OF, *_SNIPPET_OPENER_OF)))

# `{#anchor}` tokens — bookmark anchors and CC-1's `{#cc:N}` content-control
# anchors. A snippet window or an outline truncation that lands inside one must
# not emit the fragment (CC-1 A1.6).
_ANCHOR_TOKEN_RE = re.compile(r"\{#[^}\n]*\}")


def _balance_snippet_window(body: str, start: int, end: int) -> tuple[int, int]:
    """Extends a snippet window until every CriticMarkup span and anchor token it overlaps is whole."""
    while True:
        depth = dict.fromkeys(_SNIPPET_CLOSER_OF, 0)
        widened = False

        for tok in _ANCHOR_TOKEN_RE.finditer(body):
            if tok.start() < start < tok.end():
                start = tok.start()
                widened = True
            if tok.start() < end < tok.end():
                end = tok.end()
                widened = True
            if tok.start() >= end:
                break
        if widened:
            continue

        for tok in _SNIPPET_MARKUP_TOKEN_RE.finditer(body, start, end):
            token = tok.group(0)
            if token in _SNIPPET_CLOSER_OF:
                depth[token] += 1
                continue
            opener = _SNIPPET_OPENER_OF[token]
            if depth[opener]:
                depth[opener] -= 1
            elif (prev_opener := body.rfind(opener, 0, start)) != -1:
                start = prev_opener
                widened = True
                break

        if widened:
            continue

        for opener, unclosed in depth.items():
            if unclosed and (next_closer := body.find(_SNIPPET_CLOSER_OF[opener], end)) != -1:
                end = next_closer + len(_SNIPPET_CLOSER_OF[opener])
                widened = True
                break

        if not widened:
            return start, end


_TRAILING_BUBBLE_RE = re.compile(r"\{>>\s*(\[[^\]\n]{0,80}\])(.*?)<<\}", re.DOTALL)


def _trailing_bubble_header(body: str, at: int) -> str:
    """
    The meta bubble the projection writes immediately after a deletion's or
    insertion's closer, reduced to its `[Chg:N …]` header — the id an agent
    needs to accept or reject the change it is looking at. The bubble's prose
    (author, date, pairings) is elided with `...` rather than reproduced,
    because this is re-attached to a snippet that was clamped for size in the
    first place. Empty when no bubble follows.
    """
    if not (m := _TRAILING_BUBBLE_RE.match(body, at)):
        return ""
    return "{>>" + m.group(1) + (" ..." if m.group(2).strip() else "") + "<<}"


def _enclosing_snippet_markup(body: str, start: int, end: int) -> tuple[str, str]:
    """
    Returns the ``(prefix, suffix)`` CriticMarkup tags a snippet window needs
    because it sits STRICTLY INSIDE spans that open before it and close after
    it.

    `_balance_snippet_window` only sees delimiters WITHIN the window, so a
    window cut out of the middle of a long deletion contains no delimiters at
    all, is declared balanced, and ships deleted text as live prose — the agent
    reads a clause the document no longer has and cannot see the `[Chg:N]` id
    it would need to accept or reject it (QA finding 2). The window is not
    widened to the span's own edges: a 4000-char deletion would then defeat
    clamping entirely. Only the tags are re-attached, so the snippet reads
    `{--…deleted…--}` and the ordered-balance invariant still holds.

    Spans are emitted outermost-first in the prefix and innermost-first in the
    suffix, so nesting stays well-formed. A `{>>` bubble carries its
    `[Chg:N …]` header into the prefix (that id is the only reason to show a
    bubble at all); a deletion or insertion carries the id-bearing bubble the
    projection writes immediately after its closer (`{--…--}{>>[Chg:7 delete]
    Author<<}`, ingest.py) into the suffix, because the tag alone says "this
    was deleted" without saying WHICH change deleted it. A pair whose closer is
    missing from the rest of the body is skipped rather than have a closer
    invented for it.
    """
    open_spans: list[tuple[int, str, str]] = []
    for opener, closer in _SNIPPET_MARKUP_PAIRS:
        pair_re = re.compile(f"{re.escape(opener)}|{re.escape(closer)}")
        stack: list[int] = []
        for tok in pair_re.finditer(body, 0, start):
            if tok.group(0) == opener:
                stack.append(tok.start())
            elif stack:
                stack.pop()
        if not stack or (closer_at := body.find(closer, end)) == -1:
            continue
        open_at = stack[-1]
        prefix, suffix = opener, closer
        if opener == "{>>":
            if header := re.match(r"\[[^\]\n]{0,80}\]", body[open_at + len(opener) : start]):
                prefix += header.group(0)
        else:
            suffix += _trailing_bubble_header(body, closer_at + len(closer))
        open_spans.append((open_at, prefix, suffix))

    open_spans.sort()
    return (
        "".join(prefix for _pos, prefix, _suffix in open_spans),
        "".join(suffix for _pos, _prefix, suffix in reversed(open_spans)),
    )


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merges overlapping/touching (start, end) spans, sorted by start."""
    merged: list[tuple[int, int]] = []
    for span_start, span_end in sorted(spans):
        if merged and span_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
        else:
            merged.append((span_start, span_end))
    return merged


def render_outline_tree(
    nodes: List[Any],
    max_level: int = 2,
    verbose: bool = False,
    is_cli: bool = False,
    file_path: str = "document.docx",
    no_chrome: bool = False,
) -> str:
    """
    Renders a flat list of OutlineNode objects as a Markdown tree.

    Args:
        nodes: full list of OutlineNode objects from extract_outline().
        max_level: only render nodes at level <= max_level. Default 2 keeps
            the output usable on large documents (a 1000-page legal doc can
            have 7000+ Heading-4-styled paragraphs that drown out real
            navigation structure). Pass max_level=6 for full depth.
        verbose: when True, includes style name, has_table flag, and
            footnote IDs in the per-node metadata. Off by default to
            keep the payload small for the common navigation case.
    """
    if not nodes:
        return "# (No headings detected)\n\nThis document has no detectable headings."

    visible = [n for n in nodes if n.level <= max_level]

    if not visible:
        if no_chrome:
            return (
                f"# (No headings at level <= {max_level})\n\nDocument has {len(nodes)} headings, all at deeper levels."
            )
        if is_cli:
            hint = f"Run `adeu extract {file_path} --mode outline --outline-max-level N` (up to 6) to see them."
        else:
            hint = "Call read_docx with mode='outline' and outline_max_level=N (up to 6) to see them."
        return (
            f"# (No headings at level <= {max_level})\n\n"
            f"Document has {len(nodes)} headings, all at deeper levels. "
            f"{hint}"
        )

    lines = []
    for node in visible:
        prefix = "#" * node.level
        if verbose:
            meta_parts = [f"p{node.page}", node.style]
            if node.has_table:
                meta_parts.append("has table")
            if node.footnote_ids:
                meta_parts.append("fn:" + ",".join(node.footnote_ids))
            meta = ", ".join(meta_parts)
            lines.append(f"{prefix} {node.text} ({meta})")
        else:
            page_str = f"p{node.page}"
            if node.end_page and node.end_page > node.page:
                page_str = f"p{node.page}-p{node.end_page}"
            lines.append(f"{prefix} {node.text} ({page_str})")
    return "\n".join(lines)


def _with_path_header(file_path: str, fields_banner: str | None, ui_markdown: str) -> str:
    """The LLM-only header block: File Path, then the fields banner.

    spec-projection §7 puts the banner immediately after the File Path line, so
    the two render as one blockquote. Both are chrome and both vanish under
    `no_chrome`, which exists so the projection can round-trip.
    """
    header = f"> **File Path:** `{file_path}`"
    if fields_banner:
        header += f"\n{fields_banner}"
    return f"{header}\n\n{ui_markdown}"


def build_full_document_response(
    text: str, file_path: str, no_chrome: bool = False, fields_banner: str | None = None
) -> BuilderResult:
    """
    Returns the ENTIRE document body in one response, with no page banner,
    continuation footer, or appendix pointer.

    This is the round-trip artifact for text-based apply/diff: page chrome is
    presentation, and a single page of a multi-page document can never round-
    trip safely (QA 2026-07-17 F1). Reached via `--page all` on the CLI and
    `page='all'` with `mode='full'` over MCP.
    """
    body, _appendix = split_structural_appendix(text)
    ui_markdown = body
    llm_content = ui_markdown if no_chrome else _with_path_header(file_path, fields_banner, ui_markdown)
    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_paginated_response(
    text: str,
    page: int,
    file_path: str,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    no_chrome: bool = False,
    fields_banner: str | None = None,
) -> BuilderResult:
    """
    Splits projected Markdown into pages and returns the requested page.

    The structural appendix is NOT included in the page content. Body pages
    get a one-line footer pointing the agent at mode='appendix' if the
    document has an appendix.

    `pagination_result`, when provided, MUST be paginate(body_of(text), "")
    — the server-layer projection cache passes its precomputed result so a
    warm page turn does no pagination work.

    Raises BuilderError if `page` is out of range.
    """
    body, appendix = split_structural_appendix(text)
    has_appendix = bool(appendix.strip())

    # Paginate body only. Pass empty string as structural_appendix so the
    # paginator does not glue anything onto each page.
    result = pagination_result if pagination_result is not None else paginate(body, structural_appendix="")

    if page < 1 or page > result.total_pages:
        raise BuilderError(f"Page {page} out of range (doc has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    if no_chrome:
        page_marker = f"[p{selected.page}/{selected.total_pages}]\n\n" if selected.total_pages > 1 else ""
        ui_markdown = f"{page_marker}{selected.page_content}"
        llm_content = ui_markdown
    else:
        # Build the original UI markdown
        banner = build_page_banner(selected.page, selected.total_pages, file_path, is_cli=is_cli)
        footer = build_page_footer(selected.page, selected.total_pages, selected.has_next, file_path, is_cli=is_cli)
        appendix_pointer = build_appendix_pointer(file_path, has_appendix, is_cli=is_cli)
        ui_markdown = banner + selected.page_content + footer + appendix_pointer

        # Prepend the path ONLY for the LLM
        llm_content = _with_path_header(file_path, fields_banner, ui_markdown)

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_page_range_response(
    text: str,
    start: int,
    end: int,
    file_path: str,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    no_chrome: bool = False,
) -> BuilderResult:
    """
    Returns a range of synthetic pages (from `start` to `end`, 1-indexed),
    capped at PAGE_RANGE_MAX_PAGES (8).

    Raises BuilderError if `start < 1` or `start > total_pages`.
    """
    if start < 1:
        raise BuilderError(f"Invalid page number {start}: page numbers must be positive integers.")
    if start > end:
        raise BuilderError(f"end page ({end}) cannot be less than start page ({start})")

    body, appendix = split_structural_appendix(text)
    has_appendix = bool(appendix.strip())

    result = pagination_result if pagination_result is not None else paginate(body, structural_appendix="")
    total_pages = result.total_pages

    if start > total_pages:
        raise BuilderError(f"Page {start} out of range (doc has {total_pages} pages).")

    last = min(end, start + PAGE_RANGE_MAX_PAGES - 1, total_pages)

    page_blocks: List[str] = []
    for p_num in range(start, last + 1):
        selected = result.pages[p_num - 1]
        if no_chrome:
            banner = f"[p{selected.page}/{selected.total_pages}]\n\n" if selected.total_pages > 1 else ""
        else:
            banner = build_page_banner(selected.page, selected.total_pages, file_path, is_cli=is_cli)
        page_blocks.append(f"{banner}{selected.page_content}")

    ui_parts = ["\n\n".join(page_blocks)]

    if not no_chrome:
        if last < end and last < total_pages:
            next_start = last + 1
            if is_cli:
                ui_parts.append(
                    f"> **Range capped at {PAGE_RANGE_MAX_PAGES} pages.** Continue with `--page {next_start}-{end}`."
                )
            else:
                ui_parts.append(
                    f'> **Range capped at {PAGE_RANGE_MAX_PAGES} pages.** Continue with `page="{next_start}-{end}"`.'
                )
        elif end > total_pages:
            ui_parts.append(f"> **[range stopped at page {total_pages}: the document has {total_pages} page(s)]**")

        appendix_pointer = build_appendix_pointer(file_path, has_appendix, is_cli=is_cli)
        if appendix_pointer:
            ui_parts.append(appendix_pointer.strip())

    ui_markdown = "\n\n".join(ui_parts)
    llm_content = ui_markdown if no_chrome else f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_outline_response(
    doc: "DocumentObject | None",
    projected_text: str,
    file_path: str,
    outline_max_level: int = 2,
    outline_verbose: bool = False,
    paragraph_offsets: dict | None = None,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    outline_nodes: "list | None" = None,
    no_chrome: bool = False,
) -> BuilderResult:
    """
    Returns a structural map of headings as a Markdown tree.

    Args:
        outline_max_level: cap on heading depth shown (default 2).
        outline_verbose: include per-node style/table/footnote metadata.
        paragraph_offsets: when provided, enables the fast outline path that
            avoids re-projecting paragraphs. Caller obtains this from
            _extract_text_from_doc(return_paragraph_offsets=True).
        pagination_result: precomputed paginate(body_of(projected_text), "").
        outline_nodes: precomputed extract_outline output for this document
            version. When provided together with pagination_result (the
            projection-cache path), `doc` and `paragraph_offsets` are not
            consulted and may be None — rendering needs only the nodes.
    """

    # Levels outside 1-6 are meaningless (0/negative would render a
    # nonsensical "L1-L0" range label, QA L2). The CLI rejects them at parse
    # time; clamp here so MCP callers get the nearest sensible depth.
    outline_max_level = max(1, min(outline_max_level, 6))

    # Pagination is used here only to compute body page boundaries for
    # heading->page mapping. We deliberately pass empty string instead of the
    # appendix — the appendix is never injected per page.
    body, _appendix = split_structural_appendix(projected_text)
    if pagination_result is None:
        pagination_result = paginate(body, structural_appendix="")

    if outline_nodes is not None:
        nodes = outline_nodes
    else:
        if doc is None:
            raise BuilderError("build_outline_response needs either `doc` or precomputed `outline_nodes`.")
        nodes = extract_outline(
            doc,
            body,
            pagination_result.body_pages,
            pagination_result.body_page_offsets,
            paragraph_offsets=paragraph_offsets,
        )
    rendered = render_outline_tree(
        nodes,
        max_level=outline_max_level,
        verbose=outline_verbose,
        is_cli=is_cli,
        file_path=file_path,
        no_chrome=no_chrome,
    )

    visible_count = sum(1 for n in nodes if n.level <= outline_max_level)
    deeper_count = len(nodes) - visible_count
    deeper_hint = f" ({deeper_count} more at deeper levels, raise outline_max_level to see)" if deeper_count > 0 else ""

    if is_cli:
        read_hint = f"Run `adeu extract {file_path} --page N` to read a section."
    else:
        read_hint = "Call `read_docx` with `mode='full'` and `page=N` to read a section."

    if no_chrome:
        ui_markdown = rendered
        llm_content = ui_markdown
    else:
        # Build the original UI markdown
        header = (
            f"> **Outline view** — showing {visible_count} of {len(nodes)} headings "
            f"(L1-L{outline_max_level}{deeper_hint}) across "
            f"{pagination_result.total_pages} page(s). "
            f"{read_hint}\n\n"
            f"---\n\n"
        )
        ui_markdown = header + rendered
        # Prepend the path ONLY for the LLM
        llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_budget_guard_message(
    projected_text: str,
    file_path: str,
    doc: "DocumentObject | None" = None,
    outline_nodes: "list | None" = None,
    pagination_result: "PaginationResult | None" = None,
    paragraph_offsets: dict | None = None,
    is_cli: bool = False,
) -> str:
    """
    Builds the whole-document response budget refusal for an oversized unbounded
    read. Shared by the CLI, the disk MCP path, and the Live Word MCP path so all
    three refuse with the same page count and the same L1 heading map.

    Page count comes from paginate(body, "") — the same pagination every reader
    path uses — so the page numbers the refusal advertises are the page numbers
    `--page N` / `page=N` accept.

    `outline_nodes` / `pagination_result` let a caller that already has them
    (the projection cache) skip the work. Documents with no L1 heading get no
    outline section at all, rather than a "(No headings detected)" placeholder.
    """
    from adeu.payloads import response_budget_limit, whole_doc_guard_message

    body, _appendix = split_structural_appendix(projected_text)
    pagination = pagination_result if pagination_result is not None else paginate(body, structural_appendix="")

    nodes = outline_nodes
    if nodes is None and doc is not None:
        nodes = extract_outline(
            doc,
            body,
            pagination.body_pages,
            pagination.body_page_offsets,
            paragraph_offsets=paragraph_offsets,
        )
    nodes = nodes or []
    has_l1 = any(node.level == 1 for node in nodes)
    outline = render_outline_tree(nodes, max_level=1, is_cli=is_cli, file_path=file_path) if has_l1 else ""

    return whole_doc_guard_message(
        total_chars=len(projected_text),
        limit=response_budget_limit(),
        file_path=file_path,
        outline=outline,
        page_count=pagination.total_pages,
    )


@dataclass
class _LedgerEntry:
    kind: str  # "chg" or "com"
    cid: str  # e.g. "12" or "5"
    change_type: str  # "ins", "del", or "fmt"
    author: str
    page: int
    snippet: str
    pair_ids: list[str] = field(default_factory=list)
    reply_to_id: str | None = None
    position: int = 0


def build_search_response(
    text: str,
    search_query: str,
    search_regex: bool,
    search_case_sensitive: bool,
    page: int | str | None,
    file_path: str,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    max_matches: int = 20,
    match_offset: int = 0,
    full_paragraph: bool = False,
    no_chrome: bool = False,
) -> BuilderResult:
    """
    Filters projected Markdown to exact substring or regex matches.

    `page` semantics:
      - None or "all" (case-insensitive): return ALL matches across the whole
        document. When matches span >1 document page, include a one-line
        distribution summary.
      - positive int N: return only matches whose offset falls within document
        page N. If N has zero hits but the query exists on other pages, emit a
        helpful empty-result pointer (not an error). If N exceeds the document's
        total pages, raise BuilderError.
      - anything else (0, negative, non-"all" string): raise BuilderError.

    Occurrence counts (the "appears X times" line under each match) are always
    computed from the FULL match set, never filtered.

    `max_matches < 1` renders NO match entries — just the counts header and a
    note naming the knob to raise. It is never rewritten to the default 20.
    """
    # `max_matches < 1` is honoured as the zero it says, not silently rewritten
    # to the default 20: a caller (or a tool wrapper computing a remaining
    # budget) that asks for 0 matches and is handed 20 full snippets gets a
    # payload it never asked for (QA finding 3). The response still reports the
    # totals and names the knob to raise.
    if match_offset < 0:
        match_offset = 0

    body, _ = split_structural_appendix(text)
    flags = 0 if search_case_sensitive else re.IGNORECASE

    # Invalid-regex handling differs by caller. The MCP path downgrades to a
    # literal search with an explanatory note: the model reads the note and
    # either accepts the literal hits or fixes its pattern, without burning a
    # turn on a hard error. The CLI path is strict — automation that asked
    # for regex semantics gets a non-zero exit, never silently-literal
    # results. Patterns that blow the matching time budget (catastrophic
    # backtracking) follow the same split.
    regex_downgraded_note = ""
    if search_regex:
        try:
            matches = list(user_finditer(search_query, body, flags=flags))
        except re.error as e:
            if is_cli:
                raise BuilderError(
                    f"--search-regex pattern is not a valid regular expression: {e}. "
                    "Fix the pattern, or drop --search-regex to search for the literal text."
                ) from None
            regex_downgraded_note = (
                f"> **Note:** `{search_query}` is not a valid regular expression "
                f"({e}), so it was searched as literal text instead. "
                f"If you meant a regex, fix the pattern; if you meant literal "
                f"text, set `search_regex` to false."
            )
            matches = list(re.finditer(re.escape(search_query), body, flags=flags))
        except RegexTimeoutError as e:
            if is_cli:
                raise BuilderError(str(e)) from None
            regex_downgraded_note = (
                f"> **Note:** `{search_query}` was searched as literal text instead of as a regular expression: {e}"
            )
            matches = list(re.finditer(re.escape(search_query), body, flags=flags))
    else:
        matches = list(re.finditer(re.escape(search_query), body, flags=flags))

    # Pagination needed for both filter mode and distribution summary, even
    # when there are no matches (to validate `page` is in range).
    pag_res = pagination_result if pagination_result is not None else paginate(body, "")
    page_offsets = pag_res.body_page_offsets
    total_doc_pages = pag_res.total_pages

    # ---- Resolve `page` into either None (= all) or a 1-indexed int. ----
    page_filter: int | None
    if page is None:
        page_filter = None
    elif isinstance(page, str):
        if page.lower() == "all":
            page_filter = None
        else:
            # Allow numeric strings ("3"); reject anything else.
            try:
                page_filter = int(page)
            except (TypeError, ValueError):
                raise BuilderError(
                    f"Invalid page value: {page!r}. In search mode, `page` must be "
                    f"omitted (search all pages), `'all'`, or a positive integer "
                    f"document page number."
                ) from None
            if page_filter < 1:
                raise BuilderError(
                    f"Invalid page value: {page!r}. In search mode, `page` must be "
                    f"omitted, `'all'`, or a positive integer document page number."
                )
    elif isinstance(page, int):
        if page < 1:
            raise BuilderError(
                f"Invalid page value: {page!r}. In search mode, `page` must be "
                f"omitted, `'all'`, or a positive integer document page number."
            )
        page_filter = page
    else:
        raise BuilderError(
            f"Invalid page value: {page!r}. In search mode, `page` must be "
            f"omitted, `'all'`, or a positive integer document page number."
        )

    if page_filter is not None and page_filter > total_doc_pages:
        raise BuilderError(
            f"Document page {page_filter} is out of range — the document has "
            f"{total_doc_pages} page(s). In search mode, `page` filters matches "
            f"by document page; omit `page` (or pass `page='all'`) to search "
            f"across the whole document."
        )

    # ---- No matches anywhere. ----
    if not matches:
        # The retry advice must name knobs the caller can actually type: CLI
        # flags for the CLI, tool parameters for MCP (QA 2026-07-18 L1).
        if is_cli:
            retry_hint = (
                "Verify your search spelling, or retry with --search-case-insensitive "
                "or with --search-regex if you used pattern wildcards."
            )
        else:
            retry_hint = (
                "Verify your search spelling, or try setting `search_case_sensitive` to false "
                "or enabling `search_regex` if you used pattern wildcards."
            )
        if no_chrome:
            ui_markdown = f"No matches found for query `{search_query}`."
        else:
            ui_markdown = (
                f"> **Search Results** — No matches found for query `{search_query}` in `{Path(file_path).name}`.\n\n"
                + retry_hint
            )
        if regex_downgraded_note:
            ui_markdown = f"{regex_downgraded_note}\n\n{ui_markdown}"
        llm_content = ui_markdown if no_chrome else f"> **File Path:** `{file_path}`\n\n{ui_markdown}"
        return BuilderResult(
            content=llm_content,
            structured_content={
                "markdown": ui_markdown,
                "title": f"Search: {Path(file_path).name}",
                "file_path": str(Path(file_path).resolve()),
            },
        )

    # ---- Assign each match to its document page. ----
    matches_with_pages = [(m, _offset_to_page(m.start(), page_offsets)) for m in matches]
    total_matches = len(matches_with_pages)

    # Global occurrence map — never filtered.
    occurrences_map: dict[str, int] = {}
    for m, _p in matches_with_pages:
        occurrences_map[m.group(0)] = occurrences_map.get(m.group(0), 0) + 1

    # Distribution of matches across doc pages — also computed from the full set.
    page_distribution: dict[int, int] = {}
    for _m, p in matches_with_pages:
        page_distribution[p] = page_distribution.get(p, 0) + 1
    pages_with_hits = sorted(page_distribution.keys())

    # ---- Apply filter. ----
    if page_filter is None:
        filtered = matches_with_pages
    else:
        filtered = [(m, p) for (m, p) in matches_with_pages if p == page_filter]

        # `page=N` valid but has no hits, query exists elsewhere.
        if not filtered:
            other_pages_str = ", ".join(str(p) for p in pages_with_hits)
            if no_chrome:
                ui_markdown = (
                    f"No matches on document page {page_filter} for query `{search_query}`. "
                    f"Query appears on page(s) {other_pages_str}."
                )
            else:
                ui_markdown = (
                    f"> **Search Results** — No matches on document page {page_filter} "
                    f"for query `{search_query}` in `{Path(file_path).name}`.\n\n"
                    f"The query DOES appear elsewhere ({total_matches} match"
                    f"{'es' if total_matches != 1 else ''} on page"
                    f"{'s' if len(pages_with_hits) != 1 else ''} {other_pages_str}). "
                    f"Omit `page` or pass `page='all'` to see them."
                )
            llm_content = ui_markdown if no_chrome else f"> **File Path:** `{file_path}`\n\n{ui_markdown}"
            return BuilderResult(
                content=llm_content,
                structured_content={
                    "markdown": ui_markdown,
                    "title": f"Search: {Path(file_path).name}",
                    "file_path": str(Path(file_path).resolve()),
                },
            )

    # ---- Render. ----
    total_filtered = len(filtered)

    def window_note_response(note: str) -> BuilderResult:
        """
        A counts header plus one explanatory note and NO match entries, for
        every reason the requested window renders nothing: `match_offset` past
        the last match, `max_matches` below 1, or a size budget that cannot
        pay for even one snippet. The totals are still reported so the caller
        knows the query itself matched.
        """
        ui_parts: list[str] = []
        if not no_chrome:
            if page_filter is None:
                ui_parts.append(
                    f"> **Search Results** — Found {total_matches} match"
                    f"{'es' if total_matches != 1 else ''} for query `{search_query}` "
                    f"in `{Path(file_path).name}`."
                )
            else:
                ui_parts.append(
                    f"> **Search Results** — Found {total_filtered} match"
                    f"{'es' if total_filtered != 1 else ''} on document page {page_filter} "
                    f"for query `{search_query}` in `{Path(file_path).name}` "
                    f"({total_matches} total in document)."
                )
        ui_parts.append(note)
        if regex_downgraded_note:
            ui_parts.insert(0, regex_downgraded_note)
        note_markdown = "\n\n".join(part for part in ui_parts if part)
        llm_content = note_markdown if no_chrome else f"> **File Path:** `{file_path}`\n\n{note_markdown}"
        return BuilderResult(
            content=llm_content,
            structured_content={
                "markdown": note_markdown,
                "title": f"Search: {Path(file_path).name}",
                "file_path": str(Path(file_path).resolve()),
            },
        )

    if max_matches < 1:
        knob = "`--max-matches N`" if is_cli else "`max_matches=N`"
        if no_chrome:
            note_str = f"No matches shown (max_matches={max_matches}, total matches={total_filtered})."
        else:
            note_str = (
                f"> **Note:** No matches shown (max_matches={max_matches}, total matches={total_filtered}). "
                f"Pass {knob} with N >= 1 to see match snippets."
            )
        return window_note_response(note_str)

    if match_offset >= total_filtered:
        if no_chrome:
            note_str = f"No matches in this window (match_offset={match_offset}, total matches={total_filtered})."
        else:
            note_str = (
                f"> **Note:** No matches in this window (match_offset={match_offset}, total matches={total_filtered})."
            )
        return window_note_response(note_str)

    selected_matches = filtered[match_offset : match_offset + max_matches]

    def build_header(num_rendered: int) -> list[str]:
        """
        Header, distribution, and continuation notes for a response that
        renders `num_rendered` of the filtered matches. Built from the final
        count so the "N shown" figure and the `match_offset` to continue from
        stay truthful when the budget pass drops trailing entries.
        """
        head: list[str] = []
        next_offset = match_offset + num_rendered
        has_more = next_offset < total_filtered

        if page_filter is None:
            if total_filtered > num_rendered or match_offset > 0:
                head.append(
                    f"> **Search Results** — Found {total_matches} match"
                    f"{'es' if total_matches != 1 else ''} for query `{search_query}` "
                    f"in `{Path(file_path).name}` ({total_matches} total, {num_rendered} shown)."
                )
            else:
                head.append(
                    f"> **Search Results** — Found {total_matches} match"
                    f"{'es' if total_matches != 1 else ''} for query `{search_query}` "
                    f"in `{Path(file_path).name}`."
                )
            # Distribution summary only when matches span >1 document page.
            if len(pages_with_hits) > 1:
                dist_str = ", ".join(f"p{p}: {page_distribution[p]}" for p in pages_with_hits)
                head.append(f"> Distribution across {len(pages_with_hits)} document pages — {dist_str}")
        else:
            shown = total_filtered
            if total_filtered > num_rendered or match_offset > 0:
                head.append(
                    f"> **Search Results** — Found {shown} match"
                    f"{'es' if shown != 1 else ''} on document page {page_filter} "
                    f"for query `{search_query}` in `{Path(file_path).name}` "
                    f"({total_matches} total in document, {num_rendered} shown)."
                )
            else:
                head.append(
                    f"> **Search Results** — Found {shown} match"
                    f"{'es' if shown != 1 else ''} on document page {page_filter} "
                    f"for query `{search_query}` in `{Path(file_path).name}` "
                    f"({total_matches} total in document)."
                )
            other_pages = [p for p in pages_with_hits if p != page_filter]
            if other_pages:
                other_pages_str = ", ".join(str(p) for p in other_pages)
                head.append(
                    f"> Additional matches exist on page"
                    f"{'s' if len(other_pages) != 1 else ''} {other_pages_str} — "
                    f"omit `page` or pass `page='all'` to see them."
                )

        if has_more:
            knob = f"`--match-offset {next_offset}`" if is_cli else f"`match_offset={next_offset}`"
            head.append(
                f"> **Note:** Only {num_rendered} matches shown (max_matches={max_matches}). Continue with {knob}."
            )
        return head

    # Hoisted to adeu.outline for CC-2 so the fields ledger renders identical
    # breadcrumbs from the same projection rather than a second dialect.
    get_heading = heading_path_at

    # Match index is preserved from the FULL match list so an LLM that sees
    # "Match 7 (p3)" knows it is the 7th match overall, not the 7th on this page.
    full_index_map = {id(m): i + 1 for i, (m, _p) in enumerate(matches_with_pages)}

    def group_by_line(hits: list) -> list[tuple[int, list]]:
        """
        Groups hits by their containing projection line: one paragraph renders
        as ONE entry with every hit emphasized, instead of once per regex
        alternation branch with divergent highlights (QA round 3, finding
        3.10). Called on every budget attempt, because the unit the budget
        pass drops is the HIT, not the entry — dropping the tail of a hit list
        both shortens the last entry and, once its last hit goes, removes it.
        """
        groups: list[tuple[int, list]] = []
        by_line: dict[int, list] = {}
        for m, p_num in hits:
            last_nl = body.rfind("\n", 0, m.start())
            line_start = 0 if last_nl == -1 else last_nl + 1
            if line_start not in by_line:
                by_line[line_start] = []
                groups.append((line_start, by_line[line_start]))
            by_line[line_start].append((m, p_num))
        return groups

    def render_entry(line_start: int, group: list, radius: int | None) -> str:
        """
        Renders one paragraph's hits as a single match entry. `radius` is the
        context kept on each side of every hit; None renders the whole
        paragraph (`full_paragraph`). Blocks are joined with blank lines
        because each one is its own Markdown block — a bare "\\n" glued the
        heading, snippet, and occurrence line into one paragraph (QA finding 3).
        """
        first_m, p_num = group[0]

        last_m_end = max(m.end() for m, _p in group)
        next_nl = body.find("\n", last_m_end)
        line_end = len(body) if next_nl == -1 else next_nl

        if radius is None:
            intervals = [(line_start, line_end)]
        else:
            windows = [(max(line_start, m.start() - radius), min(line_end, m.end() + radius)) for m, _p in group]
            # Balance AFTER merging (a widened window can swallow its
            # neighbour) and merge again, so no two segments overlap.
            intervals = _merge_spans([_balance_snippet_window(body, s, e) for s, e in _merge_spans(windows)])

        segments: list[str] = []
        for s_pos, e_pos in intervals:
            spans = [(m.start() - s_pos, m.end() - s_pos) for m, _p in group if s_pos <= m.start() and m.end() <= e_pos]
            # Re-attach the tags of any span this window sits strictly inside
            # (QA finding 2): a window cut out of the middle of a deletion
            # holds no delimiters at all, so without this the deleted clause
            # reads as live prose. The tags are added OUTSIDE
            # _emphasized_snippet, whose job is stripping the document's own
            # bold/italic markers from the region's characters.
            open_tags, close_tags = _enclosing_snippet_markup(body, s_pos, e_pos)
            segments.append(open_tags + _emphasized_snippet(body[s_pos:e_pos], spans) + close_tags)

        # " ... " marks elided interior text between distant hits; the outer
        # "..." marks context trimmed off the head/tail. The head/tail marks
        # are measured against the line each EDGE landed on, not the hit's
        # line: balancing an unterminated bubble can pull the window onto an
        # earlier line, and comparing against the hit's line then silently
        # dropped the "..." for text elided on that earlier line.
        snippet = " ... ".join(segments)
        first_line_start = body.rfind("\n", 0, intervals[0][0]) + 1
        last_nl_after = body.find("\n", intervals[-1][1])
        last_line_end = len(body) if last_nl_after == -1 else last_nl_after
        if intervals[0][0] > first_line_start:
            snippet = "..." + snippet
        if intervals[-1][1] < last_line_end:
            snippet = snippet + "..."

        snippet_lines = "\n".join(f"> {line}" for line in snippet.split("\n") if line.strip())

        idx = full_index_map[id(first_m)]
        match_lines = ["---", f"### Match {idx} (p{p_num})"]
        if h_path := get_heading(first_m.start(), body):
            match_lines.append(f"**Path:** `{h_path}`")

        distinct_strs: list[str] = []
        for m, _p in group:
            if m.group(0) not in distinct_strs:
                distinct_strs.append(m.group(0))
        if len(distinct_strs) == 1:
            n = occurrences_map[distinct_strs[0]]
            occurrence_line = (
                f"*Occurrences:* This exact phrasing appears {n} time{'s' if n != 1 else ''} in the document."
            )
        else:
            occurrence_line = (
                "*Occurrences:* "
                + "; ".join(
                    f"`{s}` appears {occurrences_map[s]} time{'s' if occurrences_map[s] != 1 else ''}"
                    for s in distinct_strs
                )
                + " in the document."
            )
        match_lines.extend([snippet_lines, occurrence_line])
        return "\n\n".join(match_lines)

    content_prefix = "" if no_chrome else f"> **File Path:** `{file_path}`\n\n"

    def compose(hits: list, radius: int | None, budget_note: str) -> str:
        parts = [] if no_chrome else build_header(len(hits))
        parts.extend(render_entry(line_start, group, radius) for line_start, group in group_by_line(hits))
        if budget_note and not no_chrome:
            parts.append(budget_note)
        # The downgrade note survives `no_chrome`: it reports that the query
        # was searched with DIFFERENT semantics than asked for, so suppressing
        # it would make the hit list read as regex matches. Query semantics are
        # not chrome — the zero-match and window-note paths keep it too.
        if regex_downgraded_note:
            parts.insert(0, regex_downgraded_note)
        return "\n\n".join(part for part in parts if part)

    # ---- Response size budget (QA finding 2). ----
    # A ±120 window is up to 240 chars of context PER HIT, so 20 hits in long
    # paragraphs blow the ~60-tokens-per-match ceiling this response is sized
    # against even though each snippet is individually clamped. Render at the
    # widest radius that fits the whole payload; if even the narrowest does
    # not, drop trailing HITS (the caller reaches them with match_offset)
    # rather than emit an oversized response. `full_paragraph` is an explicit
    # opt-out: the caller asked for whole paragraphs and gets them.
    #
    # The unit dropped is the HIT, never the entry. Trimming entries could not
    # enforce the budget at all when the hits share ONE projection line — 20
    # edits or 20 table cells in one paragraph are one entry, so an
    # entry-dropping pass had nothing to drop and shipped ~1800 tokens against
    # a 1200 ceiling — and the radius ladder cannot rescue that case either,
    # because a balanced window is at least as wide as the CriticMarkup spans
    # it must keep whole, however small the radius (QA finding 1). Dropping the
    # tail of the hit list shortens the last entry hit by hit (its trailing
    # "..." says text was elided) and removes the entry once its last hit goes,
    # so `build_header` keeps reporting a truthful "N shown" and a
    # `match_offset` that resumes exactly where the response stopped. When not
    # even one hit fits, the response says so instead of overshooting.
    #
    # The ceiling includes the response's fixed chrome (see
    # search_budget_tokens): on `max_matches=1` or `2` the header and entry
    # scaffolding alone outweigh `max_matches * 60`, so a purely content-sized
    # budget was unreachable and every radius "failed" down to a context-free
    # `...**hit**...` (QA round 4, finding 1).
    def fits(markdown: str, rendered_count: int) -> bool:
        budget_chars = search_budget_tokens(max_matches, rendered_count) * CHARS_PER_TOKEN
        return len(content_prefix) + len(markdown) <= budget_chars

    if full_paragraph:
        ui_markdown = compose(selected_matches, None, "")
    else:
        radius = SNIPPET_RADIUS_LADDER[0]
        ui_markdown = compose(selected_matches, radius, "")
        for radius in SNIPPET_RADIUS_LADDER[1:]:
            if fits(ui_markdown, len(selected_matches)):
                break
            ui_markdown = compose(
                selected_matches,
                radius,
                f"> **Note:** Snippets trimmed to ±{radius} chars to fit the response size budget.",
            )
        kept = list(selected_matches)
        while kept and not fits(ui_markdown, len(kept)):
            kept.pop()
            if not kept:
                opt_out = "`--full-paragraph`" if is_cli else "`full_paragraph=true`"
                if no_chrome:
                    note_str = (
                        f"No matches shown in this window: not even one ±{radius}-char snippet fits "
                        f"the response size budget (max_matches={max_matches}, total matches={total_filtered})."
                    )
                else:
                    note_str = (
                        f"> **Note:** No matches shown in this window: not even one ±{radius}-char snippet fits "
                        f"the response size budget (max_matches={max_matches}, total matches={total_filtered}). "
                        f"Raise `max_matches`, or pass {opt_out} to read the matching paragraph in full."
                    )
                return window_note_response(note_str)
            ui_markdown = compose(
                kept,
                radius,
                f"> **Note:** Snippets trimmed to ±{radius} chars and trailing matches dropped "
                f"to fit the response size budget — continue from the `match_offset` above.",
            )

    return BuilderResult(
        content=content_prefix + ui_markdown,
        structured_content={
            "markdown": ui_markdown,
            "title": f"Search: {Path(file_path).name}",
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_appendix_response(
    text: str,
    page: int,
    file_path: str,
    is_cli: bool = False,
    no_chrome: bool = False,
) -> BuilderResult:
    """
    Returns the structural appendix (defined terms, anchors, diagnostics) for
    the document, paginated. The appendix is treated AS the body for pagination
    purposes — same paginator, same boundary safety, same per-page banner.

    The agent fetches this on demand to inform editing decisions on documents
    where the body pages flag an appendix exists.

    Raises BuilderError if `page` is out of range.
    Returns a single-page "no appendix" response if the document has no
    structural metadata.
    """
    _body, appendix = split_structural_appendix(text)

    if not appendix.strip():
        ui_markdown = (
            "# Appendix\n\n"
            "This document has no structural appendix "
            "(no defined terms, named anchors, or diagnostics detected)."
        )
        llm_content = ui_markdown if no_chrome else f"> **File Path:** `{file_path}`\n\n{ui_markdown}"
        return BuilderResult(
            content=llm_content,
            structured_content={
                "markdown": ui_markdown,
                "title": Path(file_path).name,
                "file_path": str(Path(file_path).resolve()),
            },
        )

    # Treat the appendix AS the body and paginate it.
    result = paginate(appendix, structural_appendix="")

    if page < 1 or page > result.total_pages:
        raise BuilderError(f"Appendix page {page} out of range (appendix has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    if no_chrome:
        page_marker = f"[p{selected.page}/{selected.total_pages}]\n\n" if selected.total_pages > 1 else ""
        ui_markdown = f"{page_marker}{selected.page_content}"
        llm_content = ui_markdown
    else:
        # Build the appendix-specific banner. Reusing _build_page_banner would emit
        # generic "Page N of M" wording; the agent benefits from knowing it's
        # looking at the appendix, not body.
        if selected.total_pages > 1:
            banner = (
                f"> **Appendix page {selected.page} of {selected.total_pages}** — "
                f"structural metadata for this document.\n\n---\n\n"
            )
            if is_cli:
                cmd = f"adeu extract {file_path} --mode appendix --page {selected.page + 1}"
                footer = (
                    (
                        f"\n\n---\n\n> **Continues on appendix page {selected.page + 1} "
                        f"of {selected.total_pages}.** Run `{cmd}` for the next page."
                    )
                    if selected.has_next
                    else ""
                )
            else:
                footer = (
                    (f"\n\n---\n\n> **Continues on appendix page {selected.page + 1} of {selected.total_pages}.**")
                    if selected.has_next
                    else ""
                )
        else:
            banner = "> **Appendix** — structural metadata for this document.\n\n---\n\n"
            footer = ""

        ui_markdown = banner + selected.page_content + footer
        llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def _parse_com_header(slice_text: str) -> tuple[str, str, int]:
    m1 = re.match(r"^\s*(.*?)\s*@\s*(\d{4}\S*):(?=\s|\Z)\s*(.*)$", slice_text, re.DOTALL)
    if m1:
        author = m1.group(1).strip()
        body = m1.group(3)
        delim_offset = len(slice_text) - len(body)
        return author, body, delim_offset

    m2 = re.match(r"^\s*(?:(.*?):(?=\s|\Z)\s*|:\s*)(.*)$", slice_text, re.DOTALL)
    if m2:
        raw_author = m2.group(1)
        body = m2.group(2)
        delim_offset = len(slice_text) - len(body)
        author = raw_author.strip() if raw_author else ""
        return author, body, delim_offset

    return "", slice_text.strip(), -1


def fields_discovery_hint(file_path: str, is_cli: bool = False) -> str:
    """The surface-aware pointer at the fields ledger (spec-projection §7).

    Surface-aware for the QA F11 reason: telling an MCP client to run a shell
    command, or a CLI user to call a tool, is advice they cannot act on.
    """
    if is_cli:
        return f" \u00b7 run `adeu extract {file_path} --mode fields` for the field ledger"
    return ' \u00b7 read mode="fields" for the field ledger'


def build_fields_response(
    doc: Any,
    text: str,
    file_path: str,
    offset: int = 0,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    no_chrome: bool = False,
) -> BuilderResult:
    """Render ``mode="fields"`` — the content-control ledger (spec §2-§4).

    ``text`` must be the RAW projection: the ledger previews values by reading
    the text between a control's anchors, so a clean view (which drops the
    placeholder bubbles) would report a different document than the one the
    agent edits.
    """
    body, _appendix = split_structural_appendix(text)
    pag_res = pagination_result if pagination_result is not None else paginate(body, structural_appendix="")

    entries = collect_fields(doc, body, pag_res.body_page_offsets)
    protection = read_document_protection(doc)
    ledger = render_ledger(os.path.basename(file_path) or file_path, entries, protection, offset=offset)

    if no_chrome:
        llm_content = ledger
    else:
        llm_content = f"> **File Path:** `{file_path}`\n\n{ledger}"

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ledger,
            "title": os.path.basename(file_path),
            "file_path": file_path,
        },
    )


def build_changes_response(
    text: str,
    file_path: str,
    comments_data: dict | None = None,
    author_filter: str | None = None,
    page: int | str | None = None,
    offset: int = 0,
    is_cli: bool = False,
    pagination_result: "PaginationResult | None" = None,
    existing_change_ids: Iterable[str] | None = None,
    no_chrome: bool = False,
) -> BuilderResult:
    """
    Enumerates every tracked change and comment in a DOCX document as a concise
    ledger (<=18 tokens/change on average).
    """
    if offset < 0:
        offset = 0

    body, _appendix = split_structural_appendix(text)
    pag_res = pagination_result if pagination_result is not None else paginate(body, structural_appendix="")
    page_offsets = pag_res.body_page_offsets
    total_pages = pag_res.total_pages

    chg_entries: dict[str, _LedgerEntry] = {}
    com_entries: dict[str, _LedgerEntry] = {}
    pair_map: dict[str, list[str]] = defaultdict(list)

    TAG_RE = re.compile(r"\[(Chg|Com):(\w+)(?:\s+(insert|delete|format))?\]")

    for m in re.finditer(r"\{>>(.*?)<<\}", body, re.DOTALL):
        b_start = m.start()
        p_num = _offset_to_page(b_start, page_offsets)
        bubble_raw = m.group(1).strip()

        pre = body[max(0, b_start - 100000) : b_start]
        wrappers = list(re.finditer(r"(\{\+\+|\{--|\{==)(.*?)(?:\+\+\}|--\}|==\})", pre, re.DOTALL))
        all_ins_snips = [wm.group(2) for wm in wrappers if wm.group(1) == "{++"]
        all_del_snips = [wm.group(2) for wm in wrappers if wm.group(1) == "{--"]
        all_fmt_snips = [wm.group(2) for wm in wrappers if wm.group(1) == "{=="]

        tag_matches = list(TAG_RE.finditer(bubble_raw))
        if not tag_matches:
            continue

        first_com_delim_pos = float("inf")
        for tm in tag_matches:
            if tm.group(1) == "Com":
                slice_after = bubble_raw[tm.end() :]
                _auth, _body, d_off = _parse_com_header(slice_after)
                if d_off != -1:
                    first_com_delim_pos = tm.end() + d_off
                    break

        header_tokens = []
        for tm in tag_matches:
            kind = tm.group(1)
            if kind == "Com":
                header_tokens.append(tm)
            elif kind == "Chg":
                if tm.start() <= first_com_delim_pos:
                    header_tokens.append(tm)
                else:
                    # Inside a comment body a [Chg:N] tag is only a header when it
                    # opens its own line; mid-line mentions are prose and stay put.
                    line_start = bubble_raw.rfind("\n", 0, tm.start()) + 1
                    if not bubble_raw[line_start : tm.start()].strip():
                        header_tokens.append(tm)

        if not header_tokens:
            continue

        parsed_chg_items = []
        parsed_com_items = []

        for i, tm in enumerate(header_tokens):
            kind = tm.group(1)
            cid = tm.group(2)
            raw_type = tm.group(3)

            next_start = header_tokens[i + 1].start() if i + 1 < len(header_tokens) else len(bubble_raw)
            token_slice = bubble_raw[tm.end() : next_start]

            if kind == "Chg":
                rest_text = token_slice.strip()
                parsed_chg_items.append(
                    {
                        "kind": "Chg",
                        "cid": cid,
                        "raw_type": raw_type,
                        "rest": rest_text,
                    }
                )
            elif kind == "Com":
                c_author, c_body, _ = _parse_com_header(token_slice)
                parsed_com_items.append(
                    {
                        "kind": "Com",
                        "cid": cid,
                        "parsed_author": c_author,
                        "body_text": c_body.strip(),
                    }
                )

        shared_chg_rest = next((item["rest"] for item in reversed(parsed_chg_items) if item["rest"]), "")
        for item in parsed_chg_items:
            if not item["rest"]:
                item["rest"] = shared_chg_rest

            raw_type = item["raw_type"]
            if raw_type == "delete":
                change_type = "del"
            elif raw_type == "insert":
                change_type = "ins"
            elif raw_type == "format":
                change_type = "fmt"
            else:
                change_type = "del" if all_del_snips else ("fmt" if all_fmt_snips else "ins")
            item["change_type"] = change_type

        parsed_items = parsed_chg_items + parsed_com_items

        N_del = sum(1 for item in parsed_chg_items if item["change_type"] == "del")
        N_ins = sum(1 for item in parsed_chg_items if item["change_type"] == "ins")
        N_fmt = sum(1 for item in parsed_chg_items if item["change_type"] == "fmt")

        bubble_del_snips = all_del_snips[-N_del:] if (N_del > 0 and len(all_del_snips) >= N_del) else all_del_snips
        bubble_ins_snips = all_ins_snips[-N_ins:] if (N_ins > 0 and len(all_ins_snips) >= N_ins) else all_ins_snips
        bubble_fmt_snips = all_fmt_snips[-N_fmt:] if (N_fmt > 0 and len(all_fmt_snips) >= N_fmt) else all_fmt_snips

        del_idx = 0
        ins_idx = 0
        fmt_idx = 0

        for item in parsed_items:
            kind = item["kind"]
            cid = item["cid"]

            if kind == "Chg":
                rest = item["rest"]
                change_type = item["change_type"]
                if change_type == "del":
                    raw_snip = (
                        bubble_del_snips[del_idx]
                        if del_idx < len(bubble_del_snips)
                        else (bubble_del_snips[-1] if bubble_del_snips else "")
                    )
                    del_idx += 1
                elif change_type == "ins":
                    raw_snip = (
                        bubble_ins_snips[ins_idx]
                        if ins_idx < len(bubble_ins_snips)
                        else (bubble_ins_snips[-1] if bubble_ins_snips else "")
                    )
                    ins_idx += 1
                elif change_type == "fmt":
                    raw_snip = (
                        bubble_fmt_snips[fmt_idx]
                        if fmt_idx < len(bubble_fmt_snips)
                        else (bubble_fmt_snips[-1] if bubble_fmt_snips else "")
                    )
                    fmt_idx += 1
                else:
                    raw_snip = ""

                if not raw_snip:
                    tag_map = {"del": ("{--", "--}"), "ins": ("{++", "++}"), "fmt": ("{==", "==}")}
                    open_tag, close_tag = tag_map.get(change_type, ("{--", "--}"))
                    tag_open = body.rfind(open_tag, 0, b_start)
                    if tag_open != -1:
                        tag_close = body.find(close_tag, tag_open, b_start)
                        if tag_close != -1:
                            raw_snip = body[tag_open + len(open_tag) : tag_close]
                        else:
                            raw_snip = body[tag_open + len(open_tag) : b_start]

                clean_snip = re.sub(r"\s+", " ", raw_snip).strip()
                if len(clean_snip) > 48:
                    clean_snip = clean_snip[:45] + "..."

                pair_match = re.search(r"\(pairs\s+(?:with\s+)?((?:Chg:\w+(?:,\s*)?)+)\)", rest)
                if pair_match:
                    partner_cids = [m.group(1) for m in re.finditer(r"Chg:(\w+)", pair_match.group(1))]
                    bubble_cids = [it["cid"] for it in parsed_chg_items]
                    if cid in partner_cids:
                        non_partner_cids = [c for c in bubble_cids if c not in partner_cids]
                        src_cid = non_partner_cids[0] if non_partner_cids else cid
                    else:
                        src_cid = cid

                    for pid in partner_cids:
                        if pid != src_cid:
                            if pid not in pair_map[src_cid]:
                                pair_map[src_cid].append(pid)
                            if src_cid not in pair_map[pid]:
                                pair_map[pid].append(src_cid)

                author = re.sub(r"\s*\((?:pairs(?:\s+with)?|reply\s+to)\s+.*?\)", "", rest).strip()
                author = author or "Unknown"

                if existing_change_ids is not None:
                    if cid not in existing_change_ids and f"Chg:{cid}" not in existing_change_ids:
                        continue

                if cid not in chg_entries:
                    chg_entries[cid] = _LedgerEntry(
                        kind="chg",
                        cid=cid,
                        change_type=change_type,
                        author=author,
                        page=p_num,
                        snippet=clean_snip,
                        position=b_start,
                    )

            elif kind == "Com":
                cdata = None
                if comments_data:
                    cdata = (
                        comments_data.get(cid)
                        or comments_data.get(f"Com:{cid}")
                        or (comments_data.get(int(cid)) if cid.isdigit() else None)
                    )

                if cdata is not None:
                    author = cdata.get("author") or "Unknown"
                    raw_comm = cdata.get("text", "")
                    parent_id = cdata.get("parent_id")
                else:
                    parsed_author = item.get("parsed_author", "")
                    raw_comm = item.get("body_text", "")
                    reply_match = re.search(r"\(reply\s+to\s+(Com:\w+|\w+)\)", parsed_author)
                    parent_id = reply_match.group(1) if reply_match else None
                    author = parsed_author

                author = re.sub(r"\s*\((?:pairs(?:\s+with)?|reply\s+to)\s+.*?\)", "", author).strip() or "Unknown"

                clean_comm = re.sub(r"\s+", " ", raw_comm).strip()
                if len(clean_comm) > 120:
                    clean_comm = clean_comm[:117] + "..."

                reply_to = (
                    (str(parent_id) if str(parent_id).startswith("Com:") else f"Com:{parent_id}") if parent_id else None
                )

                if cid not in com_entries:
                    com_entries[cid] = _LedgerEntry(
                        kind="com",
                        cid=cid,
                        change_type="",
                        author=author,
                        page=p_num,
                        snippet=clean_comm,
                        reply_to_id=reply_to,
                        position=b_start,
                    )

    if comments_data:
        for cid, cdata in comments_data.items():
            str_cid = str(cid).removeprefix("Com:")
            if str_cid not in com_entries:
                author = cdata.get("author") or "Unknown"
                author = re.sub(r"\s*\((?:pairs(?:\s+with)?|reply\s+to)\s+.*?\)", "", author).strip() or "Unknown"
                raw_comm = cdata.get("text", "")
                clean_comm = re.sub(r"\s+", " ", raw_comm).strip()
                if len(clean_comm) > 120:
                    clean_comm = clean_comm[:117] + "..."
                parent_id = cdata.get("parent_id")
                reply_to = (
                    (str(parent_id) if str(parent_id).startswith("Com:") else f"Com:{parent_id}") if parent_id else None
                )
                com_entries[str_cid] = _LedgerEntry(
                    kind="com",
                    cid=str_cid,
                    change_type="",
                    author=author,
                    page=1,
                    snippet=clean_comm,
                    reply_to_id=reply_to,
                    position=999999,
                )

    if existing_change_ids is not None:
        for raw_id in existing_change_ids:
            clean_cid = str(raw_id).removeprefix("Chg:")
            if clean_cid not in chg_entries:
                chg_entries[clean_cid] = _LedgerEntry(
                    kind="chg",
                    cid=clean_cid,
                    change_type="del",
                    author="Unknown",
                    page=1,
                    snippet="",
                    position=999999,
                )

    for cid, e in chg_entries.items():
        if cid in pair_map:
            e.pair_ids = pair_map[cid]
        if existing_change_ids is not None:
            e.pair_ids = [
                pid for pid in e.pair_ids if pid in existing_change_ids or f"Chg:{pid}" in existing_change_ids
            ]

    def _sort_key(e: _LedgerEntry):
        num_id = int(e.cid) if e.cid.isdigit() else 0
        return (e.position, e.kind, num_id)

    all_entries = sorted(list(chg_entries.values()) + list(com_entries.values()), key=_sort_key)

    filtered = all_entries
    if author_filter:
        af = author_filter.strip().lower()
        filtered = [e for e in filtered if af in e.author.lower()]

    if page is not None and str(page).lower() != "all":
        if isinstance(page, tuple):
            kind, p_val = "range", page
        else:
            try:
                kind, p_val = parse_page_arg(page)
            except ValueError as err:
                raise BuilderError(str(err)) from err

        if kind == "range":
            assert isinstance(p_val, tuple)
            start_p, end_p = p_val
            if start_p < 1 or end_p < 1 or start_p > total_pages:
                raise BuilderError(f"Page {start_p} out of range (doc has {total_pages} pages).")
            filtered = [e for e in filtered if start_p <= e.page <= end_p]
        elif kind == "single":
            assert isinstance(p_val, int)
            if p_val < 1 or p_val > total_pages:
                raise BuilderError(f"Page {p_val} out of range (doc has {total_pages} pages).")
            filtered = [e for e in filtered if e.page == p_val]

    total_changes = sum(1 for e in filtered if e.kind == "chg")
    total_comments = sum(1 for e in filtered if e.kind == "com")

    dist: dict[int, int] = {}
    for e in filtered:
        dist[e.page] = dist.get(e.page, 0) + 1

    dist_str = ", ".join(f"p{p}: {dist[p]}" for p in sorted(dist.keys())) if dist else "none"

    authors = sorted(list({e.author for e in filtered if e.author}))
    authors_str = ", ".join(authors) if authors else "None"

    header = (
        f"> **Changes ledger** — {total_changes} change(s), {total_comments} comment(s) across {total_pages} page(s).\n"
        f"> Distribution — {dist_str}\n"
        f"> Authors — {authors_str}\n\n"
    )

    total_entries = len(filtered)
    slice_entries = filtered[offset : offset + 300]

    lines = []
    for e in slice_entries:
        if e.kind == "chg":
            if e.pair_ids:
                sorted_pids = sorted(e.pair_ids, key=lambda x: (int(x) if x.isdigit() else 0, x))
                pair_suffix = f"  (pairs {', '.join(f'Chg:{pid}' for pid in sorted_pids)})"
            else:
                pair_suffix = ""
            line = f'Chg:{e.cid}  {e.change_type}  {e.author}  p{e.page}  "{e.snippet}"{pair_suffix}'
        else:
            reply_suffix = f"  (reply to {e.reply_to_id})" if e.reply_to_id else ""
            line = f'Com:{e.cid}  {e.author}  p{e.page}  "{e.snippet}"{reply_suffix}'
        lines.append(line)

    continuation = ""
    if offset + 300 < total_entries:
        next_offset = offset + 300
        if is_cli:
            cli_parts = [f"adeu extract {file_path}", "--mode changes"]
            if author_filter:
                cli_parts.append(f'--changes-author "{author_filter}"')
            if page is not None:
                cli_parts.append(f"--page {page}")
            cli_parts.append(f"--changes-offset {next_offset}")
            cmd_str = " ".join(cli_parts)
            continuation = (
                f"\n\n> **Showing entries {offset + 1}-{offset + len(slice_entries)} of {total_entries}.** "
                f"Continue with `{cmd_str}`."
            )
        else:
            mcp_args = []
            if file_path:
                mcp_args.append(f'file_path="{file_path}"')
            mcp_args.append('mode="changes"')
            if author_filter:
                mcp_args.append(f'changes_author="{author_filter}"')
            if page is not None:
                if isinstance(page, str):
                    mcp_args.append(f'page="{page}"')
                else:
                    mcp_args.append(f"page={page}")
            mcp_args.append(f"changes_offset={next_offset}")
            args_str = ", ".join(mcp_args)
            continuation = (
                f"\n\n> **Showing entries {offset + 1}-{offset + len(slice_entries)} of {total_entries}.** "
                f"Continue with `read_docx({args_str})`."
            )

    if no_chrome:
        # The ledger lines ARE the payload here, so chrome-stripping normally
        # leaves them alone. With nothing to list (clean document, a filter
        # matching no entry, or an offset past the last one) the counts are the
        # only answer there is: emit them as a bare line, never an empty
        # response (QA 2026-08-12: `--mode changes --no-chrome` returned "").
        ui_markdown = "\n".join(lines) or f"{total_changes} change(s), {total_comments} comment(s)"
        llm_content = ui_markdown
    else:
        ui_markdown = header + "\n".join(lines) + continuation
        llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )
