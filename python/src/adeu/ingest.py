# FILE: src/adeu/ingest.py
import io
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import structlog
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from adeu.domain import build_structural_appendix
from adeu.redline.comments import CommentsManager
from adeu.utils.content_controls import (
    CHECKBOX_CLOSE,
    CHECKBOX_OPEN,
    QN_W_SDTCONTENT,
    BlockSdt,
    SdtEvent,
    assign_ordinals,
    part_element,
    wrapping_sdt,
)
from adeu.utils.docx import (
    DocxEvent,
    ProjectedRun,
    _get_style_cache,
    apply_formatting_to_segments,
    compute_change_pair_map,
    get_paragraph_prefix,
    is_heading_paragraph,
    is_native_heading,
    iter_block_items,
    iter_document_parts_with_kind,
    iter_paragraph_content,
    iter_row_cell_elements,
    iter_table_row_elements,
    markers_from_flags,
    paragraph_mark_is_deleted,
    strip_bom_from_docx_bytes,
)
from adeu.utils.opc import load_document as Document
from adeu.utils.text import escape_critic_tokens

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RowGeometry:
    """Text-space extent of one projected table row plus its cell texts."""

    start: int
    end: int
    cells: List[str]


@dataclass(slots=True)
class TableGeometry:
    """Text-space extent of one projected top-level table."""

    start: int
    end: int
    rows: List[RowGeometry] = field(default_factory=list)


@dataclass(slots=True)
class ExtractStructure:
    """
    Structural map of a projection: which offset ranges belong to which OPC
    part, and where top-level table rows live. Produced in the SAME pass as
    the text, so offsets always agree with it. Consumed by the diff pipeline
    to keep generated edits from crossing part boundaries (QA 2026-07-18 C1)
    and to emit structured row operations for table changes (QA C2).
    """

    part_ranges: List[Tuple[int, int, str]] = field(default_factory=list)  # (start, end, kind)
    tables: List[TableGeometry] = field(default_factory=list)


def _anchored_wrapper(element: Any, sdt_infos: dict | None):
    """The SdtInfo of the control wrapping this w:tr/w:tc, when it anchors."""
    if not sdt_infos:
        return None
    sdt = wrapping_sdt(element)
    if sdt is None:
        return None
    info = sdt_infos.get(id(sdt))
    return info if info is not None and info.anchored else None


def extract_text_from_stream(
    file_stream: io.BytesIO,
    filename: str = "document.docx",
    clean_view: bool = False,
    include_appendix: bool = True,
) -> str:
    """
    Extracts text from a file stream using raw run concatenation.
    Includes Markdown headers (#) and CriticMarkup Comments ({==Text==}{>>Comment<<}).

    Args:
        clean_view: If True, simulates "Accept All Changes": hides deletions,
                    removes insertion wrappers, hides comments.
        include_appendix: If False, omits the generated read-only structural
                    appendix — required whenever the text feeds a comparison
                    (QA 2026-07-18 H1).

    CRITICAL: This must match DocumentMapper._build_map logic exactly.
    """
    try:
        file_stream.seek(0)

        sanitized_bytes = strip_bom_from_docx_bytes(file_stream.read())
        doc = Document(io.BytesIO(sanitized_bytes))
        return _extract_text_from_doc(doc, clean_view, include_appendix=include_appendix)
    except Exception as e:
        logger.error(f"Text extraction failed: {e}", exc_info=True)
        raise ValueError(f"Could not extract text: {str(e)}") from e


