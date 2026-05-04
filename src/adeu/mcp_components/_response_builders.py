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
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, List

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

from adeu.outline import extract_outline
from adeu.pagination import paginate, split_structural_appendix

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject


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


def render_outline_tree(nodes: List[Any]) -> str:
    """
    Renders a flat list of OutlineNode objects as a Markdown tree.
    """
    if not nodes:
        return "# (No headings detected)\n\nThis document has no detectable headings."

    lines = []
    for node in nodes:
        prefix = "#" * node.level
        meta_parts = [f"p{node.page}", node.style]
        if node.has_table:
            meta_parts.append("has table")
        if node.footnote_ids:
            meta_parts.append("fn:" + ",".join(node.footnote_ids))
        meta = ", ".join(meta_parts)
        lines.append(f"{prefix} {node.text} ({meta})")
    return "\n".join(lines)


def build_paginated_response(text: str, page: int, file_path: str) -> ToolResult:
    """
    Splits projected Markdown into pages and returns the requested page.

    Raises ToolError if `page` is out of range.
    """
    body, appendix = split_structural_appendix(text)
    result = paginate(body, structural_appendix=appendix)

    if page < 1 or page > result.total_pages:
        raise ToolError(f"Page {page} out of range (doc has {result.total_pages} pages).")

    selected = result.pages[page - 1]

    # Build the original UI markdown
    banner = _build_page_banner(selected.page, selected.total_pages)
    footer = _build_page_footer(selected.page, selected.total_pages, selected.has_next)
    ui_markdown = banner + selected.page_content + footer

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


def build_outline_response(doc: "DocumentObject", projected_text: str, file_path: str) -> ToolResult:
    """
    Returns a structural map of headings as a Markdown tree.
    """

    body, appendix = split_structural_appendix(projected_text)
    pagination_result = paginate(body, structural_appendix=appendix)

    nodes = extract_outline(
        doc,
        body,
        pagination_result.body_pages,
        pagination_result.body_page_offsets,
    )
    rendered = render_outline_tree(nodes)

    # Build the original UI markdown
    header = (
        f"> **Outline view** — {len(nodes)} headings across "
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
