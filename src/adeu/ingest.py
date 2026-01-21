import io

import structlog
from docx import Document
from docx.text.run import Run

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    CommentEvent,
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

    active_ids: set[str] = set()
    current_segment_text = ""

    for item in iter_paragraph_content(paragraph):
        if isinstance(item, Run):
            prefix, suffix = get_run_style_markers(item)
            text = get_run_text(item)
            current_segment_text += f"{prefix}{text}{suffix}"

        elif isinstance(item, CommentEvent):
            # Flush current segment using OLD active_ids
            if current_segment_text:
                if active_ids:
                    parts.append(f"{{=={current_segment_text}==}}")
                    # Emit comments block
                    meta_block = _build_meta_block(active_ids, comments_map)
                    parts.append(f"{{>>{meta_block}<<}}")
                else:
                    parts.append(current_segment_text)

                current_segment_text = ""

            # Update State
            if item.type == "start":
                active_ids.add(item.id)
            elif item.type == "end":
                if item.id in active_ids:
                    active_ids.remove(item.id)
            elif item.type == "ref":
                # We ignore explicit reference tags in the Flattened model because
                # the "End" event triggers the comment emission implicitly.
                pass

    # Flush final segment
    if current_segment_text:
        if active_ids:
            parts.append(f"{{=={current_segment_text}==}}")
            meta_block = _build_meta_block(active_ids, comments_map)
            parts.append(f"{{>>{meta_block}<<}}")
        else:
            parts.append(current_segment_text)

    return "".join(parts)


def _build_meta_block(active_ids, comments_map) -> str:
    """
    Constructs the content inside {>> ... <<}
    Sorts by ID to ensure stability.
    """
    lines = []
    sorted_ids = sorted(list(active_ids))

    for cid in sorted_ids:
        if cid not in comments_map:
            continue

        data = comments_map[cid]
        author = data["author"]
        body = data["text"]
        date_str = data["date"]
        resolved = data["resolved"]

        # Header: [Author @ Date (RESOLVED)]
        header_parts = [author]
        if date_str:
            short_date = date_str.split("T")[0]
            header_parts.append(f"@ {short_date}")
        if resolved:
            header_parts.append("(RESOLVED)")

        header = " ".join(header_parts)

        lines.append(f"[{header}] {body}")

    return "\n".join(lines)
