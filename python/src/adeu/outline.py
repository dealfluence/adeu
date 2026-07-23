# FILE: src/adeu/outline.py
"""
Structural outline extractor.

Walks a python-docx Document in document order, identifies headings (matching
the same rules used by ingest.get_paragraph_prefix), and emits a flat list of
OutlineNode records with per-heading metadata.

Used by read_docx mode='outline'. The outline is computed from the live
Document object — same source as the projected body — so heading text and
page assignments stay consistent with what mode='full' returns.

Heading ownership: a heading owns the document range from its position up to
(but not including) the next heading of equal or higher level. has_table and
footnote_ids are computed over that owned range.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional

from docx.document import Document as DocumentObject
from docx.table import Table
from docx.text.paragraph import Paragraph

from adeu.ingest import build_paragraph_text, extract_table
from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    get_paragraph_prefix,
    iter_block_items,
    iter_document_parts,
    iter_paragraph_content,
)


@dataclass
class OutlineNode:
    level: int  # 1–6
    text: str  # heading text, no markdown markers, no CriticMarkup
    page: int  # 1-indexed page on which this heading appears
    style: str  # "Heading 1" / "Title" / "(heuristic)" / "(outline_level)"
    has_table: bool  # True if owned range contains a Table
    footnote_ids: List[str] = field(default_factory=list)  # e.g. ["fn-3", "en-1"]
    end_page: Optional[int] = None


# ---------------------------------------------------------------------------
# Internal walk records
# ---------------------------------------------------------------------------


@dataclass
class _BlockRecord:
    """One walked block item with its projected length and start offset."""

    item: Any  # Paragraph | Table | (FootnoteItem skipped — see _walk_doc)
    is_paragraph: bool
    is_table: bool
    start_offset: int  # offset in projected body where this block starts
    projected_length: int  # length of the projected text for this block


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _direct_has_table(
    block_records: List[_BlockRecord],
    range_start: int,
    range_end: int,
) -> bool:
    """
    Returns True iff a Table appears in block_records[range_start:range_end]
    BEFORE any nested child heading. This prevents `has_table` from bubbling
    up to ancestor headings: a heading only advertises a table if it is the
    nearest heading that owns the table.

    Example:
        H1
          H2
            <table>
        # H2 has_table=True (table appears before any deeper heading)
        # H1 has_table=False (table is "claimed" by H2 first)
    """
    for idx in range(range_start, range_end):
        rec = block_records[idx]
        if rec.is_paragraph and _is_heading(rec.item):
            # We hit a child heading before finding a table — child claims any tables.
            return False
        if rec.is_table:
            return True
    return False


def extract_outline(
    doc: DocumentObject,
    projected_body: str,
    body_pages: List[str],
    body_page_offsets: List[int],
    paragraph_offsets: dict | None = None,
) -> List[OutlineNode]:
    """
    Walks the document and returns a flat list of OutlineNode records in document order.

    Args:
        doc: the python-docx Document.
        projected_body: post-projection Markdown, BODY ONLY (no appendix).
        body_pages: pre-appendix-injection page bodies, in order.
        body_page_offsets: start offset (in projected_body) of each entry in body_pages.
            Must satisfy len(body_pages) == len(body_page_offsets).
        paragraph_offsets: when provided, this is the offset
            map produced by _extract_text_from_doc(return_paragraph_offsets=True).
            Used to fast-path heading text extraction by slicing projected_body
            instead of re-projecting each paragraph. When None, falls back to the
            legacy walk that re-projects (slow on large docs).

    Returns:
        Flat list of OutlineNode. Empty if no headings detected.
    """
    if len(body_pages) != len(body_page_offsets):
        raise ValueError(
            f"body_pages ({len(body_pages)}) and body_page_offsets ({len(body_page_offsets)}) length mismatch"
        )

    # Fast path: we have authoritative paragraph offsets from the body
    # projection. Skip the legacy walk entirely.
    if paragraph_offsets is not None:
        return _extract_outline_fast(doc, projected_body, body_page_offsets, paragraph_offsets)

    # Legacy slow path (no offset map).
    # Build the comments map once — required by build_paragraph_text.
    comments_map = CommentsManager(doc).extract_comments_data()

    # 1. Walk the document body in projection order, recording each block's offset
    #    and projected length. The walk mirrors ingest._extract_blocks.
    block_records = _walk_doc_body(doc, comments_map)

    # 2. Identify headings among the records.
    heading_indices: List[int] = []
    for idx, rec in enumerate(block_records):
        if not (rec.is_paragraph and _is_heading(rec.item)):
            continue
        if not _heading_passes_quality_filter(rec.item, comments_map):
            continue
        heading_indices.append(idx)

    if not heading_indices:
        return []

    # 3. For each heading, compute owned range, metadata, and resolved page.
    nodes: List[OutlineNode] = []
    for h_pos, rec_idx in enumerate(heading_indices):
        rec = block_records[rec_idx]
        paragraph = rec.item

        level = _heading_level(paragraph)
        text = _heading_text(paragraph, comments_map)
        style = _determine_heading_style(paragraph)

        # Owned range: blocks strictly between this heading and the next equal-or-
        # higher heading.
        owned_end = _find_owned_end(block_records, heading_indices, h_pos, level)
        owned_blocks = block_records[rec_idx + 1 : owned_end]

        has_table = _direct_has_table(block_records, rec_idx + 1, owned_end)
        footnote_ids = _collect_footnote_ids(owned_blocks)

        page_num = _offset_to_page(rec.start_offset, body_page_offsets)

        nodes.append(
            OutlineNode(
                level=level,
                text=text,
                page=page_num,
                style=style,
                has_table=has_table,
                footnote_ids=footnote_ids,
            )
        )

    total_pages = len(body_pages)
    for i, node in enumerate(nodes):
        end_page = total_pages
        for j in range(i + 1, len(nodes)):
            if nodes[j].level <= node.level:
                if nodes[j].page > node.page:
                    end_page = nodes[j].page - 1
                else:
                    end_page = node.page
                break
        node.end_page = end_page

    return nodes


def _extract_outline_fast(
    doc: DocumentObject,
    projected_body: str,
    body_page_offsets: List[int],
    paragraph_offsets: dict,
) -> List[OutlineNode]:
    """
    Fast outline extraction using the pre-computed paragraph offset map.

    For each paragraph in the document, we already know its (start, length) in
    projected_body. Heading text is extracted by slicing projected_body and
    stripping markdown markers — no per-paragraph re-projection.

    has_table and footnote_ids are computed by walking the tree, which is
    fast (lxml structural traversal, not text projection). Owned-range
    determination is unchanged from the legacy path.
    """
    # Walk paragraphs and tables in projection order, but ONLY to detect
    # heading-eligible paragraphs. We do not re-project text.
    paragraphs_and_tables: list = []
    seen_cells: set = set()

    def walk(container):
        for item in iter_block_items(container):
            i_type = type(item).__name__
            if i_type == "FootnoteItem":
                walk(item)
            elif isinstance(item, Paragraph):
                paragraphs_and_tables.append(("p", item))
            elif isinstance(item, Table):
                paragraphs_and_tables.append(("t", item))
                for row in item.rows:
                    for cell in row.cells:
                        cid = id(cell._tc)
                        if cid in seen_cells:
                            continue
                        seen_cells.add(cid)
                        walk(cell)

    # Body part only — header/footer/notes don't contribute to outline.
    walk(doc)

    # Identify heading paragraphs.
    heading_indices: list[int] = []
    for idx, (kind, item) in enumerate(paragraphs_and_tables):
        if kind != "p":
            continue
        # If the paragraph has no offset, it was skipped during projection
        # (e.g. inside a deleted table row in clean_view). It cannot be a heading.
        if id(item._element) not in paragraph_offsets:
            continue
        if not _is_heading(item):
            continue
        if not _heading_passes_quality_filter_fast(item, projected_body, paragraph_offsets):
            continue
        heading_indices.append(idx)

    if not heading_indices:
        return []

    nodes: List[OutlineNode] = []
    for h_pos, item_idx in enumerate(heading_indices):
        _, paragraph = paragraphs_and_tables[item_idx]
        level = _heading_level(paragraph)
        text = _heading_text_fast(paragraph, projected_body, paragraph_offsets)
        style = _determine_heading_style(paragraph)

        # Owned range: items strictly between this heading and the next
        # equal-or-higher heading.
        owned_end = item_idx
        for next_h_pos in range(h_pos + 1, len(heading_indices)):
            next_idx = heading_indices[next_h_pos]
            next_paragraph = paragraphs_and_tables[next_idx][1]
            if _heading_level(next_paragraph) <= level:
                owned_end = next_idx
                break
        else:
            owned_end = len(paragraphs_and_tables)

        owned = paragraphs_and_tables[item_idx + 1 : owned_end]

        # has_table: nearest-claim semantics (no bubbling to ancestors).
        has_table = False
        for kind2, item2 in owned:
            if kind2 == "p" and _is_heading(item2):
                break
            if kind2 == "t":
                has_table = True
                break

        # Footnote IDs in document order, deduped.
        footnote_ids = _collect_footnote_ids_fast(owned)

        # Page resolution from the paragraph's known offset.
        para_offset = paragraph_offsets.get(id(paragraph._element))
        if para_offset is not None:
            start_offset, _length, _proxy = para_offset
            page_num = _offset_to_page(start_offset, body_page_offsets)
        else:
            page_num = 1

        nodes.append(
            OutlineNode(
                level=level,
                text=text,
                page=page_num,
                style=style,
                has_table=has_table,
                footnote_ids=footnote_ids,
            )
        )

    total_pages = len(body_page_offsets)
    for i, node in enumerate(nodes):
        end_page = total_pages
        for j in range(i + 1, len(nodes)):
            if nodes[j].level <= node.level:
                if nodes[j].page > node.page:
                    end_page = nodes[j].page - 1
                else:
                    end_page = node.page
                break
        node.end_page = end_page

    return nodes


def _heading_passes_quality_filter_fast(
    paragraph: Paragraph,
    projected_body: str,
    paragraph_offsets: dict,
) -> bool:
    """
    Fast variant of _heading_passes_quality_filter that uses the offset map
    for heuristic-promoted headings instead of calling build_paragraph_text.
    """
    style = _determine_heading_style(paragraph)
    if style != "(heuristic)":
        return True

    text = _heading_text_fast(paragraph, projected_body, paragraph_offsets)
    if not text:
        return False
    word_count = len(re.findall(r"\w+", text))
    return word_count >= _HEURISTIC_MIN_WORDS


def _heading_text_fast(
    paragraph: Paragraph,
    projected_body: str,
    paragraph_offsets: dict,
) -> str:
    """
    Fast variant of _heading_text using the offset map. Slices projected_body
    instead of re-projecting the paragraph.
    """
    offset = paragraph_offsets.get(id(paragraph._element))
    if offset is None:
        # Defensive fallback — paragraph wasn't projected (shouldn't happen
        # for body paragraphs, but might for ones in unsupported parts).
        return ""
    start, length, _proxy = offset
    raw = projected_body[start : start + length]
    cleaned = _strip_critic_markup(raw)
    cleaned = _strip_inline_formatting(cleaned)
    # The projection includes the heading prefix ("# ", "## ", ...). Strip it.
    cleaned = re.sub(r"^#+\s+", "", cleaned)
    return cleaned.strip()


def _collect_footnote_ids_fast(owned_items: list) -> List[str]:
    """
    Walks owned (kind, item) tuples, collecting footnote/endnote references
    in document order, deduplicated, with first-seen order preserved.
    """
    seen: set = set()
    ordered: List[str] = []

    for kind, item in owned_items:
        if kind != "p":
            continue
        for event in _iter_paragraph_events(item):
            if event.type == "footnote":
                fn_id = f"fn-{event.id}"
            elif event.type == "endnote":
                fn_id = f"en-{event.id}"
            else:
                continue
            if fn_id not in seen:
                seen.add(fn_id)
                ordered.append(fn_id)

    return ordered


# ---------------------------------------------------------------------------
# Internal: document walk with offset tracking
# ---------------------------------------------------------------------------
def _walk_doc_body(doc: DocumentObject, comments_map: dict) -> List[_BlockRecord]:
    """
    Walks doc in projection order, emitting one _BlockRecord per Paragraph or Table.

    PERF: We deliberately do NOT re-project blocks here. Instead we walk the body
    structure once to enumerate (paragraph|table) items, and compute offsets by
    splitting the already-projected body string on the same `\n\n` boundaries
    that ingest._extract_blocks emits. Length is taken from the split slice.

    Tables: we still descend into cells to find inner headings, but we do NOT
    measure inner-paragraph projected lengths (outline doesn't use them — it
    just needs to know which paragraphs exist for heading detection).
    """
    # Compute offset of body part inside projected_body. Header/footer parts
    # come first in iter_document_parts. We need their projected LENGTHS only,
    # not their text — but the cheapest way to know "where does the body start
    # in projected_body" is to skip past header/footer joins. Since we don't
    # have those lengths cached, we accept a one-time projection of header/
    # footer parts here. They are typically tiny vs. the body, so this is fast.
    parts = list(iter_document_parts(doc))

    body_start_offset = 0
    body_part = None
    for part in parts:
        if part is doc:
            body_part = part
            break
        part_text = _project_part(part, comments_map)
        if part_text:
            if body_start_offset > 0:
                body_start_offset += 2
            body_start_offset += len(part_text)

    if body_part is None:
        body_part = doc
        body_start_offset = 0
    else:
        if body_start_offset > 0:
            body_start_offset += 2

    # Walk body block-by-block. We compute each block's projected length lazily
    # by calling build_paragraph_text/extract_table — but ONLY for blocks that
    # we need length for. For outline purposes, the only thing that matters is
    # heading position; non-heading lengths only matter as cumulative offsets.
    #
    # Tradeoff: build_paragraph_text still runs once per body paragraph —
    # O(N) — but tables are measured by length arithmetic alone. Recursively
    # measuring them (extract_table + walking cells + build_paragraph_text
    # per inner paragraph) is the expensive path this avoids.
    records: List[_BlockRecord] = []
    cursor = body_start_offset
    is_first_block = True

    for item in iter_block_items(body_part):
        if isinstance(item, Paragraph):
            prefix = get_paragraph_prefix(item)
            p_text = build_paragraph_text(item, comments_map, clean_view=False)
            block_len = len(prefix + p_text)

            if not is_first_block:
                cursor += 2

            records.append(
                _BlockRecord(
                    item=item,
                    is_paragraph=True,
                    is_table=False,
                    start_offset=cursor,
                    projected_length=block_len,
                )
            )
            cursor += block_len
            is_first_block = False

        elif isinstance(item, Table):
            table_text = extract_table(item, comments_map, clean_view=False)
            block_len = len(table_text) if table_text else 0

            if not is_first_block:
                cursor += 2

            table_start = cursor
            records.append(
                _BlockRecord(
                    item=item,
                    is_paragraph=False,
                    is_table=True,
                    start_offset=table_start,
                    projected_length=block_len,
                )
            )
            # Record inner blocks for heading detection — but DO NOT call
            # build_paragraph_text on them (we don't need their lengths).
            _record_table_inner_blocks_lite(item, table_start, records, comments_map)
            cursor += block_len
            is_first_block = False

    return records


def _compute_inner_block_offset(
    table: Table,
    target_paragraph: Paragraph,
    table_start_offset: int,
    comments_map: dict,
) -> int:
    """
    Computes the absolute projected-text offset where `target_paragraph` begins,
    given that it lives somewhere inside `table`. Walks the table in the same
    order as ingest.extract_table:

        rows joined by "\n"
        within each row, unique cells joined by " | "
        within each cell, blocks joined by "\n\n" (recursive)

    Returns table_start_offset if target_paragraph is not found inside the table
    (defensive fallback — should never happen in practice).

    NOTE: We deliberately ignore structural-row-tracking wrappers ({++ ... ++})
    here. They affect *the position of subsequent rows* relative to the start
    of the table, but the cost of replicating that math precisely is not worth
    it for outline page resolution — pagination granularity is ~19k chars; a
    few-character drift from a tracked-row wrapper will not flip a heading to
    the wrong page.
    """
    target_el = target_paragraph._element
    cursor = table_start_offset
    rows_processed = 0

    for row in table.rows:
        # Skip rows that ingest would skip in clean_view; outline runs against
        # the non-clean projection, so do NOT skip clean_view-deleted rows here.
        # extract_table only skips a row when clean_view=True AND trPr/del exists.
        # We are projection-side (clean_view=False), so include all rows.

        if rows_processed > 0:
            if rows_processed == 1:
                first_row = table.rows[0]
                seen_cells_first = set()
                num_cols = 0
                for cell in first_row.cells:
                    cell_id = id(cell._tc)
                    if cell_id in seen_cells_first:
                        continue
                    seen_cells_first.add(cell_id)
                    num_cols += 1
                divider_len = len(" | ".join(["---"] * num_cols)) if num_cols > 0 else 0
                cursor += 1 + divider_len + 1
            else:
                cursor += 1  # "\n" between rows

        seen_cells: set = set()
        cells_in_row = 0

        for cell in row.cells:
            cell_id = id(cell._tc)
            if cell_id in seen_cells:
                continue
            seen_cells.add(cell_id)

            if cells_in_row > 0:
                cursor += 3  # " | " between cells

            # Walk this cell's blocks in projection order.
            cursor, found = _walk_cell_for_offset(cell, target_el, cursor, comments_map)
            if found:
                return cursor

            cells_in_row += 1

        rows_processed += 1

    return table_start_offset


def _walk_cell_for_offset(
    cell,
    target_el,
    cell_start_cursor: int,
    comments_map: dict,
) -> tuple[int, bool]:
    """
    Walks a single table cell's block items, accumulating offsets exactly the
    way ingest._extract_blocks does: blocks joined by "\n\n".

    Returns (offset_at_or_after_target, found_flag). When found_flag is True,
    offset_at_or_after_target is the absolute offset where target_el's
    paragraph begins.
    """
    cursor = cell_start_cursor
    is_first_block = True

    for inner_item in iter_block_items(cell):
        if not is_first_block:
            cursor += 2  # "\n\n" between blocks

        if isinstance(inner_item, Paragraph):
            if inner_item._element is target_el:
                return cursor, True
            prefix = get_paragraph_prefix(inner_item)
            p_text = build_paragraph_text(inner_item, comments_map, clean_view=False)
            cursor += len(prefix + p_text)

        elif isinstance(inner_item, Table):
            # Recurse into nested table. If the target paragraph is inside this
            # nested table, _compute_inner_block_offset returns its absolute
            # offset; otherwise it returns the table's start (which we then need
            # to skip past by adding the projected length).
            nested_offset = _compute_inner_block_offset(
                inner_item, _paragraph_from_element(target_el), cursor, comments_map
            )
            if nested_offset != cursor:
                # We "found" something deeper. Verify by checking that the
                # target_el actually lives inside this table.
                if _element_is_descendant(target_el, inner_item._element):
                    return nested_offset, True
            # Not in this nested table — skip past it.
            table_text = extract_table(inner_item, comments_map, clean_view=False)
            cursor += len(table_text) if table_text else 0

        is_first_block = False

    return cursor, False


def _paragraph_from_element(p_el):
    """
    Returns a lightweight Paragraph wrapper around p_el. Used only to satisfy
    the type signature of _compute_inner_block_offset when recursing through
    nested tables — we only ever read ._element off it.

    parent=None violates Paragraph's nominal type but is safe in practice:
    downstream code only touches ._element.
    """
    return Paragraph(p_el, None)  # type: ignore[arg-type]


def _element_is_descendant(target_el, ancestor_el) -> bool:
    """True iff target_el is contained anywhere within ancestor_el's subtree."""
    cur = target_el.getparent()
    while cur is not None:
        if cur is ancestor_el:
            return True
        cur = cur.getparent()
    return False


