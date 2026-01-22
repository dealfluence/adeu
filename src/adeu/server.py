import logging
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import structlog
from mcp.server.fastmcp import FastMCP

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit, ReviewAction
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
def read_docx(file_path: str, clean_view: bool = False) -> str:
    """
    Reads a DOCX file and returns its text content.

    Args:
        file_path: Absolute path to the DOCX file.
        clean_view: If False (default), returns the 'Raw' text with inline CriticMarkup ({--del--}{++ins++})
                    so you can see existing redlines and comments.
                    If True, returns the 'Accepted' text (hides deletions, shows insertions) - useful for
                    seeing the clean final state.
    """
    try:
        stream = _read_file_bytes(file_path)
        return extract_text_from_stream(stream, filename=Path(file_path).name, clean_view=clean_view)
    except Exception as e:
        return f"Error reading file: {str(e)}"


@mcp.tool()
def diff_docx_files(original_path: str, modified_path: str, compare_clean: bool = True) -> str:
    """
    Compares two DOCX files and returns a text-based Unified Diff.

    Args:
        original_path: Path to the base document.
        modified_path: Path to the new document.
        compare_clean: If True (default), compares the 'Accepted' state of both docs (ignores tracking markup).
                       This mimics Word's 'Compare Documents' feature.
                       If False, compares the raw text including existing redline markup (useful for debugging
                       markup changes).
    """
    try:
        stream_orig = _read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name, clean_view=compare_clean)

        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name, clean_view=compare_clean)

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
    Applies a list of text replacements to the DOCX file (Track Changes).

    Matching Strategy:
    - The tool tries to match `target_text` against the document.
    - It supports 'Fuzzy Matching' to handle extra whitespace or varying legal placeholders (e.g. [___] vs [_____]).
    - It can match against the 'Accepted' view of the document even if the raw file contains deleted text.

    Args:
        original_docx_path: Absolute path to the source file.
        edits: List of edits. Each edit transforms `target_text` -> `new_text`.
        author_name: Name to appear in Track Changes (e.g., 'Reviewer AI').
        output_path: Optional. If not provided, updates the file in place (if it
        ends in _redlined) or creates a new one.
    """
    try:
        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        applied, skipped = engine.apply_edits(edits)

        if not output_path:
            p = Path(original_docx_path)
            if p.stem.endswith("_redlined"):
                output_path = str(p)  # Overwrite if already redlined
            else:
                output_path = str(p.parent / f"{p.stem}_redlined{p.suffix}")

        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        return f"Applied {applied} edits. Skipped {skipped} edits. Saved to: {output_path}"

    except Exception as e:
        return f"Error applying edits: {str(e)}"


@mcp.tool()
def manage_review_actions(
    original_docx_path: str, actions: List[ReviewAction], author_name: str, output_path: Optional[str] = None
) -> str:
    """
    Manages existing Track Changes and Comments in the document.
    Use this to ACCEPT or REJECT specific edits (by ID), or REPLY to comments.

    Args:
        original_docx_path: Absolute path to the source file.
        actions: List of actions to perform (ACCEPT, REJECT, REPLY).
                 Target IDs (e.g. "Chg:1" or "Com:101") come from the CriticMarkup output.
        author_name: Name of the reviewer.
        output_path: Optional output path.
    """
    try:
        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        applied, skipped = engine.apply_review_actions(actions)

        if not output_path:
            p = Path(original_docx_path)
            if p.stem.endswith("_reviewed"):
                output_path = str(p)
            else:
                output_path = str(p.parent / f"{p.stem}_reviewed{p.suffix}")

        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        return f"Applied {applied} actions. Skipped {skipped} actions. Saved to: {output_path}"

    except Exception as e:
        return f"Error managing actions: {str(e)}"


@mcp.tool()
def accept_all_changes(docx_path: str, output_path: Optional[str] = None) -> str:
    """
    Accepts all tracked changes in the document and removes comments, creating a clean version.
    Useful for finalizing a round of negotiation before starting a new one.
    """
    try:
        # We can simulate this by reading the document and accepting changes?
        # python-docx doesn't strictly have an "Accept All" feature natively that handles complex XML perfectly.
        # However, we can use a simpler approach:
        # 1. Load Doc.
        # 2. Iterate and remove <w:del>.
        # 3. Unwrap <w:ins>.
        # 4. Remove comments.
        #
        # Ideally, we should add this logic to RedlineEngine as a utility.
        # For now, let's implement a basic version here or defer to engine.

        # Let's add the method to engine.py for robustness.
        stream = _read_file_bytes(docx_path)
        engine = RedlineEngine(stream)
        engine.accept_all_revisions()

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_clean{p.suffix}")

        _save_stream(engine.save_to_stream(), output_path)
        return f"Accepted all changes. Saved to: {output_path}"
    except Exception as e:
        return f"Error accepting changes: {str(e)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