def _extract_text_from_doc(
    doc,
    clean_view: bool = False,
    include_appendix: bool = True,
    return_paragraph_offsets: bool = False,
    return_structure: bool = False,
):
    """
    Extracts text from an already-loaded python-docx Document.

    Args:
        clean_view: if True, simulate "Accept All Changes" view.
        include_appendix: if True (default), append the structural appendix
            (defined terms, anchors, diagnostics) to the projected text.
            Set False when the caller discards the appendix (mode='full' /
            mode='outline' do not ship it in the response).
        return_paragraph_offsets: if True (default False), returns a tuple
            (text, offset_map) where offset_map is Dict[id(p._element), (start, length)]
            for every paragraph projected. Used by mode='outline'
            to avoid re-projecting paragraphs to extract heading text.
        return_structure: if True (default False), returns a tuple
            (text, ExtractStructure) mapping offset ranges to OPC parts and
            top-level table rows. Mutually exclusive with
            return_paragraph_offsets.

    Returns:
        - text: str   (default)
        - (text, offset_map): tuple   (when return_paragraph_offsets=True)
        - (text, structure): tuple    (when return_structure=True)

    PERF: normalize_docx() is deliberately not called here. The ingest
    pipeline tolerates fragmented runs (build_paragraph_text coalesces
    marker/wrapper boundaries), and read-only ingest is safe without it;
    the RedlineEngine normalizes on edit paths, where DOM mutation is
    already happening anyway.
    """
    comments_mgr = CommentsManager(doc)
    comments_map = comments_mgr.extract_comments_data()

    # Ordinals are assigned ONCE, over the parts in projection order, and the
    # resulting map is threaded through every level below. Spec-projection.md
    # §9 requires this to be a single shared pre-pass rather than a counter
    # each producer maintains: a counter is exactly the shape of bug CC-12 was
    # (two producers agreeing with each other and both wrong).
    sdt_infos = assign_ordinals(part_element(part) for part, _kind in iter_document_parts_with_kind(doc))

    full_text: list[str] = []
    # Store the lxml proxy as the 3rd tuple item to keep it alive, preventing
    # CPython from recycling the id() memory address between passes.
    offset_map: Optional[dict[int, tuple[int, int, Any]]] = {} if return_paragraph_offsets else None
    structure: Optional[ExtractStructure] = ExtractStructure() if return_structure else None
    cursor = 0

    for part, part_kind in iter_document_parts_with_kind(doc):
        # part_cursor accounts for the \n\n separator that will precede this part
        # in the final join, ensuring internal offsets align exactly.
        part_cursor = cursor + 2 if full_text else cursor
        part_text = _extract_blocks(
            part,
            comments_map,
            clean_view,
            offset_map=offset_map,
            cursor=part_cursor,
            table_acc=structure.tables if structure is not None else None,
            sdt_infos=sdt_infos,
        )
        if part_text:
            if full_text:
                # The "\n\n" separator that join() inserts between parts must
                # be reflected in the cursor so subsequent paragraph offsets
                # remain accurate.
                cursor += 2
            full_text.append(part_text)
            if structure is not None:
                structure.part_ranges.append((cursor, cursor + len(part_text), part_kind))
            cursor += len(part_text)

    base_text = "\n\n".join(full_text)

    if include_appendix:
        appendix = build_structural_appendix(doc, base_text)
        if appendix:
            base_text = base_text + appendix

    if return_paragraph_offsets:
        return base_text, offset_map
    if return_structure:
        return base_text, structure
    return base_text