def _record_table_inner_blocks_lite(
    table: Table,
    inherited_offset: int,
    records: List[_BlockRecord],
    comments_map: dict,
) -> None:
    """
    PERF-optimized version of _record_table_inner_blocks: records inner
    paragraphs/tables for heading detection without paying the full projection
    cost for non-headings.

    Headings inside tables get TRUE offsets computed via _compute_inner_block_offset
    (so outline.page resolves correctly when a table spans page boundaries).
    Non-heading inner blocks keep projected_length=0 and inherit the table's
    start_offset — they are never used to resolve a page, only to scan for
    nested headings/tables.
    """
    seen_cells: set = set()
    for row in table.rows:
        for cell in row.cells:
            cell_id = id(cell._tc)
            if cell_id in seen_cells:
                continue
            seen_cells.add(cell_id)

            for inner_item in iter_block_items(cell):
                if isinstance(inner_item, Paragraph):
                    # Only pay the projection cost when the inner paragraph is
                    # actually a heading — non-heading inner paragraphs do not
                    # need a true offset (they're never assigned a page).
                    if _is_heading(inner_item):
                        true_offset = _compute_inner_block_offset(table, inner_item, inherited_offset, comments_map)
                    else:
                        true_offset = inherited_offset

                    records.append(
                        _BlockRecord(
                            item=inner_item,
                            is_paragraph=True,
                            is_table=False,
                            start_offset=true_offset,
                            projected_length=0,
                        )
                    )
                elif isinstance(inner_item, Table):
                    records.append(
                        _BlockRecord(
                            item=inner_item,
                            is_paragraph=False,
                            is_table=True,
                            start_offset=inherited_offset,
                            projected_length=0,
                        )
                    )
                    _record_table_inner_blocks_lite(inner_item, inherited_offset, records, comments_map)


