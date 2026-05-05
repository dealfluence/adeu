# FILE: src/adeu/mcp_components/tools/document.py
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from docx import Document as load_document
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult

from adeu.diff import generate_edits_from_text
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.mcp_components._response_builders import (
    build_outline_response,
    build_paginated_response,
)
from adeu.mcp_components.shared import (
    MARKDOWN_UI_URI,
    add_timing_if_debug,
    read_file_bytes,
    save_stream,
)
from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine


async def _read_docx_disk(
    file_path: str,
    ctx: Context,
    clean_view: bool,
    mode: str = "full",
    page: int = 1,
) -> ToolResult:
    """Core logic for reading a DOCX from disk. Dispatches on `mode`."""
    await ctx.info(
        f"Reading DOCX file: {Path(file_path).name}",
        extra={
            "file_path": file_path,
            "clean_view": clean_view,
            "mode": mode,
            "page": page,
        },
    )

    try:
        stream = read_file_bytes(file_path)
        await ctx.debug(
            "File bytes read successfully into memory",
            extra={"size_bytes": len(stream.getvalue())},
        )

        doc = load_document(stream)
        text = _extract_text_from_doc(doc, clean_view=clean_view)
        await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})

        if mode == "outline":
            return build_outline_response(doc, text, file_path)
        # mode == "full"
        return build_paginated_response(text, page, file_path)

    except ToolError:
        raise
    except FileNotFoundError as e:
        await ctx.error("File not found", extra={"file_path": file_path})
        raise ToolError(f"Error reading file: {str(e)}") from e
    except Exception as e:
        await ctx.error("Failed to parse DOCX", extra={"error": str(e), "file_path": file_path})
        raise ToolError(f"Error reading file: {str(e)}") from e


async def _process_document_batch_disk(
    original_docx_path: str,
    author_name: str,
    ctx: Context,
    changes: List[DocumentChange],
    output_path: Optional[str],
) -> str:
    """Core logic for modifying a DOCX on disk."""
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

        stream = read_file_bytes(original_docx_path)
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
        save_stream(result_stream, output_path)

        await ctx.info("Batch process complete and saved", extra={"output_path": output_path})

        res = (
            f"Batch complete. Saved to: {output_path}\n"
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.\n"
            f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped."
        )
        if stats.get("skipped_details"):
            res += "\n\nSkipped Details:\n" + "\n".join(stats["skipped_details"])
        return res

    except Exception as e:
        await ctx.error("Critical error during batch processing", extra={"error": str(e)})
        return f"Error processing batch: {str(e)}"


@tool(
    description=(
        "Compares two DOCX files and generates a text-based Unified Diff. "
        "Use this to see exactly what changed between two versions of a document. "
        "By default (compare_clean=True), it compares the 'Accepted' finalized states of both documents. "
        "Set compare_clean=False if you need to compare the raw underlying text including Tracked Change CriticMarkup."
    ),
    annotations={"readOnlyHint": True},
)
async def diff_docx_files(
    original_path: Annotated[str, "Path to the base document."],
    modified_path: Annotated[str, "Path to the new document."],
    ctx: Context,
    compare_clean: Annotated[bool, "If True, compares 'Accepted' state. If False, compares raw text."] = True,
) -> str:
    start_time = time.perf_counter()
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
        stream_orig = read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name, clean_view=compare_clean)

        await ctx.debug("Extracting text from modified document")
        stream_mod = read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name, clean_view=compare_clean)

        await ctx.debug("Generating text differences")
        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            await ctx.warning("No text differences found between the documents.")
            return add_timing_if_debug(start_time, "No text differences found between the documents.")

        await ctx.info(f"Diff complete. Found {len(edits)} differences.")
        res = _create_diff_output(original_path, modified_path, text_orig, edits)
        return add_timing_if_debug(start_time, res)

    except Exception as e:
        await ctx.error("Failed to compute diff", extra={"error": str(e)})
        return add_timing_if_debug(start_time, f"Error computing diff: {str(e)}")


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
    description=(
        "Accepts all tracked changes and removes all comments in a single operation, "
        "producing a finalized clean document. "
        "Use this when a document review is entirely complete and you want to clear all redlines. "
        "For selective acceptance/rejection of specific changes, use `process_document_batch` instead."
    ),
    annotations={"destructiveHint": True},
)
async def accept_all_changes(
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    start_time = time.perf_counter()
    await ctx.info(f"Accepting all changes for document: {Path(docx_path).name}")
    try:
        stream = read_file_bytes(docx_path)
        engine = RedlineEngine(stream)

        await ctx.debug("Engine loaded, executing accept_all_revisions()")
        engine.accept_all_revisions()

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_clean{p.suffix}")

        save_stream(engine.save_to_stream(), output_path)
        await ctx.info("Clean document saved successfully", extra={"output_path": output_path})

        return add_timing_if_debug(start_time, f"Accepted all changes. Saved to: {output_path}")
    except Exception as e:
        await ctx.error(
            "Failed to accept all changes",
            extra={"error": str(e), "docx_path": docx_path},
        )
        return add_timing_if_debug(start_time, f"Error accepting changes: {str(e)}")


@tool(
    description="Opens a local file in its native desktop application (e.g., Microsoft Word for DOCX files).",
    annotations={"openWorldHint": True},
)
async def open_local_file(
    file_path: Annotated[str, "Absolute path to the file to open."],
    ctx: Context,
) -> str:
    start_time = time.perf_counter()
    await ctx.info(f"Opening file in native app: {file_path}")
    p = Path(file_path)
    if not p.exists():
        raise ToolError(f"File not found: {file_path}")

    try:
        if sys.platform == "win32":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)], check=True)
        else:
            subprocess.run(["xdg-open", str(p)], check=True)
        return add_timing_if_debug(start_time, f"Successfully opened {p.name} in its native application.")
    except Exception as e:
        await ctx.error("Failed to open file", extra={"error": str(e)})
        raise ToolError(f"Failed to open file: {e}") from e


