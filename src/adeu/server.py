# FILE: src/adeu/server.py
import logging
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import structlog
from mcp.server.fastmcp import FastMCP

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown as _apply_edits_to_markdown
from adeu.models import DocumentEdit, ReviewAction
from adeu.redline.engine import RedlineEngine

# --- LOGGING CONFIGURATION ---
# MCP communicates over stdio.
# CRITICAL: All logs must go to stderr. Any print to stdout will break the JSON-RPC protocol.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
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

        output = [
            f"--- {Path(original_path).name}",
            f"+++ {Path(modified_path).name}",
            "",
        ]
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
def process_document_batch(
    original_docx_path: str,
    author_name: str,
    actions: Optional[List[ReviewAction]] = None,
    edits: Optional[List[DocumentEdit]] = None,
    output_path: Optional[str] = None,
) -> str:
    """
    ATOMIC PIPELINE: Applies a mixed batch of review actions (ACCEPT/REJECT/REPLY) and text edits in a single operation.

    This is the ONLY tool you should use to modify the document text, accept/reject changes, or reply to comments.
    It reads the document once, resolves all structural review actions, recalculates the Virtual DOM boundaries,
    and then applies text edits safely without misanchoring.

    Matching Strategy for Edits:
    - The tool tries to match `target_text` against the document.
    - It supports 'Fuzzy Matching' to handle extra whitespace or varying legal placeholders (e.g. [___] vs [_____]).

    Args:
        original_docx_path: Absolute path to the source file.
        author_name: Name to appear in Track Changes (e.g., 'Reviewer AI').
        actions: Optional list of review actions (ACCEPT, REJECT, REPLY) to perform FIRST.
                 Target IDs (e.g. "Chg:1" or "Com:101") come from the CriticMarkup output.
        edits: Optional list of text replacements to perform SECOND. Each edit transforms `target_text` -> `new_text`.
        output_path: Optional output path. If not provided, updates the file in place
                     (if it ends in _processed or _redlined) or creates a new one.
    """
    try:
        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        actions = actions or []
        edits = edits or []

        if not actions and not edits:
            return "Error: No actions or edits provided."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)

        applied_actions, skipped_actions = 0, 0
        if actions:
            applied_actions, skipped_actions = engine.apply_review_actions(actions)

            # CRITICAL: Rebuild the mapper so text edits anchor against the post-action DOM state
            if edits:
                engine.mapper._build_map()
                engine.clean_mapper = None
        if edits:
            validation_errors = engine.validate_edits(edits)
            if validation_errors:
                error_report = (
                    f"Batch rejected. {len(validation_errors)} out of {len(edits)} edits failed validation:\n\n"
                    + "\n\n".join(validation_errors)
                )
                return error_report
        applied_edits, skipped_edits = 0, 0
        if edits:
            applied_edits, skipped_edits = engine.apply_edits(edits)

        if not output_path:
            p = Path(original_docx_path)
            if p.stem.endswith("_processed") or p.stem.endswith("_redlined"):
                output_path = str(p)
            else:
                output_path = str(p.parent / f"{p.stem}_processed{p.suffix}")

        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        return (
            f"Batch complete. Saved to: {output_path}\n"
            f"Actions: {applied_actions} applied, {skipped_actions} skipped.\n"
            f"Edits: {applied_edits} applied, {skipped_edits} skipped."
        )

    except Exception as e:
        return f"Error processing batch: {str(e)}"


@mcp.tool()
def accept_all_changes(docx_path: str, output_path: Optional[str] = None) -> str:
    """
    Accepts all tracked changes in the document and removes comments, creating a clean version.
    Useful for finalizing a round of negotiation before starting a new one.
    """
    try:
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


@mcp.tool()
def apply_edits_as_markdown(
    docx_path: str,
    edits: List[DocumentEdit],
    output_path: Optional[str] = None,
    include_index: bool = False,
    highlight_only: bool = False,
    clean_view: bool = True,
) -> str:
    """
    Reads a DOCX file, extracts its text, applies edits as CriticMarkup, and saves as a Markdown file.
    Use this to create a marked-up Markdown version of the document showing proposed changes.

    Args:
        docx_path: Absolute path to the DOCX file.
        edits: List of edits. Each edit has target_text (text to find),
               new_text (replacement), and optional comment.
        output_path: Optional path for the output .md file. If not provided,
                     saves alongside the DOCX with same name but .md extension.
        include_index: If True, appends the edit's 0-based index as [Edit:N] in the markup.
        highlight_only: If True, only highlights target_text with {==...==} notation
                        without applying the actual changes. Useful for showing
                        which parts of the document will be affected.
        clean_view: If True (default), extracts the 'Accepted' state of the document
                    (hides existing deletions, shows insertions). If False, includes
                    existing CriticMarkup in the extracted text.

    Returns:
        Confirmation message with the path to the saved Markdown file, or error message.

        The saved file contains CriticMarkup annotations:
        - Deletions: {--deleted text--}
        - Insertions: {++inserted text++}
        - Modifications: {--old--}{++new++}
        - Comments: {>>comment text<<}
        - Highlights (highlight_only mode): {==highlighted==}
    """
    try:
        # 1. Read and extract text from DOCX
        stream = _read_file_bytes(docx_path)
        markdown_text = extract_text_from_stream(
            stream,
            filename=Path(docx_path).name,
            clean_view=clean_view,
        )

        # 2. Apply edits to the extracted text
        result = _apply_edits_to_markdown(
            markdown_text=markdown_text,
            edits=edits,
            include_index=include_index,
            highlight_only=highlight_only,
        )

        # 3. Determine output path
        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_markup.md")

        # 4. Save as Markdown file
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result)

        return f"Saved CriticMarkup to: {output_path}"

    except FileNotFoundError:
        return f"Error: File not found: {docx_path}"
    except Exception as e:
        return f"Error applying edits as markdown: {str(e)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
