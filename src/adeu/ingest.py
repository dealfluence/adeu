import io

import structlog
from docx import Document
from docx.text.run import Run

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
    iter_document_parts,
    iter_paragraph_content,
)

logger = structlog.get_logger(__name__)


def extract_text_from_stream(file_stream: io.BytesIO, filename: str = "document.docx") -> str:
    """
    Extracts text from a file stream using raw run concatenation.
    Includes Markdown headers (#) and CriticMarkup Comments ({==Text==}{>>Comment<<}).

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
                p_text = _build_paragraph_text(para, comments_map)

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
                            p_content = _build_paragraph_text(p, comments_map)
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


def _build_paragraph_text(paragraph, comments_map):
    """
    Flatten overlapping comments into sequential CriticMarkup blocks.

    Logic:
    - Iterate stream.
    - Keep 'current_segment_text'.
    - Keep 'active_ids' set.
    - If active_ids changes (start/end event):
        - If segment has text:
             - Emit Highlight: {==TEXT==} (only if active_ids > 0)
             - Emit Text: TEXT (if active_ids == 0)
             - Emit Metadata: {>>...<<} (for PREVIOUS active_ids)
        - Reset segment text.
        - Update active_ids.
    """
    parts = []

    active_ins: dict[str, DocxEvent] = {}
    active_del: dict[str, DocxEvent] = {}
    active_comments: set[str] = set()

    current_segment_text = ""

    for item in iter_paragraph_content(paragraph):
        if isinstance(item, Run):
            prefix, suffix = get_run_style_markers(item)
            text = get_run_text(item)

            # Accumulate text.
            # Flush immediately to ensure state accuracy per run.
            seg = f"{prefix}{text}{suffix}"
            if seg:
                formatted = _format_segment(seg, active_ins, active_del, active_comments, comments_map)
                parts.append(formatted)

        elif isinstance(item, DocxEvent):
            # Flush current segment using OLD active_ids
            # Note: with immediate flush logic above, current_segment_text is effectively unused
            # but kept if we switch back to buffering logic.
            if current_segment_text:
                # (Legacy buffer flush logic removed for simplicity in this diff)
                pass

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

    # Flush final segment (unused in immediate mode)
    return "".join(parts)


def _format_segment(text, active_ins, active_del, active_comments, comments_map) -> str:
    if not text:
        return ""

    # 1. Determine Wrapper
    start_token = ""
    end_token = ""

    # Priority: Del > Ins > Comment
    if active_del:
        start_token = "{--"
        end_token = "--}"
    elif active_ins:
        start_token = "{++"
        end_token = "++}"
    elif active_comments:
        start_token = "{=="
        end_token = "==}"
    else:
        return text

    # 2. Build Metadata
    meta_lines = []

    # Ins/Del Metadata
    for i_id, meta in active_ins.items():
        auth = meta.author or "Unknown"
        meta_lines.append(f"[Chg:{i_id}] {auth}")
    for d_id, meta in active_del.items():
        auth = meta.author or "Unknown"
        meta_lines.append(f"[Chg:{d_id}] {auth}")

    # Comment Metadata
    for c_id in sorted(active_comments):
        if c_id in comments_map:
            data = comments_map[c_id]
            header = f"[Com:{c_id}] {data['author']}"
            if data["date"]:
                header += f" @ {data['date'].split('T')[0]}"
            meta_lines.append(f"{header}: {data['text']}")

    meta_block = "\n".join(meta_lines)
    return f"{start_token}{text}{end_token}{{>>{meta_block}<<}}"