# ==========================================
# TOOL DESCRIPTION CONSTANTS (DRY)
# ==========================================

READ_DOCX_COMMON_DESC = (
    "Reads a DOCX file and extracts its text content. Use this to ingest documents into your context window.\n"
)
READ_DOCX_WIN32_EXTRA = (
    "Auto-Routing: If the provided file is currently open in Microsoft Word, "
    "Adeu will automatically sync with the live window. "
    "If you don't know the file path yet and want to read whatever document "
    "the user is currently working on, LEAVE `file_path` EMPTY!\n"
)
READ_DOCX_TAIL = (
    "By default (clean_view=False), it returns text with inline CriticMarkup "
    "(e.g., {++inserted++}, {--deleted--}, {==highlighted==}{>>comment<<}) "
    "representing Tracked Changes and Comments. "
    "Set clean_view=True ONLY if you want to read the final, clean text, ignoring all redlines and comments.\n\n"
    "PAGINATION & OUTLINE:\n"
    "- mode='outline' returns a structural map of headings with page numbers, styles, "
    "table presence, and referenced footnotes. Body content is omitted. Use this first "
    "on large documents to plan targeted reads.\n"
    "- mode='full' (default) returns the document body. Documents over ~19,000 characters "
    "are split into pages; use page=N to read a specific page (1-indexed). Documents under "
    "the limit are returned in full on page 1.\n"
    "- Page boundaries differ between clean_view=True and clean_view=False.\n"
    "- The Structural Appendix (defined terms, anchors, diagnostics) is repeated on every page."
)

PROCESS_BATCH_COMMON_DESC = (
    "Applies a batch of structural edits, text modifications, and review actions to a document. "
    "This is your primary tool for editing DOCX files.\n\n"
    "CRITICAL: All changes in the batch evaluate against the ORIGINAL document state. "
    "Do not send sequential edits that depend on each other within the same batch "
    "(e.g. rename X to Y, then modify Y). "
    "Instead, apply the rename in one batch, then modify Y in a subsequent batch.\n\n"
)
PROCESS_BATCH_WIN32_EXTRA = (
    "Auto-Routing: If the provided file is currently open in Microsoft Word, "
    "Adeu will automatically execute these edits live on the canvas. "
    "If you want to apply edits to the user's currently active document "
    "and don't know the path, LEAVE `original_docx_path` EMPTY!\n\n"
)
PROCESS_BATCH_OPERATIONS_DESC = (
    "The `changes` parameter is a list of operations. Each item MUST have a `type`:\n"
    "1. 'modify': Search-and-replace text. Provide exact `target_text` (CRITICAL: include "
    "surrounding context if the word appears multiple times to ensure unique matching) and "
    "`new_text` (the replacement). `new_text` supports full Markdown structure: "
    "'# Heading 1' through '###### Heading 6' at the start of a line for heading styles, "
    "'**bold**' and '_italic_' inline formatting, and blank lines ('\\n\\n') to split "
    "`new_text` into multiple paragraphs. Multi-paragraph inserts are tracked as one "
    "logical revision. To delete text, make `new_text` empty. Do NOT manually write "
    "CriticMarkup tags ({++, {--, {>>). To add a comment, use the 'comment' parameter.\n"
    "2. 'accept': Finalize a tracked change. Requires `target_id` (e.g., 'Chg:12').\n"
    "3. 'reject': Revert a tracked change. Requires `target_id` (e.g., 'Chg:12').\n"
    "4. 'reply': Reply to a comment. Requires `target_id` (e.g., 'Com:5') and `text`.\n"
    "5. 'insert_row': Insert table row. Requires `target_text` (anchor), `position` "
    "('above'/'below'), and `cells` (Markdown strings).\n"
    "6. 'delete_row': Delete table row. Requires `target_text` inside the row to be deleted.\n\n"
    "CRITICAL ID VOLATILITY: Tracked Change and Comment IDs (e.g. 'Chg:12', 'Com:5') are strictly session-bound "
    "and MAY BE RENUMBERED by Microsoft Word dynamically (e.g., during background AutoSaves). "
    "You MUST call `read_docx` immediately before executing any `accept`, `reject`, or `reply` operations "
    "to guarantee you have the freshest IDs. Do not cache IDs across tool calls.\n\n"
    "Always provide a realistic `author_name` for Tracked Changes. This name will be used for "
    "attribution in the document's tracked changes and comments."
)


