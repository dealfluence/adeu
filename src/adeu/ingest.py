# FILE: src/adeu/ingest.py
import io

import structlog
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.domain import build_structural_appendix
from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    apply_formatting_to_segments,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
    iter_block_items,
    iter_document_parts,
    iter_paragraph_content,
    normalize_docx,
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

        # FIX C: Normalize in-memory DOM so ingest sees coalesced runs regardless
        # of whether the source is an on-disk file or a Flat OPC synthesised stream
        # from live Word (which emits run fragmentation from cursor/undo state).
        normalize_docx(doc)

        comments_mgr = CommentsManager(doc)
        comments_map = comments_mgr.extract_comments_data()

        full_text = []

        for part in iter_document_parts(doc):
            part_text = _extract_blocks(part, comments_map, clean_view)
            if part_text:
                full_text.append(part_text)

        base_text = "\n\n".join(full_text)
        appendix = build_structural_appendix(doc, base_text)
        if appendix:
            return base_text + appendix
        return base_text

    except Exception as e:
        logger.error(f"Text extraction failed: {e}", exc_info=True)
        raise ValueError(f"Could not extract text: {str(e)}") from e


def _extract_blocks(container, comments_map, clean_view: bool) -> str:
    """
    Recursively extracts text from a container (Document, Cell, Header, etc.)
    iterating over Paragraphs and Tables in order.
    """
    blocks = []

    c_type = type(container).__name__
    if c_type == "NotesPart":
        header = "## Footnotes" if container.note_type == "fn" else "## Endnotes"
        blocks.append(f"---\n{header}")

    is_first_para = True
    for item in iter_block_items(container):
        i_type = type(item).__name__

        if i_type == "FootnoteItem":
            fn_text = _extract_blocks(item, comments_map, clean_view)
            if fn_text:
                blocks.append(fn_text)
        elif isinstance(item, Paragraph):
            prefix = get_paragraph_prefix(item)
            if is_first_para and c_type == "FootnoteItem":
                prefix = f"[^{container.note_type}-{container.id}]: " + prefix
            p_text = _build_paragraph_text(item, comments_map, clean_view)
            blocks.append(prefix + p_text)
            is_first_para = False

        elif isinstance(item, Table):
            table_text = _extract_table(item, comments_map, clean_view)
            if table_text:
                blocks.append(table_text)
            is_first_para = False

    return "\n\n".join(blocks)


def _extract_table(table: Table, comments_map, clean_view: bool) -> str:
    rows_text = []
    for row in table.rows:
        cell_texts = []
        seen_cells = set()

        for cell in row.cells:
            if cell in seen_cells:
                continue
            seen_cells.add(cell)

            cell_content = _extract_blocks(cell, comments_map, clean_view)
            cell_texts.append(cell_content)

        row_str = " | ".join(cell_texts)
        rows_text.append(row_str)

    return "\n".join(rows_text)


def _build_paragraph_text(paragraph, comments_map, clean_view: bool = False):
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

    deferred_meta_states = []

    pending_text = ""
    current_wrappers = ("", "")  # CriticMarkup tokens, e.g. ("{++", "++}")
    # `current_style` tracks the style of the trailing segment in pending_text,
    # used only to decide whether the next incoming run can elide adjacent markers.
    current_style = ("", "")

    items = list(iter_paragraph_content(paragraph))

    for i, item in enumerate(items):
        if isinstance(item, Run):
            prefix, suffix = get_run_style_markers(item)
            text = get_run_text(item)

            if clean_view and active_del:
                continue

            seg = apply_formatting_to_segments(text, prefix, suffix)
            if seg:
                if clean_view:
                    new_wrappers = ("", "")
                else:
                    new_wrappers = _get_wrappers(active_ins, active_del, active_comments)
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
                    current_state = (
                        active_ins.copy(),
                        active_del.copy(),
                        active_comments.copy(),
                    )
                    deferred_meta_states.append(current_state)

                    should_defer = False
                    is_redline = bool(active_ins) or bool(active_del)

                    if is_redline:
                        j = i + 1
                        next_is_redline = False
                        temp_ins_count = len(active_ins)
                        temp_del_count = len(active_del)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, Run):
                                if not get_run_text(next_item):
                                    j += 1
                                    continue
                                if temp_ins_count > 0 or temp_del_count > 0:
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
                            j += 1

                        if next_is_redline:
                            should_defer = True

                    if not should_defer:
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


def _get_wrappers(active_ins, active_del, active_comments):
    if active_del:
        return "{--", "--}"
    elif active_ins:
        return "{++", "++}"
    elif active_comments:
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

    for ins_map, del_map, comments_set in states_list:
        for map_obj in (ins_map, del_map):
            for uid, meta in map_obj.items():
                sig = f"Chg:{uid}"
                if sig not in seen_sigs:
                    auth = meta.author or "Unknown"
                    change_lines.append(f"[{sig}] {auth}")
                    seen_sigs.add(sig)

        for root_id in sorted(comments_set):
            render_comment(root_id)

    return "\n".join(change_lines + comment_lines)
