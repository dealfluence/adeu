# FILE: src/adeu/pagination.py
"""
Stateless paginator for projected DOCX Markdown.

Splits the post-projection Markdown body into virtual pages of <= PAGE_TARGET_CHARS,
respecting CriticMarkup boundaries, table integrity, and footnote sections.

Pagination is computed deterministically on every call — no caching, no state.
The Structural Appendix (if present) is appended to every page.

Used by the read_docx MCP tool's pagination mode and indirectly by outline mode
(which needs body_pages + body_page_offsets to map heading positions to page numbers).
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

PAGE_TARGET_CHARS = 19_000
APPENDIX_MARKER = "<!-- READONLY_BOUNDARY_START -->"

# CriticMarkup open-token -> close-token. Order matters in scanning: longer/more-specific
# tokens are checked first. All four open tokens here are 3 chars, so dict order is fine.
_CRITIC_TOKENS: Dict[str, str] = {
    "{++": "++}",
    "{--": "--}",
    "{==": "==}",
    "{>>": "<<}",
}

# Note: this regex may match the literal string "Chg:42" appearing inside user-authored
# document text (e.g. quoted code, sample data). In practice this is vanishingly rare
# in real documents and accepting the false-positive risk is cheaper than parsing
# the meta-block syntax. See plan Concern 4.
_CHG_ID_PATTERN = re.compile(r"\bChg:(\d+)\b")


@dataclass
class PageInfo:
    page: int
    total_pages: int
    has_next: bool
    has_prev: bool
    tracked_change_count: int
    page_content: str


@dataclass
class PaginationResult:
    pages: List[PageInfo]
    total_pages: int
    # Pre-appendix-injection page bodies, in order. Used by outline.py to compute
    # heading -> page mapping without contending with appendix offsets.
    body_pages: List[str] = field(default_factory=list)
    # Start offset (in the original markdown_body string) at which each body page
    # begins. body_page_offsets[i] corresponds to body_pages[i]. Always 1 entry per
    # body_page; body_page_offsets[0] is always 0.
    body_page_offsets: List[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def split_structural_appendix(markdown: str) -> Tuple[str, str]:
    """
    Splits the projected Markdown into (body, appendix).

    The appendix begins at the line containing APPENDIX_MARKER and includes
    everything from that line onward. The body is everything before it, with
    trailing whitespace stripped so we don't carry a "\\n\\n---\\n\\n" tail
    that domain.py emits before the marker.
    """
    if not markdown:
        return "", ""

    idx = markdown.find(APPENDIX_MARKER)
    if idx == -1:
        return markdown, ""

    # Walk back to the start of the line containing the marker.
    line_start = markdown.rfind("\n", 0, idx) + 1  # rfind returns -1 -> +1 = 0

    body = markdown[:line_start].rstrip()
    appendix = markdown[line_start:]
    return body, appendix


def paginate(markdown_body: str, structural_appendix: str = "") -> PaginationResult:
    """
    Splits the body Markdown into virtual pages, never breaking inside a protected
    block (CriticMarkup, tables, footnote sections).

    Args:
        markdown_body: post-projection Markdown WITHOUT the structural appendix.
        structural_appendix: read-only metadata block to append to every page.

    Returns:
        PaginationResult with both finalized PageInfo objects (body+appendix) and
        the pre-injection body_pages with their original-body start offsets.
    """
    # Empty input -> single empty page (or appendix-only page if appendix is non-empty).
    if not markdown_body:
        appendix_clean = structural_appendix.strip() if structural_appendix else ""
        content = appendix_clean
        return PaginationResult(
            pages=[
                PageInfo(
                    page=1,
                    total_pages=1,
                    has_next=False,
                    has_prev=False,
                    tracked_change_count=_count_tracked_changes(content),
                    page_content=content,
                )
            ],
            total_pages=1,
            body_pages=[""],
            body_page_offsets=[0],
        )

    block_records = _tokenize_into_atomic_blocks(markdown_body)
    body_pages, body_page_offsets = _assemble_pages(block_records)

    # Inject appendix on every page.
    if structural_appendix and structural_appendix.strip():
        appendix = structural_appendix.strip()
        final_pages: List[str] = []
        for body_page in body_pages:
            if body_page:
                final_pages.append(f"{body_page}\n\n{appendix}")
            else:
                final_pages.append(appendix)
    else:
        final_pages = list(body_pages)

    total = len(final_pages)
    page_infos = [
        PageInfo(
            page=i,
            total_pages=total,
            has_next=(i < total),
            has_prev=(i > 1),
            tracked_change_count=_count_tracked_changes(content),
            page_content=content,
        )
        for i, content in enumerate(final_pages, start=1)
    ]

    return PaginationResult(
        pages=page_infos,
        total_pages=total,
        body_pages=body_pages,
        body_page_offsets=body_page_offsets,
    )


# ---------------------------------------------------------------------------
# Internal: block tokenization
# ---------------------------------------------------------------------------


def _tokenize_into_atomic_blocks(markdown_body: str) -> List[Tuple[str, int]]:
    """
    Splits markdown_body into atomic blocks. Returns a list of (block_text, start_offset)
    tuples where start_offset is the index in markdown_body where the block begins.

    A block boundary is a "\\n\\n" run where every CriticMarkup wrapper has depth 0.
    Multiple consecutive newlines collapse into a single boundary.

    After raw splitting, post-processing merges:
      a. {==X==}{>>Y<<} pairs joined directly (no whitespace).
         (In practice ingest.py never emits a "\\n\\n" between these — they live in
         the same paragraph — but we guard defensively.)
      b. Footnote/Endnote section headers ("## Footnotes" / "## Endnotes") with all
         subsequent footnote definitions until the next non-footnote block.
    """
    raw_blocks = _split_on_safe_paragraph_breaks(markdown_body)
    merged = _merge_footnote_sections(raw_blocks)
    return merged


def _split_on_safe_paragraph_breaks(text: str) -> List[Tuple[str, int]]:
    """
    Walks the string tracking CriticMarkup nesting depth. Splits on "\\n\\n" only
    when all wrapper counters are at zero. Collapses runs of >= 2 newlines into a
    single boundary so we don't emit empty blocks.

    Returns list of (block_text, start_offset) where start_offset is the position
    of block_text in the original `text`.
    """
    counters = {close: 0 for close in _CRITIC_TOKENS.values()}
    blocks: List[Tuple[str, int]] = []
    block_start = 0
    i = 0
    n = len(text)

    while i < n:
        # Try to match an open token first.
        matched_open = False
        for open_tok, close_tok in _CRITIC_TOKENS.items():
            if text.startswith(open_tok, i):
                counters[close_tok] += 1
                i += len(open_tok)
                matched_open = True
                break
        if matched_open:
            continue

        # Try to match a close token.
        matched_close = False
        for close_tok in _CRITIC_TOKENS.values():
            if text.startswith(close_tok, i):
                if counters[close_tok] > 0:
                    counters[close_tok] -= 1
                # If unbalanced, still consume so we don't loop forever.
                i += len(close_tok)
                matched_close = True
                break
        if matched_close:
            continue

        # Check for a paragraph break.
        if text[i] == "\n" and i + 1 < n and text[i + 1] == "\n":
            if all(c == 0 for c in counters.values()):
                # Valid boundary. Emit current block.
                block_text = text[block_start:i]
                if block_text:
                    blocks.append((block_text, block_start))

                # Skip all consecutive newlines (collapse multiple blank lines).
                j = i
                while j < n and text[j] == "\n":
                    j += 1
                i = j
                block_start = i
                continue

        i += 1

    # Flush trailing block.
    if block_start < n:
        block_text = text[block_start:n]
        if block_text:
            blocks.append((block_text, block_start))

    return blocks


def _merge_footnote_sections(blocks: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """
    Merges a "## Footnotes" or "## Endnotes" header block with all subsequent
    footnote/endnote definition blocks (lines starting with "[^fn-" or "[^en-").

    Footnote definitions are kept attached to their section header so they
    travel together when paginated. Page boundary may still fall *between*
    sections (e.g. body ... | ## Footnotes + defs | ## Endnotes + defs).
    """
    if not blocks:
        return blocks

    merged: List[Tuple[str, int]] = []
    i = 0
    while i < len(blocks):
        block_text, block_offset = blocks[i]
        stripped = block_text.lstrip()
        is_section_header = stripped.startswith("## Footnotes") or stripped.startswith("## Endnotes")

        if not is_section_header:
            merged.append((block_text, block_offset))
            i += 1
            continue

        # Accumulate this header plus all consecutive footnote-definition blocks.
        accumulated_text = block_text
        j = i + 1
        while j < len(blocks):
            next_text, _next_offset = blocks[j]
            next_stripped = next_text.lstrip()
            if next_stripped.startswith("[^fn-") or next_stripped.startswith("[^en-"):
                accumulated_text = f"{accumulated_text}\n\n{next_text}"
                j += 1
            else:
                break

        merged.append((accumulated_text, block_offset))
        i = j

    return merged


# ---------------------------------------------------------------------------
# Internal: page assembly (greedy)
# ---------------------------------------------------------------------------


def _assemble_pages(
    block_records: List[Tuple[str, int]],
) -> Tuple[List[str], List[int]]:
    """
    Greedy bin-packing of blocks into pages of <= PAGE_TARGET_CHARS.

    A block that exceeds PAGE_TARGET_CHARS on its own is emitted as a single
    oversized page (we never split protected content).

    Returns (page_bodies, page_start_offsets) where page_start_offsets[i] is
    the offset in the ORIGINAL markdown_body at which page_bodies[i] begins.
    """
    if not block_records:
        return [""], [0]

    pages: List[str] = []
    page_starts: List[int] = []

    current_blocks: List[str] = []
    current_size = 0
    current_start: int = -1  # offset of first block in the current page

    def flush_current():
        nonlocal current_blocks, current_size, current_start
        if current_blocks:
            pages.append("\n\n".join(current_blocks))
            page_starts.append(current_start)
        current_blocks = []
        current_size = 0
        current_start = -1

    for block_text, block_offset in block_records:
        block_size = len(block_text)
        added_size = block_size + (2 if current_blocks else 0)  # +2 for "\n\n"

        if current_blocks and current_size + added_size > PAGE_TARGET_CHARS:
            # Adding would overshoot. Flush.
            flush_current()

        if not current_blocks and block_size > PAGE_TARGET_CHARS:
            # Oversized solo block. Emit as its own page.
            pages.append(block_text)
            page_starts.append(block_offset)
            continue

        if not current_blocks:
            current_start = block_offset
        current_blocks.append(block_text)
        current_size += added_size if current_size > 0 else block_size

    flush_current()

    if not pages:
        return [""], [0]
    return pages, page_starts


# ---------------------------------------------------------------------------
# Internal: tracked change counting
# ---------------------------------------------------------------------------


def _count_tracked_changes(page_content: str) -> int:
    """Counts distinct Chg:N IDs visible on a page."""
    return len(set(_CHG_ID_PATTERN.findall(page_content)))


# ---------------------------------------------------------------------------
# UI/Formatting: Page banners, footers, and pointers
# ---------------------------------------------------------------------------


def build_page_banner(page: int, total: int, file_path: str, is_cli: bool = False) -> str:
    """
    Returns the top-of-page banner injected into LLM-facing markdown.
    Empty string when there is only one page (no navigation needed).
    """
    if total <= 1:
        return ""
    # "synthetic" is load-bearing: Adeu pages are length-based content chunks
    # sized for LLM consumption, and readers must never mistake them for
    # printed Word pages or explicit page breaks (QA 2026-07-19 ADEU-QA-005).
    if is_cli:
        cmd = f"adeu extract {file_path} --mode outline"
        return (
            f"> **Page {page} of {total}** (synthetic page — a length-based chunk, not a printed "
            f"Word page) — run `{cmd}` for a heading map of the full document.\n\n---\n\n"
        )
    return (
        f"> **Page {page} of {total}** (synthetic page — a length-based chunk, not a printed "
        f"Word page) — call `read_docx` with `mode='outline'` for a heading map of the full document.\n\n"
        f"---\n\n"
    )


def build_page_footer(page: int, total: int, has_next: bool, file_path: str, is_cli: bool = False) -> str:
    """
    Returns the bottom-of-page continuation marker. Empty when this is the
    last page or the only page.
    """
    if total <= 1 or not has_next:
        return ""
    if is_cli:
        cmd = f"adeu extract {file_path} --page {page + 1}"
        return f"\n\n---\n\n> **Continues on page {page + 1} of {total}.** Run `{cmd}` for the next page."
    return f"\n\n---\n\n> **Continues on page {page + 1} of {total}.**"


def build_appendix_pointer(file_path: str, has_appendix: bool, is_cli: bool = False) -> str:
    """
    Returns the one-line footer appended to body pages telling the agent that
    structural metadata (defined terms, cross-references, bookmarks, diagnostics)
    is available via mode='appendix'. Empty string when the document has no
    appendix content.
    """
    if not has_appendix:
        return ""
    if is_cli:
        cmd = f"adeu extract {file_path} --mode appendix --page N"
        return (
            "\n\n---\n\n"
            "> **Appendix available.** This document has structural metadata "
            "(defined terms, cross-references, bookmarks, diagnostics) that may "
            f"be relevant when editing. Run `{cmd}` "
            "to load it before submitting edits."
        )
    return (
        "\n\n---\n\n"
        "> **Appendix available.** This document has structural metadata "
        "(defined terms, cross-references, bookmarks, diagnostics) that may "
        "be relevant when editing. Call `read_docx` with `mode='appendix'` "
        "to load it before submitting edits."
    )
