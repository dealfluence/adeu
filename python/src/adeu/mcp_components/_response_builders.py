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
    reads — `markdown`, `title`, `file_path`. Everything else has been removed
    because nothing consumes it.

APPENDIX SEPARATION (Step 2)
----------------------------
The Structural Appendix (defined terms, anchors, diagnostics) is NOT included
in body pages. It is fetched on demand via mode='appendix'. Body pages get a
small one-line footer pointing the agent at the appendix mode. This was
necessary because on large legal documents the appendix can exceed 400KB,
which (a) blew the per-page payload ceiling and (b) was being silently
chunked by the MCP client.

The appendix is paginated using the same paginator as the body, with the
appendix text passed AS the body input.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, List

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

from adeu.outline import extract_outline
from adeu.pagination import paginate, split_structural_appendix

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject


def _build_appendix_pointer(file_path: str, has_appendix: bool) -> str:
    """
    Returns the one-line footer appended to body pages telling the agent that
    structural metadata (defined terms, cross-references, bookmarks, diagnostics)
    is available via mode='appendix'. Empty string when the document has no
    appendix content.

    This footer is the agent's signal that it should consult the appendix
    before making edits that touch defined terms or referenced sections.
    """
    if not has_appendix:
        return ""
    return (
        "\n\n---\n\n"
        "> **Appendix available.** This document has structural metadata "
        "(defined terms, cross-references, bookmarks, diagnostics) that may "
        "be relevant when editing. Call `read_docx` with `mode='appendix'` "
        "to load it before submitting edits."
    )


def _build_page_banner(page: int, total: int) -> str:
    """
    Returns the top-of-page banner injected into LLM-facing markdown.
    Empty string when there is only one page (no navigation needed).
    """
    if total <= 1:
        return ""
    return (
        f"> **Page {page} of {total}** — "
        f"call `read_docx` with `mode='outline'` for a heading map of the full document.\n\n"
        f"---\n\n"
    )


def _build_page_footer(page: int, total: int, has_next: bool) -> str:
    """
    Returns the bottom-of-page continuation marker. Empty when this is the
    last page or the only page.
    """
    if total <= 1 or not has_next:
        return ""
    return f"\n\n---\n\n> **Continues on page {page + 1} of {total}.**"


def render_outline_tree(
    nodes: List[Any],
    max_level: int = 2,
    verbose: bool = False,
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
        return (
            f"# (No headings at level <= {max_level})\n\n"
            f"Document has {len(nodes)} headings, all at deeper levels. "
            f"Call read_docx with mode='outline' and outline_max_level=N "
            f"(up to 6) to see them."
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
            lines.append(f"{prefix} {node.text} (p{node.page})")
    return "\n".join(lines)


def build_paginated_response(text: str, page: int, file_path: str) -> ToolResult:
    """
    Splits projected Markdown into pages and returns the requested page.

    The structural appendix is NOT included in the page content (since Step 2).
    Body pages get a one-line footer pointing the agent at mode='appendix'
    if the document has an appendix.

    Raises ToolError if `page` is out of range.
    """
    body, appendix = split_structural_appendix(text)
    has_appendix = bool(appendix.strip())

    # Paginate body only. Pass empty string as structural_appendix so the
    # paginator does not glue anything onto each page.
    result = paginate(body, structural_appendix="")

    if page < 1 or page > result.total_pages:
        raise ToolError(f"Page {page} out of range (doc has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    # Build the original UI markdown
    banner = _build_page_banner(selected.page, selected.total_pages)
    footer = _build_page_footer(selected.page, selected.total_pages, selected.has_next)
    appendix_pointer = _build_appendix_pointer(file_path, has_appendix)
    ui_markdown = banner + selected.page_content + footer + appendix_pointer

    # Prepend the path ONLY for the LLM
    llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return ToolResult(
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
) -> ToolResult:
    """
    Returns a structural map of headings as a Markdown tree.

    Args:
        outline_max_level: cap on heading depth shown (default 2).
        outline_verbose: include per-node style/table/footnote metadata.
        paragraph_offsets: when provided, enables the fast outline path that
            avoids re-projecting paragraphs. Caller obtains this from
            _extract_text_from_doc(return_paragraph_offsets=True).
    """

    # Pagination is used here only to compute body page boundaries for
    # heading->page mapping. We deliberately pass empty string instead of the
    # appendix because per-page appendix injection is gone (Step 2).
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
    )

    visible_count = sum(1 for n in nodes if n.level <= outline_max_level)
    deeper_count = len(nodes) - visible_count
    deeper_hint = f" ({deeper_count} more at deeper levels, raise outline_max_level to see)" if deeper_count > 0 else ""

    # Build the original UI markdown
    header = (
        f"> **Outline view** — showing {visible_count} of {len(nodes)} headings "
        f"(L1-L{outline_max_level}{deeper_hint}) across "
        f"{pagination_result.total_pages} page(s). "
        f"Call `read_docx` with `mode='full'` and `page=N` to read a section.\n\n"
        f"---\n\n"
    )
    ui_markdown = header + rendered
    # Prepend the path ONLY for the LLM
    llm_content = f"> **File Path:** `{file_path}`\n\n{ui_markdown}"

    return ToolResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )


def build_appendix_response(text: str, page: int, file_path: str) -> ToolResult:
    """
    Returns the structural appendix (defined terms, anchors, diagnostics) for
    the document, paginated. The appendix is treated AS the body for pagination
    purposes — same paginator, same boundary safety, same per-page banner.

    The agent fetches this on demand to inform editing decisions on documents
    where the body pages flag an appendix exists.

    Raises ToolError if `page` is out of range.
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
        return ToolResult(
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
        raise ToolError(f"Appendix page {page} out of range (appendix has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    # Build the appendix-specific banner. Reusing _build_page_banner would emit
    # generic "Page N of M" wording; the agent benefits from knowing it's
    # looking at the appendix, not body.
    if selected.total_pages > 1:
        banner = (
            f"> **Appendix page {selected.page} of {selected.total_pages}** — "
            f"structural metadata for this document.\n\n---\n\n"
        )
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

    return ToolResult(
        content=llm_content,
        structured_content={
            "markdown": ui_markdown,
            "title": Path(file_path).name,
            "file_path": str(Path(file_path).resolve()),
        },
    )