def _project_part(part, comments_map: dict) -> str:
    """
    Renders a non-body document part (header/footer/notes) to measure its
    projected length. Mirrors ingest._extract_blocks but inlined here so we
    don't reach into ingest's private helpers.

    Headers and footers iterate as Paragraphs/Tables. NotesPart is iterated
    via FootnoteItem children.
    """
    blocks: List[str] = []
    c_type = type(part).__name__

    if c_type == "NotesPart":
        header = "## Footnotes" if part.note_type == "fn" else "## Endnotes"
        blocks.append(f"---\n{header}")

    is_first_para = True
    for item in iter_block_items(part):
        i_type = type(item).__name__

        if i_type == "FootnoteItem":
            fn_text = _project_part(item, comments_map)
            if fn_text:
                blocks.append(fn_text)
        elif isinstance(item, Paragraph):
            prefix = get_paragraph_prefix(item)
            if is_first_para and c_type == "FootnoteItem":
                prefix = f"[^{part.note_type}-{part.id}]: " + prefix
            p_text = build_paragraph_text(item, comments_map, clean_view=False)
            blocks.append(prefix + p_text)
            is_first_para = False
        elif isinstance(item, Table):
            table_text = extract_table(item, comments_map, clean_view=False)
            if table_text:
                blocks.append(table_text)
            is_first_para = False

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Internal: heading detection
# ---------------------------------------------------------------------------