# ==========================================
# PLATFORM CONDITIONAL TOOL REGISTRATION
# ==========================================

if sys.platform == "win32":
    from adeu.mcp_components.tools.live_word import (
        open_word_document_impl,
        process_active_word_batch,
        read_active_word_document,
        save_active_word_document_impl,
    )

    @tool(
        description=READ_DOCX_COMMON_DESC + READ_DOCX_WIN32_EXTRA + READ_DOCX_TAIL,
        annotations={"readOnlyHint": True},
        meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
    )
    async def read_docx(
        ctx: Context,
        file_path: Annotated[
            Optional[str],
            "Path to the DOCX file. LEAVE EMPTY (Null) to read the live Word document!",
        ] = None,
        clean_view: Annotated[
            bool,
            "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
        ] = False,
        mode: Annotated[
            Literal["full", "outline"],
            "'full' returns body content (paginated for large docs). 'outline' returns "
            "a structural heading map with page numbers; body content is omitted.",
        ] = "full",
        page: Annotated[
            int,
            "Page number (1-indexed) for mode='full'. Defaults to 1. Ignored when mode='outline'.",
        ] = 1,
    ) -> ToolResult:
        start_time = time.perf_counter()
        if not file_path:
            # Read active document directly. No disk fallback available if this fails.
            res = await read_active_word_document(ctx, clean_view, None, mode=mode, page=page)
        else:
            # Try Live Word first. Fallback to Disk if Word is closed or document isn't open.
            try:
                res = await read_active_word_document(ctx, clean_view, file_path, mode=mode, page=page)
                await ctx.debug("Read document via Live Word COM.")
            except ToolError:
                # ToolError = Live Word read succeeded but the request itself failed
                # (e.g. page out of range). Do not fall back to disk; the disk doc
                # would produce the same error and might also have stale content.
                raise
            except Exception:
                # Any other exception means Live Word couldn't extract at all
                # (e.g. doc not open, COM unavailable). Fall back to disk.
                await ctx.debug("Document not open in live Word, falling back to disk read.")
                res = await _read_docx_disk(file_path, ctx, clean_view, mode, page)
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_WIN32_EXTRA + PROCESS_BATCH_OPERATIONS_DESC,
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
        ctx: Context,
        changes: Annotated[
            List[DocumentChange],
            "List of changes to apply. Each change must specify 'type'.",
        ],
        original_docx_path: Annotated[
            Optional[str],
            "Path to source file. LEAVE EMPTY (Null) to edit the live Word document!",
        ] = None,
        output_path: Annotated[
            Optional[str],
            "Optional output path (only used if original_docx_path is provided).",
        ] = None,
    ) -> str:
        start_time = time.perf_counter()
        if not original_docx_path:
            # Edit active document directly. No disk fallback available.
            res = await process_active_word_batch(ctx, changes, author_name, None)
        else:
            # Try Live Word first. Fallback to Disk if Word is closed or document isn't open.
            try:
                res = await process_active_word_batch(ctx, changes, author_name, original_docx_path)
            except Exception:
                await ctx.debug("Document not open in live Word, falling back to disk edit.")
                res = await _process_document_batch_disk(original_docx_path, author_name, ctx, changes, output_path)
        return add_timing_if_debug(start_time, res)

    if os.getenv("ADEU_ENABLE_TEST_TOOLS") in ("1", "true", "True", "yes"):

        @tool(
            description=(
                "Performs a deep, structural XML diff between two DOCX files. "
                "Bypasses the virtual Markdown representation to show raw OOXML changes "
                "(e.g., w:ins, w:del, property changes). Essential for debugging the redline engine."
            ),
            annotations={"readOnlyHint": True},
        )
        async def debug_xml_diff(
            file_a: Annotated[str, "Absolute path to the first/baseline DOCX file."],
            file_b: Annotated[str, "Absolute path to the second/modified DOCX file."],
            ctx: Context,
        ) -> str:
            start_time = time.perf_counter()
            await ctx.info(f"Generating XML diff between {Path(file_a).name} and {Path(file_b).name}")
            import difflib

            from adeu.utils.xml_debug import get_abstracted_xml_snapshot

            try:
                xml_a = get_abstracted_xml_snapshot(file_a)
                xml_b = get_abstracted_xml_snapshot(file_b)

                # R6 Fix: Strip noisy rsid and paraId metadata to speed up difflib
                import re

                xml_a = re.sub(r'\s*w:rsid[RPT]?="[^"]*"', "", xml_a)
                xml_a = re.sub(r'\s*w14:paraId="[^"]*"', "", xml_a)
                xml_a = re.sub(r'\s*w14:textId="[^"]*"', "", xml_a)

                xml_b = re.sub(r'\s*w:rsid[RPT]?="[^"]*"', "", xml_b)
                xml_b = re.sub(r'\s*w14:paraId="[^"]*"', "", xml_b)
                xml_b = re.sub(r'\s*w14:textId="[^"]*"', "", xml_b)

                # R7 Fix: Normalize whitespace between tags to exactly one newline to eliminate formatting noise
                xml_a = re.sub(r">\s+<", ">\n<", xml_a)
                xml_b = re.sub(r">\s+<", ">\n<", xml_b)

                diff_lines = list(
                    difflib.unified_diff(
                        xml_a.splitlines(),
                        xml_b.splitlines(),
                        fromfile="Baseline",
                        tofile="Modified",
                        lineterm="",
                    )
                )
                res = "No structural XML differences found." if not diff_lines else "\n".join(diff_lines)

                # R5 Fix: Truncate inline diff and provide spill file
                if len(res) > 150_000:
                    import tempfile

                    fd, path = tempfile.mkstemp(suffix=".diff", prefix="adeu_xml_diff_")
                    with open(fd, "w", encoding="utf-8") as f:
                        f.write(res)
                    res = res[:150_000] + f"\n\n... [Diff truncated to 150KB. Full diff saved to host at:\n{path}]"
                return add_timing_if_debug(start_time, res)
            except Exception as e:
                await ctx.error("Failed to generate XML diff", extra={"error": str(e)})
                raise ToolError(f"Failed to generate XML diff: {e}") from e

        @tool(
            description=(
                "Opens a DOCX file from disk into the live Microsoft Word application. "
                "Essential for automated exploratory testing and ensuring Word has the document active."
            ),
        )
        async def open_word_document(
            ctx: Context,
            file_path: Annotated[str, "Absolute path to the DOCX file to open in Word."],
            visible: Annotated[bool, "Whether to make the Word application window visible."] = True,
        ) -> str:
            start_time = time.perf_counter()
            res = await open_word_document_impl(ctx, file_path, visible)
            return add_timing_if_debug(start_time, res)

        @tool(
            description="Saves the currently active Microsoft Word document to disk. Optionally closes it after saving."
        )
        async def save_active_word_document(
            ctx: Context,
            output_path: Annotated[
                Optional[str],
                "Optional absolute path to 'Save As'. If omitted, overwrites the current file.",
            ] = None,
            close: Annotated[bool, "Whether to close the document in Word after saving."] = False,
        ) -> str:
            start_time = time.perf_counter()
            res = await save_active_word_document_impl(ctx, output_path, close)
            return add_timing_if_debug(start_time, res)

