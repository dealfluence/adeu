import logging
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import structlog
from mcp.server.fastmcp import FastMCP

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine

# --- LOGGING CONFIGURATION ---
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

mcp = FastMCP("Adeu Redlining Service")


def _read_file_bytes(path: str) -> BytesIO:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(p, "rb") as f:
        return BytesIO(f.read())


def _save_stream(stream: BytesIO, path: str):
    with open(path, "wb") as f:
        f.write(stream.getvalue())


@mcp.tool()
def read_docx(file_path: str) -> str:
    """
    Reads a local DOCX file and returns its final text content as Markdown (with all tracked changes accepted).
    Use this to understand the document content before proposing edits.
    """
    try:
        stream = _read_file_bytes(file_path)
        return extract_text_from_stream(stream, filename=Path(file_path).name)
    except Exception as e:
        return f"Error reading file: {str(e)}"


@mcp.tool()
def diff_docx_files(original_path: str, modified_path: str) -> str:
    """
    Compares two DOCX files and returns a Semantic Unified Diff.
    """
    try:
        stream_orig = _read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name)

        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name)

        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            return "No text differences found between the documents."

        output = [f"--- {Path(original_path).name}", f"+++ {Path(modified_path).name}", ""]
        CONTEXT_SIZE = 40

        for edit in edits:
            start_idx = getattr(edit, "_match_start_index", 0) or 0
            pre_start = max(0, start_idx - CONTEXT_SIZE)
            pre_context = text_orig[pre_start:start_idx]
            if pre_start > 0:
                pre_context = "..." + pre_context

            target_len = len(edit.target_text) if edit.target_text else 0
            # Heuristic for post-context since we don't know exact Op here easily
            # We assume index + target_len is end of change
            post_start = start_idx + target_len

            post_end = min(len(text_orig), post_start + CONTEXT_SIZE)
            post_context = text_orig[post_start:post_end]
            if post_end < len(text_orig):
                post_context = post_context + "..."

            pre_context = pre_context.replace("\n", " ").replace("\r", "")
            post_context = post_context.replace("\n", " ").replace("\r", "")

            output.append("@@ Word Patch @@")
            output.append(f" {pre_context}")
            if edit.target_text:
                output.append(f"- {edit.target_text}")
            if edit.new_text:
                output.append(f"+ {edit.new_text}")
            output.append(f" {post_context}")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        return f"Error computing diff: {str(e)}"


@mcp.tool()
def apply_structured_edits(
    original_docx_path: str, edits: List[DocumentEdit], author_name: str, output_path: Optional[str] = None
) -> str:
    """
    Applies a specific list of text replacements to the DOCX file
    and saves to a NEW output file, leaving the original unchanged.

    The logic is "Search and Replace".
    - To Delete: Provide target_text, leave new_text empty.
    - To Insert: Provide target_text (context), set new_text to "context + new stuff".
    - To Modify: Provide target_text, set new_text to desired state.

    Args:
        original_docx_path: Absolute path to the source file.
        edits: List of edits to apply.
        author_name: The name of the person making the changes (e.g. "Mikko Korpela").
                     This appears in the Track Changes metadata. Do NOT default to "AI" unless instructed.
        output_path: Optional. If NOT provided, a new file is automatically created in the
                     same directory as the original, named "{original_filename}_redlined.docx".
                     Use this default to avoid path errors.
    """
    try:
        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        applied, skipped = engine.apply_edits(edits)

        if not output_path:
            p = Path(original_docx_path)
            output_path = str(p.parent / f"{p.stem}_redlined{p.suffix}")

        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        return f"Applied {applied} edits. Skipped {skipped} edits. Saved to: {output_path}"

    except Exception as e:
        return f"Error applying edits: {str(e)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