_HEADING_PREFIX_RE = re.compile(r"^(#{1,6}) ")

# Minimum word count for a heading promoted by the all-caps-bold heuristic.
# Real section titles ("CODE OF CONDUCT", "STANDARD PROCUREMENT DOCUMENT") have
# >= 3 words. Single-word or two-word emphatic fragments ("OR", "ES-MSIP",
# decorative underscores) are noise and clutter the outline.
_HEURISTIC_MIN_WORDS = 3


def _is_heading(paragraph: Paragraph) -> bool:
    """
    A paragraph is a heading iff get_paragraph_prefix returns "# " through "###### ".
    List items ("* ") and plain paragraphs ("") are excluded by the regex anchor.
    """
    prefix = get_paragraph_prefix(paragraph)
    return bool(_HEADING_PREFIX_RE.match(prefix))


def _heading_passes_quality_filter(paragraph: Paragraph, comments_map: dict) -> bool:
    """
    Filters out low-signal headings before they reach the outline.

    Real Heading-styled paragraphs (Heading 1..6, Title) always pass — the
    document author explicitly marked them as headings, so we trust them even
    if they're short.

    Heuristic-promoted paragraphs (all-caps + bold + short, see
    get_paragraph_prefix) only pass if the cleaned heading text has at least
    _HEURISTIC_MIN_WORDS word tokens. This drops "OR", "_ _", "ES-MSIP",
    and similar fragments without affecting genuine all-caps section titles.
    """
    style = _determine_heading_style(paragraph)
    if style != "(heuristic)":
        return True

    text = _heading_text(paragraph, comments_map)
    if not text:
        return False
    word_count = len(re.findall(r"\w+", text))
    return word_count >= _HEURISTIC_MIN_WORDS


