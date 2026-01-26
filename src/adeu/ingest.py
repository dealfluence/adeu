import io

import structlog
from docx import Document
from docx.text.run import Run

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    apply_formatting_to_segments,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
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
        # Ensure stream is at start
        file_stream.seek(0)
        doc = Document(file_stream)

        comments_mgr = CommentsManager(doc)
        comments_map = comments_mgr.extract_comments_data()

        full_text = []

        for part in iter_document_parts(doc):
            # 1. Paragraphs
            for para in part.paragraphs:
                # Add Markdown prefix if heading
                prefix = get_paragraph_prefix(para)

                # Build content
                p_text = _build_paragraph_text(para, comments_map, clean_view)

                full_text.append(prefix + p_text)

            # 2. Tables
            for table in part.tables:
                for row in table.rows:
                    row_parts = []
                    for cell in row.cells:
                        # Cell paragraphs
                        cell_text_parts = []
                        for p in cell.paragraphs:
                            prefix = get_paragraph_prefix(p)
                            p_content = _build_paragraph_text(p, comments_map, clean_view)
                            cell_text_parts.append(prefix + p_content)

                        cell_text = "\n".join(cell_text_parts)
                        if cell_text:
                            row_parts.append(cell_text)

                    if row_parts:
                        full_text.append(" | ".join(row_parts))

        return "\n\n".join(full_text)

    except Exception as e:
        logger.error(f"Text extraction failed: {e}", exc_info=True)
        raise ValueError(f"Could not extract text: {str(e)}") from e


def _build_paragraph_text(paragraph, comments_map, clean_view: bool = False):
    """
    Flatten overlapping comments into sequential CriticMarkup blocks.
    Merges metadata for adjacent Redline blocks (Substitutions).
    """
    parts = []

    active_ins: dict[str, DocxEvent] = {}
    active_del: dict[str, DocxEvent] = {}
    active_comments: set[str] = set()

    # Buffer for deferred metadata (used for merging substitution blocks)
    # List of (active_ins_snapshot, active_del_snapshot, active_comments_snapshot)
    # Buffer for deferred metadata (used for merging substitution blocks)
    deferred_meta_states = []

    # State for Run Coalescing
    # We buffer text segments as long as the wrapper state (start/end tokens) remains identical
    pending_text = ""
    current_wrappers = ("", "")  # (start, end)

    # Pre-calculate item list to allow lookahead
    items = list(iter_paragraph_content(paragraph))

    for i, item in enumerate(items):
        if isinstance(item, Run):
            prefix, suffix = get_run_style_markers(item)
            text = get_run_text(item)

            # Clean View Logic: Skip deleted text
            if clean_view and active_del:
                continue

            seg = apply_formatting_to_segments(text, prefix, suffix)
            if seg:
                # 1. Determine Wrappers
                if clean_view:
                    new_wrappers = ("", "")
                else:
                    new_wrappers = _get_wrappers(active_ins, active_del, active_comments)

                # 2. Check if we can merge with pending text
                if pending_text and new_wrappers == current_wrappers:
                    # Same state -> Merge
                    pending_text += seg
                else:
                    # Different state -> Flush pending
                    if pending_text:
                        s_tok, e_tok = current_wrappers
                        parts.append(f"{s_tok}{pending_text}{e_tok}")

                    # Start new buffer
                    pending_text = seg
                    current_wrappers = new_wrappers

                # 3. Handle Metadata (always accumulate state snapshot)
                # In Clean View, we suppress CriticMarkup metadata block output
                # unless we want to support comments in clean view?
                # For now, Clean View implies "Final Document Text", so no inline metadata.
                if not clean_view:
                    current_state = (active_ins.copy(), active_del.copy(), active_comments.copy())
                    deferred_meta_states.append(current_state)

                    should_defer = False
                    is_redline = bool(active_ins) or bool(active_del)

                    if is_redline:
                        # Lookahead
                        j = i + 1
                        next_is_redline = False

                        temp_ins = bool(active_ins)
                        temp_del = bool(active_del)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, Run):
                                if temp_ins or temp_del:
                                    next_is_redline = True
                                break
                            elif isinstance(next_item, DocxEvent):
                                if next_item.type == "ins_start":
                                    temp_ins = True
                                elif next_item.type == "ins_end":
                                    temp_ins = False
                                elif next_item.type == "del_start":
                                    temp_del = True
                                elif next_item.type == "del_end":
                                    temp_del = False
                            j += 1

                        if next_is_redline:
                            should_defer = True

                    if not should_defer:
                        # Before flushing metadata, ensure pending text is flushed
                        # This ensures {++Text++}{>>Meta<<} order
                        if pending_text:
                            s_tok, e_tok = current_wrappers
                            parts.append(f"{s_tok}{pending_text}{e_tok}")
                            pending_text = ""
                            current_wrappers = ("", "")

                        meta_block = _build_merged_meta_block(deferred_meta_states, comments_map)
                        if meta_block:
                            parts.append(f"{{>>{meta_block}<<}}")
                        deferred_meta_states = []

        elif isinstance(item, DocxEvent):
            # Event occurred -> State change implies we must flush text buffer
            if pending_text:
                s_tok, e_tok = current_wrappers
                parts.append(f"{s_tok}{pending_text}{e_tok}")
                pending_text = ""
                current_wrappers = ("", "")

            # Update State
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

    # Final Flush
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

    # Pre-process comments to find children for threading
    # Map: parent_id -> list of child_ids
    children_map: dict[str, list[str]] = {}
    for c_id, data in comments_map.items():
        p_id = data.get("parent_id")
        if p_id:
            children_map.setdefault(p_id, []).append(c_id)

    # Helper for recursive rendering
    def render_comment(cid):
        if cid not in comments_map:
            return

        sig = f"Com:{cid}"
        if sig in seen_sigs:
            return

        data = comments_map[cid]
        header = f"[{sig}] {data['author']}"
        if data["date"]:
            # Simplify date if present
            try:
                date_str = data["date"].split("T")[0]
                header += f" @ {date_str}"
            except Exception:
                pass

        comment_lines.append(f"{header}: {data['text']}")
        seen_sigs.add(sig)

        # Render Children recursively
        if cid in children_map:
            # Sort children by Date to ensure deterministic threaded order
            children = children_map[cid]
            # ISO 8601 dates sort correctly as strings
            children.sort(key=lambda x: comments_map.get(x, {}).get("date", ""))
            for child_id in children:
                render_comment(child_id)

    for ins_map, del_map, comments_set in states_list:
        # 1. Changes (Ins & Del)
        for map_obj in (ins_map, del_map):
            for uid, meta in map_obj.items():
                sig = f"Chg:{uid}"
                if sig not in seen_sigs:
                    auth = meta.author or "Unknown"
                    change_lines.append(f"[{sig}] {auth}")
                    seen_sigs.add(sig)

        # 2. Comments (Roots only)
        # comments_set contains IDs visible in the document range.
        # We assume these are roots (or at least anchors).
        # We render them and their children.
        for root_id in sorted(comments_set):
            render_comment(root_id)

    # Return Changes first, then Comments
    return "\n".join(change_lines + comment_lines)
