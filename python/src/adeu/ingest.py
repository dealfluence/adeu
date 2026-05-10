# FILE: src/adeu/ingest.py
import io
from typing import Any, Optional

import structlog
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.domain import build_structural_appendix
from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    _get_style_cache,
    apply_formatting_to_segments,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
    is_heading_paragraph,
    is_native_heading,
    iter_block_items,
    iter_document_parts,
    iter_paragraph_content,
)

logger = structlog.get_logger(__name__)


def extract_text_from_stream(file_stream: io.BytesIO, filename: str = "document.docx", clean_view: bool = False) -> str:
    """
    Extracts text from a file stream using raw run concatenation.
    Includes Markdown headers (#) and CriticMarkup Comments ({==Text==}{>>Comment<<}).

    Args:
        clean_view: If True, simulates "Accept All Changes": hides deletions,
                    removes insertion wrappers, hides comments.

    CRITICAL: This must match DocumentMapper._build_map logic exactly.
    """
    try:
        file_stream.seek(0)
        doc = Document(file_stream)
        return _extract_text_from_doc(doc, clean_view)
    except Exception as e:
        logger.error(f"Text extraction failed: {e}", exc_info=True)
        raise ValueError(f"Could not extract text: {str(e)}") from e


def _extract_text_from_doc(
    doc,
    clean_view: bool = False,
    include_appendix: bool = True,
    return_paragraph_offsets: bool = False,
):
    """
    Extracts text from an already-loaded python-docx Document.

    Args:
        clean_view: if True, simulate "Accept All Changes" view.
        include_appendix: if True (default), append the structural appendix
            (defined terms, anchors, diagnostics) to the projected text.
            Set False when the caller knows it will discard the appendix
            (e.g. mode='full' / mode='outline' since Step 3 — those modes
            no longer ship the appendix in the response).
        return_paragraph_offsets: if True (default False), returns a tuple
            (text, offset_map) where offset_map is Dict[id(p._element), (start, length)]
            for every paragraph projected. Used by mode='outline' (Step 4 / Option A)
            to avoid re-projecting paragraphs to extract heading text.

    Returns:
        - text: str   (default)
        - (text, offset_map): tuple   (when return_paragraph_offsets=True)

    PERF: We no longer call normalize_docx() here. The ingest pipeline tolerates
    fragmented runs (build_paragraph_text coalesces marker/wrapper boundaries
    via FIX C logic). normalize_docx remains called by the RedlineEngine on
    edit paths via the engine's own initialization, where DOM mutation is
    already happening anyway. Read-only ingest skipping it is safe.
    """
    comments_mgr = CommentsManager(doc)
    comments_map = comments_mgr.extract_comments_data()

    full_text: list[str] = []
    # Store the lxml proxy as the 3rd tuple item to keep it alive, preventing
    # CPython from recycling the id() memory address between passes.
    offset_map: Optional[dict[int, tuple[int, int, Any]]] = {} if return_paragraph_offsets else None
    cursor = 0

    for part in iter_document_parts(doc):
        # part_cursor accounts for the \n\n separator that will precede this part
        # in the final join, ensuring internal offsets align exactly.
        part_cursor = cursor + 2 if full_text else cursor
        part_text = _extract_blocks(
            part,
            comments_map,
            clean_view,
            offset_map=offset_map,
            cursor=part_cursor,
        )
        if part_text:
            if full_text:
                # The "\n\n" separator that join() inserts between parts must
                # be reflected in the cursor so subsequent paragraph offsets
                # remain accurate.
                cursor += 2
            full_text.append(part_text)
            cursor += len(part_text)

    base_text = "\n\n".join(full_text)

    if include_appendix:
        appendix = build_structural_appendix(doc, base_text)
        if appendix:
            base_text = base_text + appendix

    if return_paragraph_offsets:
        return base_text, offset_map
    return base_text