def _extract_blocks(
    container: Any,
    comments_map: dict,
    clean_view: bool,
    offset_map: dict | None = None,
    cursor: int = 0,
    table_acc: list | None = None,
    style_cache: dict | None = None,
    default_pstyle: str | None = None,
    part: Any = None,
    sdt_infos: dict | None = None,
    in_cell: bool = False,
) -> str:
    """
    Recursively extracts text from a container (Document, Cell, Header, etc.)
    iterating over Paragraphs and Tables in order.

    table_acc: optional list collecting TableGeometry for TOP-LEVEL tables.
    Deliberately not forwarded into cells — nested tables stay invisible to
    the structured row-op diff, whose row pairing assumes one flat grid.
    """
    if part is None:
        part = getattr(container, "part", container)
    if style_cache is None:
        style_cache, default_pstyle = _get_style_cache(part)

    blocks = []
    local_cursor = cursor

    c_type = type(container).__name__
    if c_type == "NotesPart":
        header = "## Footnotes" if container.note_type == "fn" else "## Endnotes"
        blocks.append(f"---\n{header}")
        local_cursor += len(header) + 4  # "---\n" + header chars
        # Note: the +4 above is "---\n" length. The actual block append uses
        # f"---\n{header}" which has length 4 + len(header). The local_cursor
        # advance must match. Below we'll add the inter-block "\n\n" before
        # the next block.

    # Replay the join behavior: blocks are joined by "\n\n", which means
    # we add 2 to the cursor between blocks (not before the first).
    is_first_block = len(blocks) == 0

    is_first_para = True
    for item in iter_block_items(container, emit_sdt=sdt_infos is not None):
        i_type = type(item).__name__

        if not is_first_block:
            local_cursor += 2  # "\n\n" between blocks

        block_start = local_cursor

        if isinstance(item, BlockSdt):
            # A block-level content control. Recurse into its contents exactly
            # as a Table recurses into its rows, then bracket the result with
            # token lines: open token on its own line, a single "\n" joining it
            # to the wrapped content, close token on its own line (spec §3/§5).
            # The surrounding "\n\n" comes from the block join, as for any
            # other block.
            info = sdt_infos.get(id(item.element)) if sdt_infos else None
            # Spec §3 exception: inside a table cell a block-level anchor
            # renders INLINE. A row is one projected line, so token lines would
            # break the "|" grammar and desynchronise the column count.
            joiner = "" if in_cell else "\n"
            open_tok = close_tok = ""
            if info is not None and info.anchored:
                open_tok = f"{info.open_token}{joiner}"
                close_tok = f"{joiner}{info.close_token}"
            inner = _extract_blocks(
                item.element.find(QN_W_SDTCONTENT),
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=block_start + len(open_tok),
                sdt_infos=sdt_infos,
                style_cache=style_cache,
                default_pstyle=default_pstyle,
                part=part,
                in_cell=in_cell,
            )
            if inner:
                full = f"{open_tok}{inner}{close_tok}"
                blocks.append(full)
                local_cursor = block_start + len(full)
                is_first_block = False
            elif not is_first_block:
                # Projects nothing: the reader drops the block AND its
                # separator, same contract as an empty table.
                local_cursor -= 2
            is_first_para = False

        elif i_type == "FootnoteItem":
            fn_text = _extract_blocks(
                item,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=block_start,
                sdt_infos=sdt_infos,
                style_cache=style_cache,
                default_pstyle=default_pstyle,
            )
            if fn_text:
                blocks.append(fn_text)
                local_cursor = block_start + len(fn_text)
                is_first_block = False
            else:
                # Empty footnote contributes nothing; rewind the "\n\n" we
                # speculatively added.
                if not is_first_block:
                    local_cursor -= 2
        elif isinstance(item, Paragraph):
            p_elem = item._element
            style_prefix = get_paragraph_prefix(p_elem, style_cache, default_pstyle, part=part)
            prefix = style_prefix
            if is_first_para and c_type == "FootnoteItem":
                prefix = f"[^{container.note_type}-{container.id}]: " + prefix
            # Pass the UNDECORATED style prefix: the heading test is about the
            # paragraph's own style, not the footnote label.
            p_text = build_paragraph_text(
                p_elem,
                comments_map,
                clean_view,
                style_cache,
                default_pstyle,
                paragraph_prefix=style_prefix,
                part=part,
                sdt_infos=sdt_infos,
            )
            if clean_view and not p_text and paragraph_mark_is_deleted(p_elem):
                # Accepting a tracked paragraph-mark deletion merges the
                # paragraph away; when nothing visible survives inside it,
                # the accepted view must not render an empty container
                # (QA round 3, finding 2.4).
                if not is_first_block:
                    local_cursor -= 2
                is_first_para = False
                continue
            full_block = prefix + p_text
            blocks.append(full_block)
            if offset_map is not None:
                offset_map[id(p_elem)] = (
                    block_start,
                    len(full_block),
                    p_elem,
                )
            local_cursor = block_start + len(full_block)
            is_first_para = False
            is_first_block = False

        elif isinstance(item, Table):
            tbl_elem = item._element
            geometry = TableGeometry(start=block_start, end=block_start) if table_acc is not None else None
            table_text = extract_table(
                tbl_elem,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=block_start,
                geometry=geometry,
                sdt_infos=sdt_infos,
                style_cache=style_cache,
                default_pstyle=default_pstyle,
                part=part,
            )
            if table_text:
                blocks.append(table_text)
                local_cursor = block_start + len(table_text)
                is_first_block = False
                if geometry is not None and table_acc is not None:
                    geometry.end = block_start + len(table_text)
                    table_acc.append(geometry)
            else:
                if not is_first_block:
                    local_cursor -= 2
            is_first_para = False

    return "\n\n".join(blocks)


