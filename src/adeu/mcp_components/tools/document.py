# FILE: src/adeu/mcp/tools/document.py
from pathlib import Path
from typing import Annotated, List, Optional

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.mcp_components.shared import VIEW_URI, _read_file_bytes, _save_stream
from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine


@tool(
    description=(
        "Reads a DOCX file and returns its text content. Use this to ingest the document into your context window."
    ),
    annotations={"readOnlyHint": True},
    meta={"ui": {"resourceUri": VIEW_URI}},
)
async def read_docx(
    file_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    clean_view: Annotated[
        bool,
        "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
    ] = False,
) -> ToolResult:
    await ctx.info(
        f"Reading DOCX file: {Path(file_path).name}",
        extra={"file_path": file_path, "clean_view": clean_view},
    )

    try:
        stream = _read_file_bytes(file_path)
        await ctx.debug(
            "File bytes read successfully into memory",
            extra={"size_bytes": len(stream.getvalue())},
        )

        text = extract_text_from_stream(stream, filename=Path(file_path).name, clean_view=clean_view)
        await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
        return ToolResult(
            content=text,
            structured_content={"markdown": text},
        )

    except FileNotFoundError as e:
        await ctx.error("File not found", extra={"file_path": file_path})
        raise ToolError(f"Error reading file: {str(e)}") from e
    except Exception as e:
        await ctx.error("Failed to parse DOCX", extra={"error": str(e), "file_path": file_path})
        raise ToolError(f"Error reading file: {str(e)}") from e


@tool(
    description="Compares two DOCX files and returns a text-based Unified Diff.",
    annotations={"readOnlyHint": True},
)
async def diff_docx_files(
    original_path: Annotated[str, "Path to the base document."],
    modified_path: Annotated[str, "Path to the new document."],
    ctx: Context,
    compare_clean: Annotated[bool, "If True, compares 'Accepted' state. If False, compares raw text."] = True,
) -> str:
    await ctx.info(
        "Starting document diff",
        extra={
            "original_path": original_path,
            "modified_path": modified_path,
            "compare_clean": compare_clean,
        },
    )

    try:
        await ctx.debug("Extracting text from original document")
        stream_orig = _read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name, clean_view=compare_clean)

        await ctx.debug("Extracting text from modified document")
        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name, clean_view=compare_clean)

        await ctx.debug("Generating text differences")
        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            await ctx.warning("No text differences found between the documents.")
            return "No text differences found between the documents."

        await ctx.info(f"Diff complete. Found {len(edits)} differences.")
        return _create_diff_output(original_path, modified_path, text_orig, edits)

    except Exception as e:
        await ctx.error("Failed to compute diff", extra={"error": str(e)})
        return f"Error computing diff: {str(e)}"


def _create_diff_output(original_path: str, modified_path: str, text_orig: str, edits: List[ModifyText]):
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
    result = "\n".join(output)
    return result


@tool(
    description="Applies a batch of document changes (review actions and text edits).",
    annotations={"destructiveHint": True},
)
async def process_document_batch(
    original_docx_path: Annotated[str, "Absolute path to the source file."],
    author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
    ctx: Context,
    changes: Annotated[
        List[DocumentChange],
        "List of changes to apply. Each change must specify 'type' as 'accept', 'reject', 'reply', or 'modify'.",
    ],
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    await ctx.info(
        "Initializing atomic batch process",
        extra={
            "original_docx_path": original_docx_path,
            "author_name": author_name,
            "changes_count": len(changes) if changes else 0,
        },
    )

    try:
        if not author_name or not author_name.strip():
            await ctx.warning("Batch processing rejected: author_name is empty.")
            return "Error: author_name cannot be empty."

        if not changes:
            await ctx.warning("Batch processing rejected: No actions or edits provided.")
            return "Error: No changes provided."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        await ctx.debug("Redline Engine initialized successfully")

        try:
            await ctx.debug("Processing document batch")
            stats = engine.process_batch(changes)
            await ctx.info("Changes processed successfully", extra=stats)
        except BatchValidationError as e:
            await ctx.error(
                "Batch validation failed",
                extra={
                    "error_count": len(e.errors),
                    "errors": e.errors,
                },
            )
            error_report = "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(e.errors)
            return error_report

        if not output_path:
            p = Path(original_docx_path)
            if p.stem.endswith("_processed") or p.stem.endswith("_redlined"):
                output_path = str(p)
            else:
                output_path = str(p.parent / f"{p.stem}_processed{p.suffix}")

        await ctx.debug(
            "Saving processed document stream to disk",
            extra={"output_path": output_path},
        )
        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        await ctx.info("Batch process complete and saved", extra={"output_path": output_path})

        return (
            f"Batch complete. Saved to: {output_path}\n"
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.\n"
            f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped."
        )

    except Exception as e:
        await ctx.error("Critical error during batch processing", extra={"error": str(e)})
        return f"Error processing batch: {str(e)}"


@tool(
    description="Accepts all tracked changes in the document and removes comments, creating a clean version.",
    annotations={"destructiveHint": True},
)
async def accept_all_changes(
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    await ctx.info(f"Accepting all changes for document: {Path(docx_path).name}")
    try:
        stream = _read_file_bytes(docx_path)
        engine = RedlineEngine(stream)

        await ctx.debug("Engine loaded, executing accept_all_revisions()")
        engine.accept_all_revisions()

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_clean{p.suffix}")

        _save_stream(engine.save_to_stream(), output_path)
        await ctx.info("Clean document saved successfully", extra={"output_path": output_path})

        return f"Accepted all changes. Saved to: {output_path}"
    except Exception as e:
        await ctx.error(
            "Failed to accept all changes",
            extra={"error": str(e), "docx_path": docx_path},
        )
        return f"Error accepting changes: {str(e)}"