def _heading_level(paragraph: Paragraph) -> int:
    """Returns the heading level 1..6 for a confirmed heading paragraph."""
    prefix = get_paragraph_prefix(paragraph)
    m = _HEADING_PREFIX_RE.match(prefix)
    if not m:
        # Defensive: caller is supposed to guard with _is_heading first.
        return 1
    level = len(m.group(1))
    return min(level, 6)


# ---------------------------------------------------------------------------
# Internal: heading text extraction
# ---------------------------------------------------------------------------


def _heading_text(paragraph: Paragraph, comments_map: dict) -> str:
    """
    Returns the heading text as the user would see it:
      - same projection as build_paragraph_text (so tracked changes / formatting
        look the same as in the body)
      - heading prefix removed
      - CriticMarkup tags stripped (insertions kept as plain text, deletions
        and meta-blocks dropped)
      - leading/trailing whitespace stripped
      - inline bold/italic markers stripped (outline is a summary, not a faithful
        re-render of formatting — see Concern 5).
    """
    p_text = build_paragraph_text(paragraph, comments_map, clean_view=False)
    cleaned = _strip_critic_markup(p_text)
    cleaned = _strip_inline_formatting(cleaned)
    return cleaned.strip()


def _strip_critic_markup(text: str) -> str:
    """
    Removes CriticMarkup tags. Insertions ({++X++}) keep their inner text;
    deletions ({--X--}) and meta-blocks ({>>X<<}) are dropped. Highlights
    ({==X==}) keep their inner text.

    This matches the philosophy of presenting the *intended* heading text in
    the outline — what the heading would say if all current edits were accepted.
    """
    if not text:
        return ""
    text = re.sub(r"\{--.*?--\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{>>.*?<<\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\{==(.*?)==\}", r"\1", text, flags=re.DOTALL)
    return text