def extract_table(
    table: Any,
    comments_map,
    clean_view: bool,
    offset_map: dict | None = None,
    cursor: int = 0,
    geometry: "TableGeometry | None" = None,
    style_cache: dict | None = None,
    default_pstyle: str | None = None,
    part: Any = None,
    sdt_infos: dict | None = None,
) -> str:
    """
    Args:
        offset_map: see _extract_blocks docstring.
        cursor: absolute offset where this table begins in the final body.
        geometry: optional TableGeometry to fill with per-row offsets/cells.
    """
    rows_text: list[str] = []
    rows_processed = 0
    local_cursor = cursor

    tbl_elem = table._element if hasattr(table, "_element") else table

    for tr in iter_table_row_elements(tbl_elem):
        cell_texts: list[str] = []
        seen_cells: set = set()

        # Structural Row Tracking — figure out wrapper offsets first so cell
        # offsets land correctly inside the wrapped row text.
        trPr = tr.find(qn("w:trPr"))
        ins = trPr.find(qn("w:ins")) if trPr is not None else None
        del_node = trPr.find(qn("w:del")) if trPr is not None else None

        if clean_view and del_node is not None:
            continue

        # Row separator "\n" between rows
        row_start = local_cursor + (1 if rows_processed > 0 else 0)

        # Wrapper prefix (e.g. "{++ ") shifts the inner content
        wrapper_prefix_len = 0
        if not clean_view:
            if ins is not None:
                wrapper_prefix_len = len("{++ ")
            elif del_node is not None:
                wrapper_prefix_len = len("{-- ")

        cell_cursor = row_start + wrapper_prefix_len
        first_cell = True

        for tc in iter_row_cell_elements(tr):
            if tc in seen_cells:
                continue
            seen_cells.add(tc)

            if not first_cell:
                cell_cursor += 3  # " | " between cells

            cell_info = _anchored_wrapper(tc, sdt_infos)
            cell_open = cell_info.open_token if cell_info else ""
            cell_content = _extract_blocks(
                tc,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=cell_cursor + len(cell_open),
                sdt_infos=sdt_infos,
                style_cache=style_cache,
                default_pstyle=default_pstyle,
                part=part,
                in_cell=True,
            )
            if cell_info is not None:
                # Cell-level control (sdtContent > w:tc): anchors render inline
                # inside this cell's segment (spec §3).
                cell_content = f"{cell_open}{cell_content}{cell_info.close_token}"
            if not clean_view:
                first_p_list = tc.findall(".//" + qn("w:p"))
                firstP = first_p_list[0] if first_p_list else None
                paraId = firstP.get(qn("w14:paraId")) if firstP is not None else None
                if paraId:
                    separator = " " if cell_content and not cell_content.endswith(" ") else ""
                    cell_content = cell_content + separator + f"{{#cell:{paraId}}}"

            cell_texts.append(cell_content)
            cell_cursor += len(cell_content)
            first_cell = False

        row_str = " | ".join(cell_texts)

        # Row-level control (sdtContent > w:tr): open token before the first
        # cell's text, close after the last, on the row's line (spec §3).
        # Applied before the tracked-change wrapper below so a row that is both
        # controlled and inserted reads "{++ {#cc:N}...{#/cc:N} ++}" — the
        # CriticMarkup is about the row's existence, the anchor about its
        # identity, and the anchor is the inner of the two.
        row_info = _anchored_wrapper(tr, sdt_infos)
        if row_info is not None:
            row_str = f"{row_info.open_token}{row_str}{row_info.close_token}"

        if not clean_view:
            # The change bubble is SEPARATED from cell content, mirroring the
            # normal insertion-bubble shape — the old ` |Chg:N++}` suffix read
            # as part of the last cell's text (QA 2026-07-23 F21a). Twin
            # rendering lives in redline/mapper.py (_map_table) and MUST stay
            # byte-identical (Virtual Text contract).
            if ins is not None:
                author = ins.get(qn("w:author")) or "Unknown"
                row_str = f"{{++ {row_str} ++}}{{>>[Chg:{ins.get(qn('w:id'))} insert] {author}<<}}"
            elif del_node is not None:
                author = del_node.get(qn("w:author")) or "Unknown"
                row_str = f"{{-- {row_str} --}}{{>>[Chg:{del_node.get(qn('w:id'))} delete] {author}<<}}"

        rows_text.append(row_str)
        local_cursor = row_start + len(row_str)
        if geometry is not None:
            geometry.rows.append(RowGeometry(start=row_start, end=local_cursor, cells=list(cell_texts)))
        rows_processed += 1

        if rows_processed == 1:
            num_cols = len(cell_texts)
            if num_cols > 0:
                divider_str = " | ".join(["---"] * num_cols)
                rows_text.append(divider_str)
                local_cursor += 1 + len(divider_str)

    return "\n".join(rows_text)


