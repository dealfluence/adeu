# FILE: src/adeu/mcp_components/_response_builders.py
"""
Shared response builders for read_docx mode dispatch.

Lives in mcp_components (not in tools/) because both tools/document.py and
tools/live_word.py call into it. Keeping it here avoids the circular import
that would result if these helpers lived in document.py and live_word.py
imported them at module load time.

PARITY INVARIANT
----------------
Both the disk path (_read_docx_disk) and the Live Word path
(read_active_word_document) MUST converge on these builders for outline and
pagination output. The contract is:

  Given the same DOCX content (whether read from disk or extracted via
  WordOpenXML from a saved-state Live Word doc), build_paginated_response and
  build_outline_response MUST return byte-identical results.

This invariant is what justifies the design — there is no Live-Word-specific
pagination or outline code. Any future feature that touches projection,
pagination, or outline should be added to ingest/pagination/outline so both
paths inherit the change automatically.

CHANNEL CONTRACT
----------------
Per the MCP spec, `content` is LLM-facing markdown and `structured_content` is
machine-facing JSON for the host UI. We do NOT mirror them; instead we ensure
each channel is self-sufficient for its audience:

  - `content`: contains the projected document text PLUS an inline pagination
    banner (top, and bottom-of-page when has_next) so the LLM knows its
    position in the document without consulting structured_content.

  - `structured_content`: contains only fields the markdown UI widget actually
    reads — `markdown`, `title`, `file_path`. Nothing else is consumed.

APPENDIX SEPARATION
-------------------
The Structural Appendix (defined terms, anchors, diagnostics) is NOT included
in body pages. It is fetched on demand via mode='appendix', and body pages get
a small one-line footer pointing the agent at the appendix mode. Rationale: on
large legal documents the appendix can exceed 400KB, which (a) blows the
per-page payload ceiling and (b) gets silently chunked by the MCP client.

The appendix is paginated using the same paginator as the body, with the
appendix text passed AS the body input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List

from adeu.outline import extract_outline
from adeu.pagination import (
    build_appendix_pointer,
    build_page_banner,
    build_page_footer,
    paginate,
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


def _emphasized_snippet(prefix: str, match: str, suffix: str) -> str:
    """
    Renders `prefix **match** suffix` with the document's own bold/italic
    projection markers stripped first, so the highlight cannot collide with
    markers already present — a regex match crossing styled runs used to
    render as `**The **Supplier** _shall provide**_` (QA 2026-07-19 v8 F-10).
    Markers are detected over the WHOLE region (a match boundary can cut a
    marker away from its word-edge context), then each part is rebuilt from
    the surviving characters.
    """
    region = prefix + match + suffix
    b1, b2 = len(prefix), len(prefix) + len(match)
    keep = [True] * len(region)
    for m in _STYLE_MARKER_RE.finditer(region):
        for i in range(m.start(), m.end()):
            keep[i] = False
    stripped_prefix = "".join(c for i, c in enumerate(region[:b1]) if keep[i])
    stripped_match = "".join(c for i, c in enumerate(region[b1:b2], start=b1) if keep[i])
    stripped_suffix = "".join(c for i, c in enumerate(region[b2:], start=b2) if keep[i])
    return f"{stripped_prefix}**{stripped_match}**{stripped_suffix}"


def render_outline_tree(
    nodes: List[Any],
    max_level: int = 2,
    verbose: bool = False,
    is_cli: bool = False,
    file_path: str = "document.docx",
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


def build_full_document_response(text: str, file_path: str) -> BuilderResult:
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
    llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"
    return BuilderResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_paginated_response(text: str, page: int, file_path: str, is_cli: bool = False) -> BuilderResult:
    """
    Splits projected Markdown into pages and returns the requested page.

    The structural appendix is NOT included in the page content. Body pages
    get a one-line footer pointing the agent at mode='appendix' if the
    document has an appendix.

    Raises BuilderError if `page` is out of range.
    """
    body, appendix = split_structural_appendix(text)
    has_appendix = bool(appendix.strip())

    # Paginate body only. Pass empty string as structural_appendix so the
    # paginator does not glue anything onto each page.
    result = paginate(body, structural_appendix="")

    if page < 1 or page > result.total_pages:
        raise BuilderError(f"Page {page} out of range (doc has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    # Build the original UI markdown
    banner = build_page_banner(selected.page, selected.total_pages, file_path, is_cli=is_cli)
    footer = build_page_footer(selected.page, selected.total_pages, selected.has_next, file_path, is_cli=is_cli)
    appendix_pointer = build_appendix_pointer(file_path, has_appendix, is_cli=is_cli)
    ui_markdown = banner + selected.page_content + footer + appendix_pointer

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


def build_outline_response(
    doc: "DocumentObject",
    projected_text: str,
    file_path: str,
    outline_max_level: int = 2,
    outline_verbose: bool = False,
    paragraph_offsets: dict | None = None,
    is_cli: bool = False,
) -> BuilderResult:
    """
    Returns a structural map of headings as a Markdown tree.

    Args:
        outline_max_level: cap on heading depth shown (default 2).
        outline_verbose: include per-node style/table/footnote metadata.
        paragraph_offsets: when provided, enables the fast outline path that
            avoids re-projecting paragraphs. Caller obtains this from
            _extract_text_from_doc(return_paragraph_offsets=True).
    """

    # Levels outside 1-6 are meaningless (0/negative would render a
    # nonsensical "L1-L0" range label, QA L2). The CLI rejects them at parse
    # time; clamp here so MCP callers get the nearest sensible depth.
    outline_max_level = max(1, min(outline_max_level, 6))

    # Pagination is used here only to compute body page boundaries for
    # heading->page mapping. We deliberately pass empty string instead of the
    # appendix — the appendix is never injected per page.
    body, _appendix = split_structural_appendix(projected_text)
    pagination_result = paginate(body, structural_appendix="")

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
    )

    visible_count = sum(1 for n in nodes if n.level <= outline_max_level)
    deeper_count = len(nodes) - visible_count
    deeper_hint = f" ({deeper_count} more at deeper levels, raise outline_max_level to see)" if deeper_count > 0 else ""

    if is_cli:
        read_hint = f"Run `adeu extract {file_path} --page N` to read a section."
    else:
        read_hint = "Call `read_docx` with `mode='full'` and `page=N` to read a section."

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


def build_search_response(
    text: str,
    search_query: str,
    search_regex: bool,
    search_case_sensitive: bool,
    page: int | str | None,
    file_path: str,
    is_cli: bool = False,
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
    """
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
    pag_res = paginate(body, "")
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
        ui_markdown = (
            f"> **Search Results** — No matches found for query `{search_query}` in `{Path(file_path).name}`.\n\n"
            + retry_hint
        )
        if regex_downgraded_note:
            ui_markdown = f"{regex_downgraded_note}\n\n{ui_markdown}"
        return BuilderResult(
            content=f"> **File Path:** `{file_path}`\n\n{ui_markdown}",
            structured_content={
                "markdown": ui_markdown,
                "title": f"Search: {Path(file_path).name}",
                "file_path": str(Path(file_path).resolve()),
            },
        )

    # ---- Assign each match to its document page. ----
    def _page_for_offset(offset: int) -> int:
        p_num = 1
        for j, off in enumerate(page_offsets):
            if offset >= off:
                p_num = j + 1
            else:
                break
        return p_num

    matches_with_pages = [(m, _page_for_offset(m.start())) for m in matches]
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
            ui_markdown = (
                f"> **Search Results** — No matches on document page {page_filter} "
                f"for query `{search_query}` in `{Path(file_path).name}`.\n\n"
                f"The query DOES appear elsewhere ({total_matches} match"
                f"{'es' if total_matches != 1 else ''} on page"
                f"{'s' if len(pages_with_hits) != 1 else ''} {other_pages_str}). "
                f"Omit `page` or pass `page='all'` to see them."
            )
            return BuilderResult(
                content=f"> **File Path:** `{file_path}`\n\n{ui_markdown}",
                structured_content={
                    "markdown": ui_markdown,
                    "title": f"Search: {Path(file_path).name}",
                    "file_path": str(Path(file_path).resolve()),
                },
            )

    # ---- Render. ----
    ui_parts: list[str] = []

    # Cap results to 20 to avoid LLM context overflow
    max_matches = 20
    is_truncated = len(filtered) > max_matches
    items_to_render = filtered[:max_matches]

    if page_filter is None:
        ui_parts.append(
            f"> **Search Results** — Found {total_matches} match"
            f"{'es' if total_matches != 1 else ''} for query `{search_query}` "
            f"in `{Path(file_path).name}`."
        )
        # Distribution summary only when matches span >1 document page.
        if len(pages_with_hits) > 1:
            dist_str = ", ".join(f"p{p}: {page_distribution[p]}" for p in pages_with_hits)
            ui_parts.append(f"> Distribution across {len(pages_with_hits)} document pages — {dist_str}")
        if is_truncated:
            ui_parts.append(
                f"> **Note:** Only the first {max_matches} matches are shown here to prevent LLM context overflow. "
                f"Narrow your search query or specify a `page` filter to see other matches."
            )
    else:
        shown = len(filtered)
        ui_parts.append(
            f"> **Search Results** — Found {shown} match"
            f"{'es' if shown != 1 else ''} on document page {page_filter} "
            f"for query `{search_query}` in `{Path(file_path).name}` "
            f"({total_matches} total in document)."
        )
        other_pages = [p for p in pages_with_hits if p != page_filter]
        if other_pages:
            other_pages_str = ", ".join(str(p) for p in other_pages)
            ui_parts.append(
                f"> Additional matches exist on page"
                f"{'s' if len(other_pages) != 1 else ''} {other_pages_str} — "
                f"omit `page` or pass `page='all'` to see them."
            )
        if is_truncated:
            ui_parts.append(
                f"> **Note:** Only the first {max_matches} matches are shown here to prevent LLM context overflow. "
                f"Narrow your search query or specify a `page` filter to see other matches."
            )

    def get_heading(idx, txt):
        path: list[str] = []
        current_level = 999
        # Scan through the END of the line containing the match: slicing at
        # the match offset cuts the line in half, so a hit INSIDE a heading
        # reported a truncated path ("Master" for a match on "Services" in
        # "# Master Services Agreement", QA 2026-07-19 F-17).
        line_end = txt.find("\n", idx)
        if line_end == -1:
            line_end = len(txt)
        for line in reversed(txt[:line_end].split("\n")):
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                if level < current_level:
                    clean_heading = re.sub(r"\*\*|__|[*_]", "", m.group(2))
                    clean_heading = re.sub(r"\{#[^}]+\}", "", clean_heading).strip()
                    if len(clean_heading) > 80:
                        clean_heading = clean_heading[:80] + "..."
                    path.insert(0, clean_heading)
                    current_level = level
                    if level == 1:
                        break
        return " > ".join(path) if path else ""

    # Match index is preserved from the FULL match list so an LLM that sees
    # "Match 7 (p3)" knows it is the 7th match overall, not the 7th on this page.
    full_index_map = {id(m): i + 1 for i, (m, _p) in enumerate(matches_with_pages)}

    for m, p_num in items_to_render:
        m_start, m_end = m.span()
        matched_str = m.group(0)

        snippet = _emphasized_snippet(
            body[max(0, m_start - 100) : m_start],
            matched_str,
            body[m_end : min(len(body), m_end + 100)],
        )
        snippet_lines = "\n".join(f"> {line}" for line in snippet.split("\n") if line.strip())

        idx = full_index_map[id(m)]
        ui_parts.extend(["---", f"### Match {idx} (p{p_num})"])
        if h_path := get_heading(m_start, body):
            ui_parts.append(f"**Path:** `{h_path}`")
        ui_parts.extend(
            [
                snippet_lines,
                f"*Occurrences:* This exact phrasing appears {occurrences_map[matched_str]} "
                f"time{'s' if occurrences_map[matched_str] != 1 else ''} in the document.",
            ]
        )

    if regex_downgraded_note:
        ui_parts.insert(0, regex_downgraded_note)
    ui_markdown = "\n\n".join(part for part in ui_parts if part)
    return BuilderResult(
        content=f"> **File Path:** `{file_path}`\n\n{ui_markdown}",
        structured_content={
            "markdown": ui_markdown,
            "title": f"Search: {Path(file_path).name}",
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_appendix_response(text: str, page: int, file_path: str, is_cli: bool = False) -> BuilderResult:
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
        llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"
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