def _strip_inline_formatting(text: str) -> str:
    """
    Strips inline **bold** and _italic_ markers from heading text.
    Conservative — only strips when the markers wrap word content, not standalone.
    """
    if not text:
        return ""
    # Bold: **X** or __X__
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # Italic: _X_ at word boundaries (avoid eating snake_case)
    text = re.sub(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Internal: heading style attribution
# ---------------------------------------------------------------------------


def _determine_heading_style(paragraph: Paragraph) -> str:
    """
    Mirrors get_paragraph_prefix's priority order:
      1. outline_level (any 0..8) → return style name if it's a Heading/Title,
         otherwise "(outline_level)"
      2. style.name starts with "Heading" or equals "Title" → return that name
      3. style name contains a 'Heading N' token (custom Word-generated
         quick styles like 'StyleHeading2NotItalicBefore0pt') → return name
      4. fallthrough → all-caps + bold heuristic → "(heuristic)"
    """
    # 1. Outline level
    try:
        lvl = paragraph.paragraph_format.outline_level
        if lvl is not None and 0 <= lvl <= 8:
            style = _safe_style_name(paragraph)
            if style and (style.startswith("Heading") or style == "Title"):
                return style
            return "(outline_level)"
    except Exception:
        pass

    # 2. Explicit Heading/Title style
    style = _safe_style_name(paragraph)
    if style and (style.startswith("Heading") or style == "Title"):
        return style

    # 3. Custom heading style name fallback (e.g. 'StyleHeading2...').
    # Treat as a real heading style so it isn't demoted to "(heuristic)"
    # and rejected by _heading_passes_quality_filter.
    if style:
        from adeu.utils.docx import _detect_heading_level_from_name

        if _detect_heading_level_from_name(style) is not None:
            return style

    # 4. All-caps + bold heuristic
    return "(heuristic)"


def _safe_style_name(paragraph: Paragraph) -> Optional[str]:
    """
    Returns paragraph.style.name or None. Mirrors ingest._get_paragraph_style_safe
    but inlined to avoid pulling in another private import.
    """
    try:
        style = paragraph.style
    except AttributeError:
        return None
    if style is None:
        return None
    return getattr(style, "name", None)


# ---------------------------------------------------------------------------
# Internal: heading ownership ranges
# ---------------------------------------------------------------------------


def _find_owned_end(
    block_records: List[_BlockRecord],
    heading_indices: List[int],
    current_h_pos: int,
    current_level: int,
) -> int:
    """
    Returns the exclusive end index in block_records for the current heading's
    owned range. Walks forward through subsequent headings until one with
    level <= current_level is found.
    """
    for next_h_pos in range(current_h_pos + 1, len(heading_indices)):
        next_idx = heading_indices[next_h_pos]
        next_paragraph = block_records[next_idx].item
        next_level = _heading_level(next_paragraph)
        if next_level <= current_level:
            return next_idx
    return len(block_records)


# ---------------------------------------------------------------------------
# Internal: footnote/endnote ID collection
# ---------------------------------------------------------------------------


def _collect_footnote_ids(owned_blocks: List[_BlockRecord]) -> List[str]:
    """
    Walks every paragraph in the owned range, collecting footnote/endnote
    references in document order, deduplicated, with first-seen order preserved.
    """
    seen: set = set()
    ordered: List[str] = []

    for rec in owned_blocks:
        if not rec.is_paragraph:
            continue
        paragraph: Paragraph = rec.item  # type: ignore[assignment]
        for event in _iter_paragraph_events(paragraph):
            if event.type == "footnote":
                fn_id = f"fn-{event.id}"
            elif event.type == "endnote":
                fn_id = f"en-{event.id}"
            else:
                continue
            if fn_id not in seen:
                seen.add(fn_id)
                ordered.append(fn_id)

    return ordered


def _iter_paragraph_events(paragraph: Paragraph) -> Iterator[DocxEvent]:
    """Yields just the DocxEvent objects from iter_paragraph_content."""
    for item in iter_paragraph_content(paragraph):
        if isinstance(item, DocxEvent):
            yield item


# ---------------------------------------------------------------------------
# Internal: offset → page mapping
# ---------------------------------------------------------------------------


def _offset_to_page(offset: int, body_page_offsets: List[int]) -> int:
    """
    Returns the 1-indexed page number for a given body offset.

    body_page_offsets[i] is the start of page i+1 (0-indexed array, 1-indexed
    page numbers). The page containing `offset` is the largest i such that
    body_page_offsets[i] <= offset, returned as i+1.
    """
    if not body_page_offsets:
        return 1
    page = 1
    for i, start in enumerate(body_page_offsets):
        if offset >= start:
            page = i + 1
        else:
            break
    return page