def build_paragraph_text(
    paragraph,
    comments_map,
    clean_view: bool = False,
    style_cache: Optional[dict] = None,
    default_pstyle: Optional[str] = None,
    paragraph_prefix: Optional[str] = None,
    part: Any = None,
    sdt_infos: Optional[dict] = None,
):
    """
    Flatten overlapping comments into sequential CriticMarkup blocks.
    Merges metadata for adjacent Redline blocks (Substitutions).

    Coalescing invariant (FIX C — see AI_CONTEXT §2):
      * `pending_text` accumulates wrapped segments for the current CriticMarkup
        wrapper group (e.g. everything inside one {++...++}).
      * Merge eligibility is based on WRAPPERS ONLY — two runs inside the same
        redline group should combine into one {++...++} block regardless of
        their individual bold/italic styling.
      * When two adjacent runs within a merged group share the SAME non-empty
        style markers (e.g. both bold), the closing marker of the previous
        segment and the opening marker of the next segment are elided so we
        emit "**AB**" instead of "**A****B**". This fixes live-Word run
        fragmentation where "New" is sometimes split into "N" + "ew" with
        identical rPr.
      * When the adjacent run has a DIFFERENT style, the markers are kept
        independently (e.g. "**A** B **C**" or "**bold** and _italic_").
    """
    parts = []

    active_ins: dict[str, DocxEvent] = {}
    active_del: dict[str, DocxEvent] = {}
    active_comments: set[str] = set()
    active_fmt: dict[str, DocxEvent] = {}

    deferred_meta_states = []

    pending_text = ""
    current_wrappers = ("", "")  # CriticMarkup tokens, e.g. ("{++", "++}")
    # `current_style` tracks the style of the trailing segment in pending_text,
    # used only to decide whether the next incoming run can elide adjacent markers.
    current_style = ("", "")

    items = list(iter_paragraph_content(paragraph, part=part, sdt_infos=sdt_infos))

    # Heading-leading-whitespace strip: in heading paragraphs, leading runs
    # whose text is whitespace-only (e.g. a lone <w:br/> or <w:tab/>) are
    # visual noise that would otherwise project as "## \nText". We drop
    # them until we hit either the first non-whitespace run or any non-Run
    # event (e.g. a tracked-change boundary), at which point heading content
    # has effectively begun and stripping must stop. Mid-content breaks
    # (e.g. "Line 1\nLine 2" in a heading) are preserved.
    # `paragraph_prefix`, when supplied by _extract_blocks, is the prefix it
    # already computed for this paragraph — reusing it avoids a second full
    # get_paragraph_prefix walk per paragraph (see is_heading_paragraph).
    is_heading = is_heading_paragraph(paragraph, style_cache, default_pstyle, prefix=paragraph_prefix)
    native_heading = is_native_heading(paragraph, style_cache, default_pstyle)
    leading_strip_active = is_heading

    for i, item in enumerate(items):
        if isinstance(item, ProjectedRun):
            # Fully fused: iter_paragraph_content already walked this run's
            # children once and carried the result, so there is no second walk
            # here at all — only the (pure) marker derivation.
            text = item.proj_text
            prefix, suffix = markers_from_flags(item.proj_bold, item.proj_italic, native_heading)

            if clean_view and active_del:
                continue

            if leading_strip_active:
                if text == "" or text.isspace():
                    # Skip this leading whitespace-only run entirely.
                    continue
                leading_strip_active = False

            seg = apply_formatting_to_segments(text, prefix, suffix)
            if seg:
                if clean_view:
                    new_wrappers = ("", "")
                else:
                    new_wrappers = _get_wrappers(active_ins, active_del, active_comments, active_fmt)
                new_style = (prefix, suffix)

                if pending_text and new_wrappers == current_wrappers:
                    # MERGE into current wrapper group.
                    # Elide adjacent same-style markers only when both sides carry
                    # the same NON-EMPTY style markers (so "**A**"+"**B**" -> "**AB**",
                    # but "foo_"+"_italic_" is NOT elided because the plain run has
                    # empty style and its trailing "_" is literal). Hoisted leading
                    # whitespace may sit before the incoming segment's opening
                    # marker ("**A**" + " **B**" -> "**A B**"), mirroring the
                    # mapper's part-level elision exactly (QA 2026-07-19 F-03).
                    lead_match = re.match(r"(\s*)" + re.escape(new_style[0]), seg) if new_style != ("", "") else None
                    trailing_ws = ""
                    for char in reversed(pending_text):
                        if char.isspace():
                            trailing_ws = char + trailing_ws
                        else:
                            break
                    pending_without_ws = pending_text if not trailing_ws else pending_text[: -len(trailing_ws)]
                    if (
                        new_style == current_style
                        and current_style != ("", "")
                        and pending_without_ws.endswith(current_style[1])
                        and lead_match is not None
                    ):
                        pending_text = (
                            pending_without_ws[: -len(current_style[1])]
                            + trailing_ws
                            + lead_match.group(1)
                            + seg[lead_match.end() :]
                        )
                    else:
                        pending_text += seg
                    current_style = new_style
                else:
                    # FLUSH: wrapper group boundary.
                    if pending_text:
                        s_tok, e_tok = current_wrappers
                        parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = seg
                    current_wrappers = new_wrappers
                    current_style = new_style

                # Handle Metadata (always accumulate state snapshot)
                if not clean_view:
                    has_meta = active_ins or active_del or active_comments or active_fmt
                    if has_meta:
                        current_state = (
                            active_ins.copy() if active_ins else {},
                            active_del.copy() if active_del else {},
                            active_comments.copy() if active_comments else set(),
                            active_fmt.copy() if active_fmt else {},
                        )
                        deferred_meta_states.append(current_state)

                    should_defer = False
                    has_any_meta = bool(active_ins) or bool(active_del) or bool(active_fmt) or bool(active_comments)

                    if has_any_meta:
                        j = i + 1
                        next_has_meta = False
                        temp_ins_count = len(active_ins)
                        temp_del_count = len(active_del)
                        temp_fmt_count = len(active_fmt)
                        temp_comment_ids = set(active_comments)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, ProjectedRun):
                                # Carried by the stream.
                                if not next_item.proj_text:
                                    j += 1
                                    continue
                                if (
                                    temp_ins_count > 0
                                    or temp_del_count > 0
                                    or temp_fmt_count > 0
                                    or len(temp_comment_ids) > 0
                                ):
                                    next_has_meta = True
                                break
                            elif isinstance(next_item, DocxEvent):
                                if next_item.type == "ins_start":
                                    temp_ins_count += 1
                                elif next_item.type == "ins_end":
                                    temp_ins_count = max(0, temp_ins_count - 1)
                                elif next_item.type == "del_start":
                                    temp_del_count += 1
                                elif next_item.type == "del_end":
                                    temp_del_count = max(0, temp_del_count - 1)
                                elif next_item.type == "fmt_start":
                                    temp_fmt_count += 1
                                elif next_item.type == "fmt_end":
                                    temp_fmt_count = max(0, temp_fmt_count - 1)
                                elif next_item.type == "start":
                                    temp_comment_ids.add(next_item.id)
                                elif next_item.type == "end":
                                    temp_comment_ids.discard(next_item.id)
                            j += 1

                        if next_has_meta:
                            should_defer = True

                    if not should_defer and deferred_meta_states:
                        meta_block = _build_merged_meta_block(deferred_meta_states, comments_map)
                        if meta_block:
                            if pending_text:
                                s_tok, e_tok = current_wrappers
                                parts.append(f"{s_tok}{pending_text}{e_tok}")
                                pending_text = ""
                                current_wrappers = ("", "")
                                current_style = ("", "")
                            parts.append(f"{{>>{meta_block}<<}}")
                        deferred_meta_states = []

        elif isinstance(item, SdtEvent):
            # Content-control boundary. A sibling of the DocxEvent branch
            # rather than a member of it: DocxEvent is a 4-field NamedTuple
            # keyed on strings, and an anchor needs the whole SdtInfo (flags,
            # class, placeholder text), so widening DocxEvent would have meant
            # a parallel out-of-band lookup at every consumer.
            #
            # Heading content has begun: an anchor is addressable text, so
            # the leading-whitespace strip stops here exactly as it does for
            # every other non-Run event.
            leading_strip_active = False
            # Anchor tokens are structural and must NOT be swept into the
            # emphasis/CriticMarkup group being accumulated: a control inside a
            # bold span would otherwise emit `**{#cc:3}text**`, and every
            # marker-stripping pass would mangle the token.
            if pending_text:
                s_tok, e_tok = current_wrappers
                parts.append(f"{s_tok}{pending_text}{e_tok}")
                pending_text = ""
                current_wrappers = ("", "")
                current_style = ("", "")
            info = item.info
            if item.type == "sdt_start":
                parts.append(info.open_token)
                # The placeholder bubble is virtual chrome: raw view only,
                # dropped in the clean view because an unfilled field has no
                # accepted-state content (spec §6).
                if not clean_view and info.showing_placeholder and info.placeholder_text:
                    parts.append(f"{{>>placeholder: {info.placeholder_text}<<}}")
            elif item.type == "checkbox_start":
                # Checkbox tokens persist in the clean view: they are structural
                # (spec §6), like the anchors, not commentary.
                parts.append(CHECKBOX_OPEN)
            elif item.type == "checkbox_mark":
                # Fallback only — the mark is normally a real run emitted by the
                # traversal, and arrives through the ProjectedRun branch.
                parts.append(info.checkbox_mark)
            elif item.type == "checkbox_end":
                parts.append(CHECKBOX_CLOSE)
            else:
                parts.append(info.close_token)

        elif isinstance(item, DocxEvent):
            # Once we see any event, real heading content has effectively begun
            # (or a tracked-change boundary now spans the leading position) —
            # stop the leading whitespace strip.
            leading_strip_active = False
            # Only flush pending text for structural events (like comments, links, footnotes).
            # Pure state transitions (like adjacent w:ins/w:del tags splitting a run) must coalesce.
            if item.type not in (
                "ins_start",
                "ins_end",
                "del_start",
                "del_end",
                "fmt_start",
                "fmt_end",
            ):
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")

            if item.type == "start":
                active_comments.add(item.id)
            elif item.type == "end":
                active_comments.discard(item.id)
            elif item.type == "ins_start":
                active_ins[item.id] = item
            elif item.type == "ins_end":
                active_ins.pop(item.id, None)
            elif item.type == "del_start":
                active_del[item.id] = item
            elif item.type == "del_end":
                active_del.pop(item.id, None)
            elif item.type == "fmt_start":
                active_fmt[item.id] = item
            elif item.type == "fmt_end":
                active_fmt.pop(item.id, None)
            elif item.type == "image":
                if clean_view and active_del:
                    continue
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                alt = (item.date or "image").replace("]", ")").replace("\n", " ")
                parts.append(f"![{alt}](docx-image:{item.id})")
            elif item.type in ("footnote", "endnote"):
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                prefix_str = "fn" if item.type == "footnote" else "en"
                parts.append(f"[^{prefix_str}-{item.id}]")
            elif item.type == "hyperlink_start":
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                parts.append("[")
            elif item.type == "hyperlink_end":
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                parts.append(f"]({item.date})")
            elif item.type == "xref_start":
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                parts.append("[~")
            elif item.type == "xref_end":
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                parts.append(f"~](#{item.id})")
            elif item.type == "bookmark":
                if pending_text:
                    s_tok, e_tok = current_wrappers
                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                    pending_text = ""
                    current_wrappers = ("", "")
                    current_style = ("", "")
                parts.append(f"{{#{item.id}}}")

    if pending_text:
        s_tok, e_tok = current_wrappers
        parts.append(f"{s_tok}{pending_text}{e_tok}")

    if deferred_meta_states:
        meta_block = _build_merged_meta_block(deferred_meta_states, comments_map)
        if meta_block:
            parts.append(f"{{>>{meta_block}<<}}")

    return "".join(parts)