else:

    @tool(
        description=READ_DOCX_COMMON_DESC + READ_DOCX_TAIL,
        annotations={"readOnlyHint": True},
        meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
    )
    async def read_docx(
        file_path: Annotated[str, "Absolute path to the DOCX file."],
        ctx: Context,
        clean_view: Annotated[
            bool,
            "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
        ] = False,
        mode: Annotated[
            Literal["full", "outline"],
            "'full' returns body content (paginated for large docs). 'outline' returns "
            "a structural heading map with page numbers; body content is omitted.",
        ] = "full",
        page: Annotated[
            int,
            "Page number (1-indexed) for mode='full'. Defaults to 1. Ignored when mode='outline'.",
        ] = 1,
    ) -> ToolResult:
        start_time = time.perf_counter()
        res = await _read_docx_disk(file_path, ctx, clean_view, mode, page)
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        original_docx_path: Annotated[str, "Absolute path to the source file."],
        author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
        ctx: Context,
        changes: Annotated[
            List[DocumentChange],
            "List of changes to apply. Each change must specify 'type'.",
        ],
        output_path: Annotated[Optional[str], "Optional output path."] = None,
    ) -> str:
        start_time = time.perf_counter()
        res = await _process_document_batch_disk(original_docx_path, author_name, ctx, changes, output_path)
        return add_timing_if_debug(start_time, res)