def _extract_blocks(
    container,
    comments_map,
    clean_view: bool,
    offset_map: dict | None = None,
    cursor: int = 0,
) -> str:
    """
    Recursively extracts text from a container (Document, Cell, Header, etc.)
    iterating over Paragraphs and Tables in order.
    """
    # Fetch style cache exactly once per container block
    part = getattr(container, "part", container)

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
    for item in iter_block_items(container):
        i_type = type(item).__name__

        if not is_first_block:
            local_cursor += 2  # "\n\n" between blocks

        block_start = local_cursor

        if i_type == "FootnoteItem":
            fn_text = _extract_blocks(
                item,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=block_start,
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
            prefix = get_paragraph_prefix(item, style_cache, default_pstyle)
            if is_first_para and c_type == "FootnoteItem":
                prefix = f"[^{container.note_type}-{container.id}]: " + prefix
            p_text = build_paragraph_text(item, comments_map, clean_view, style_cache, default_pstyle)
            full_block = prefix + p_text
            blocks.append(full_block)
            if offset_map is not None:
                offset_map[id(item._element)] = (
                    block_start,
                    len(full_block),
                    item._element,
                )
            local_cursor = block_start + len(full_block)
            is_first_para = False
            is_first_block = False

        elif isinstance(item, Table):
            table_text = extract_table(
                item,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=block_start,
            )
            if table_text:
                blocks.append(table_text)
                local_cursor = block_start + len(table_text)
                is_first_block = False
            else:
                if not is_first_block:
                    local_cursor -= 2
            is_first_para = False

    return "\n\n".join(blocks)


def extract_table(
    table: Table,
    comments_map,
    clean_view: bool,
    offset_map: dict | None = None,
    cursor: int = 0,
) -> str:
    """
    Args:
        offset_map: see _extract_blocks docstring.
        cursor: absolute offset where this table begins in the final body.
    """
    rows_text: list[str] = []
    rows_processed = 0
    local_cursor = cursor

    for row in table.rows:
        cell_texts: list[str] = []
        seen_cells: set = set()

        # Structural Row Tracking — figure out wrapper offsets first so cell
        # offsets land correctly inside the wrapped row text.
        tr = row._element
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

        for cell in row.cells:
            if cell in seen_cells:
                continue
            seen_cells.add(cell)

            if not first_cell:
                cell_cursor += 3  # " | " between cells

            cell_content = _extract_blocks(
                cell,
                comments_map,
                clean_view,
                offset_map=offset_map,
                cursor=cell_cursor,
            )
            cell_texts.append(cell_content)
            cell_cursor += len(cell_content)
            first_cell = False

        row_str = " | ".join(cell_texts)

        if not clean_view:
            if ins is not None:
                row_str = f"{{++ {row_str} |Chg:{ins.get(qn('w:id'))}++}}"
            elif del_node is not None:
                row_str = f"{{-- {row_str} |Chg:{del_node.get(qn('w:id'))}--}}"

        rows_text.append(row_str)
        local_cursor = row_start + len(row_str)
        rows_processed += 1

    return "\n".join(rows_text)


def build_paragraph_text(
    paragraph,
    comments_map,
    clean_view: bool = False,
    style_cache: Optional[dict] = None,
    default_pstyle: Optional[str] = None,
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

    items = list(iter_paragraph_content(paragraph))

    # Heading-leading-whitespace strip: in heading paragraphs, leading runs
    # whose text is whitespace-only (e.g. a lone <w:br/> or <w:tab/>) are
    # visual noise that would otherwise project as "## \nText". We drop
    # them until we hit either the first non-whitespace run or any non-Run
    # event (e.g. a tracked-change boundary), at which point heading content
    # has effectively begun and stripping must stop. Mid-content breaks
    # (e.g. "Line 1\nLine 2" in a heading) are preserved.
    is_heading = is_heading_paragraph(paragraph, style_cache, default_pstyle)
    native_heading = is_native_heading(paragraph, style_cache, default_pstyle)
    leading_strip_active = is_heading

    for i, item in enumerate(items):
        if isinstance(item, Run):
            prefix, suffix = get_run_style_markers(item, native_heading)
            text = get_run_text(item)

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
                    # empty style and its trailing "_" is literal).
                    if (
                        new_style == current_style
                        and current_style != ("", "")
                        and pending_text.endswith(current_style[1])
                        and seg.startswith(new_style[0])
                    ):
                        pending_text = pending_text[: -len(current_style[1])] + seg[len(new_style[0]) :]
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
                    is_redline = bool(active_ins) or bool(active_del) or bool(active_fmt)

                    if is_redline:
                        j = i + 1
                        next_is_redline = False
                        temp_ins_count = len(active_ins)
                        temp_del_count = len(active_del)
                        temp_fmt_count = len(active_fmt)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, Run):
                                if not get_run_text(next_item):
                                    j += 1
                                    continue
                                if temp_ins_count > 0 or temp_del_count > 0 or temp_fmt_count > 0:
                                    next_is_redline = True
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
                            j += 1

                        if next_is_redline:
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

        comment_lines.append(f"{header}: {data['text']}")
        seen_sigs.add(sig)

        if cid in children_map:
            children = children_map[cid]
            children.sort(key=lambda x: comments_map.get(x, {}).get("date", ""))
            for child_id in children:
                render_comment(child_id)

    for ins_map, del_map, comments_set, fmt_map in states_list:
        for uid, meta in ins_map.items():
            sig = f"Chg:{uid}"
            if sig not in seen_sigs:
                auth = meta.author or "Unknown"
                change_lines.append(f"[{sig} insert] {auth}")
                seen_sigs.add(sig)
        for uid, meta in del_map.items():
            sig = f"Chg:{uid}"
            if sig not in seen_sigs:
                auth = meta.author or "Unknown"
                change_lines.append(f"[{sig} delete] {auth}")
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