def _get_wrappers(active_ins, active_del, active_comments, active_fmt):
    if active_del:
        return "{--", "--}"
    elif active_ins:
        return "{++", "++}"
    elif active_comments or active_fmt:
        return "{==", "==}"
    return "", ""


def _build_merged_meta_block(states_list, comments_map) -> str:
    """
    Combines metadata from multiple states, removing duplicates.
    Canonical Order: Changes first, then Comments (threaded).
    """
    change_lines = []
    comment_lines = []
    seen_sigs = set()

    children_map: dict[str, list[str]] = {}
    for c_id, data in comments_map.items():
        p_id = data.get("parent_id")
        if p_id:
            children_map.setdefault(p_id, []).append(c_id)

    def render_comment(cid):
        if cid not in comments_map:
            return

        sig = f"Com:{cid}"
        if sig in seen_sigs:
            return

        data = comments_map[cid]
        header = f"[{sig}] {data['author']}"
        if data["date"]:
            header += f" @ {data['date']}"
        # Parity with DocumentMapper._map_comments and both Node projections,
        # all three of which mark resolved threads. Without it the agent cannot
        # tell a settled comment from an open one.
        if data.get("resolved"):
            header += "(RESOLVED)"

        comment_lines.append(f"{header}: {escape_critic_tokens(data['text'])}")
        seen_sigs.add(sig)

        if cid in children_map:
            children = children_map[cid]
            children.sort(key=lambda x: comments_map.get(x, {}).get("date", ""))
            for child_id in children:
                render_comment(child_id)

    # Ids of one resolution group (a replacement's contiguous same-author
    # del+ins pair) must not read as independently resolvable — either side
    # resolves the whole group (QA 2026-07-19 ADEU-QA-004).
    pair_map = compute_change_pair_map(states_list)

    def _pair_suffix(uid) -> str:
        return f" (pairs with {pair_map[uid]})" if uid in pair_map else ""

    for ins_map, del_map, comments_set, fmt_map in states_list:
        for uid, meta in ins_map.items():
            sig = f"Chg:{uid}"
            if sig not in seen_sigs:
                auth = meta.author or "Unknown"
                change_lines.append(f"[{sig} insert] {auth}{_pair_suffix(uid)}")
                seen_sigs.add(sig)
        for uid, meta in del_map.items():
            sig = f"Chg:{uid}"
            if sig not in seen_sigs:
                auth = meta.author or "Unknown"
                change_lines.append(f"[{sig} delete] {auth}{_pair_suffix(uid)}")
                seen_sigs.add(sig)
        for uid, meta in fmt_map.items():
            sig = f"Chg:{uid}"
            if sig not in seen_sigs:
                auth = meta.author or "Unknown"
                change_lines.append(f"[{sig} format] {auth}")
                seen_sigs.add(sig)

        for root_id in sorted(comments_set):
            render_comment(root_id)

    return "\n".join(change_lines + comment_lines)
