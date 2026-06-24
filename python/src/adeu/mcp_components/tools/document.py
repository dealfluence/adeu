import asyncio
import json
import os
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional

from docx import Document as load_document
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult

from adeu.diff import generate_edits_from_text
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.mcp_components._response_builders import (
    build_appendix_response,
    build_outline_response,
    build_paginated_response,
)
from adeu.mcp_components.shared import (
    MARKDOWN_UI_URI,
    add_timing_if_debug,
    read_file_bytes,
    save_stream,
)
from pydantic import TypeAdapter, ValidationError

from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes

# Validates a single change dict against the DocumentChange discriminated union,
# selecting the correct model (ModifyText, AcceptChange, etc.) by its 'type'.
_DOCUMENT_CHANGE_ADAPTER: TypeAdapter = TypeAdapter(DocumentChange)


def _coerce_changes(changes: List[Any]) -> List[Any]:
    """Defensively normalize a `changes` list at the tool boundary.

    Some LLM clients "double-serialize" nested arrays, delivering each element
    as a JSON string (e.g. '{"type": "modify", ...}') instead of a parsed
    object/model. The engine dispatches edits by isinstance() against the
    DocumentChange models, so a raw string is silently dropped from both the
    actions and edits lists, yielding a confusing zero-op result rather than a
    crash. Parse stringified elements into the proper model here.

    Elements that are already models/dicts pass through untouched. A string
    that fails to parse or validate is left as-is so downstream handling can
    surface a clear error rather than masking the malformed input.
    """
    if not changes:
        return changes

    coerced: List[Any] = []
    for item in changes:
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                coerced.append(item)
                continue
            if isinstance(parsed, dict):
                try:
                    coerced.append(_DOCUMENT_CHANGE_ADAPTER.validate_python(parsed))
                    continue
                except ValidationError:
                    coerced.append(parsed)
                    continue
            coerced.append(parsed)
        else:
            coerced.append(item)
    return coerced


async def _read_docx_disk(
    file_path: str,
    ctx: Context,
    clean_view: bool,
    mode: str = "full",
    page: int = 1,
    outline_max_level: int = 2,
    outline_verbose: bool = False,
) -> ToolResult:
    """Core logic for reading a DOCX from disk. Dispatches on `mode`."""
    await ctx.info(
        f"Reading DOCX file: {Path(file_path).name}",
        extra={
            "file_path": file_path,
            "clean_view": clean_view,
            "mode": mode,
            "page": page,
            "outline_max_level": outline_max_level,
            "outline_verbose": outline_verbose,
        },
    )

    try:
        stream = read_file_bytes(file_path)
        await ctx.debug(
            "File bytes read successfully into memory",
            extra={"size_bytes": len(stream.getvalue())},
        )

        sanitized_bytes = strip_bom_from_docx_bytes(stream.getvalue())
        doc = load_document(BytesIO(sanitized_bytes))

        # Only mode='appendix' actually consumes the structural appendix in
        # the response. Skipping it for the other modes saves the
        # build_structural_appendix() cost (~8.5s on a 1000-page doc).
        needs_appendix = mode == "appendix"
        # mode='outline' uses paragraph offsets to avoid re-projecting each
        # paragraph (Step 4 / Option A).
        needs_offsets = mode == "outline"

        extract_result = _extract_text_from_doc(
            doc,
            clean_view=clean_view,
            include_appendix=needs_appendix,
            return_paragraph_offsets=needs_offsets,
        )
        if needs_offsets:
            text, paragraph_offsets = extract_result
        else:
            text = extract_result
            paragraph_offsets = None

        await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})

        if mode == "outline":
            return build_outline_response(
                doc,
                text,
                file_path,
                outline_max_level=outline_max_level,
                outline_verbose=outline_verbose,
                paragraph_offsets=paragraph_offsets,
            )
        if mode == "appendix":
            return build_appendix_response(text, page, file_path)
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
    dry_run: bool = False,
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

    if not author_name or not author_name.strip():
        await ctx.warning("Batch processing rejected: author_name is empty.")
        return "Error: author_name cannot be empty."

    if not changes:
        await ctx.warning("Batch processing rejected: No actions or edits provided.")
        return "Error: No changes provided."

    # Defensive normalization for double-serialized change elements (JSON
    # strings instead of parsed models). See _coerce_changes for rationale.
    changes = _coerce_changes(changes)

    def _run_batch_sync() -> tuple[bool, Any, str]:
        stream = read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)

        try:
            stats = engine.process_batch(changes, dry_run=dry_run)
        except BatchValidationError as e:
            return False, e.errors, ""

        if dry_run:
            return True, stats, ""

        final_output = output_path
        if not final_output:
            p = Path(original_docx_path)
            if p.stem.endswith("_processed") or p.stem.endswith("_redlined"):
                final_output = str(p)
            else:
                final_output = str(p.parent / f"{p.stem}_processed{p.suffix}")

        result_stream = engine.save_to_stream()
        save_stream(result_stream, final_output)
        return True, stats, final_output

    try:
        await ctx.debug("Offloading RedlineEngine to background thread")
        success, result_data, final_output_path = await asyncio.to_thread(_run_batch_sync)

        if not success:
            await ctx.error("Batch validation failed", extra={"error_count": len(result_data)})
            return "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(result_data)

        await ctx.info("Batch process complete and saved", extra={"output_path": final_output_path})

        stats = result_data
        if dry_run:
            res = "Dry-run simulation complete.\n"
        else:
            res = f"Batch complete. Saved to: {final_output_path}\n"

        res += (
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.\n"
            f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped.\n"
        )

        if stats.get("edits"):
            res += "\nDetailed Edit Reports:\n"
            for i, report in enumerate(stats["edits"]):
                status_indicator = "✅ [applied]" if report["status"] == "applied" else "❌ [failed]"
                res += f"Edit {i + 1} {status_indicator}:\n"
                res += f"  Target: '{report['target_text']}'\n"
                res += f"  New text: '{report['new_text']}'\n"
                if report.get("warning"):
                    res += f"  Warning: {report['warning']}\n"
                if report.get("error"):
                    res += f"  Error: {report['error']}\n"
                if report.get("critic_markup"):
                    res += f"  Preview (CriticMarkup): {report['critic_markup']}\n"
                if report.get("clean_text"):
                    res += f"  Clean text preview: {report['clean_text']}\n"

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
    tags={"docx"},
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
    from adeu.diff import trim_common_context

    output = [
        f"--- {Path(original_path).name}",
        f"+++ {Path(modified_path).name}",
        "",
    ]
    CONTEXT_SIZE = 40

    for edit in edits:
        raw_start = getattr(edit, "_match_start_index", 0) or 0
        raw_target = edit.target_text or ""
        raw_new = edit.new_text or ""

        # Compute the SEMANTIC change region by stripping common context that
        # `generate_edits_from_text` baked into target_text/new_text (anchor for
        # synthetic insertions, common prefix/suffix from coalesced edits).
        prefix_len, suffix_len = trim_common_context(raw_target, raw_new)

        target_end_in_target = len(raw_target) - suffix_len
        new_end_in_new = len(raw_new) - suffix_len

        display_target = raw_target[prefix_len:target_end_in_target]
        display_new = raw_new[prefix_len:new_end_in_new]

        # Shift the anchor point in the original text by the stripped prefix.
        change_start = raw_start + prefix_len
        change_end = change_start + len(display_target)

        # Compute context windows around the SEMANTIC change region.
        pre_start = max(0, change_start - CONTEXT_SIZE)
        pre_context = text_orig[pre_start:change_start]
        if pre_start > 0:
            pre_context = "..." + pre_context

        post_end = min(len(text_orig), change_end + CONTEXT_SIZE)
        post_context = text_orig[change_end:post_end]
        if post_end < len(text_orig):
            post_context = post_context + "..."

        pre_context = pre_context.replace("\n", " ").replace("\r", "")
        post_context = post_context.replace("\n", " ").replace("\r", "")

        output.append("@@ Word Patch @@")
        output.append(f" {pre_context}")
        if display_target:
            output.append(f"- {display_target}")
        if display_new:
            output.append(f"+ {display_new}")
        output.append(f" {post_context}")
        output.append("")

    return "\n".join(output)


@tool(
    description=(
        "Accepts all tracked changes and removes all comments in a single operation, "
        "producing a finalized clean document. "
        "Use this when a document review is entirely complete and you want to clear all redlines. "
        "For selective acceptance/rejection of specific changes, use `process_document_batch` instead."
    ),
    tags={"docx"},
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
        engine.accept_all_revisions(remove_comments=True)

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
    tags={"docx"},
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
    "Reads a DOCX file. Returns text with inline CriticMarkup for "
    "Tracked Changes and Comments: {++inserted++}, {--deleted--}, "
    "{==highlighted==}{>>comment<<}. Set clean_view=True for the "
    "finalized 'Accepted' text without markup.\n\n"
)

READ_DOCX_WIN32_EXTRA = (
    "If the file is open in Word, reads from the live canvas automatically. "
    "Leave file_path empty to read whatever document is currently active.\n\n"
)

READ_DOCX_TAIL = (
    "Modes:\n"
    "- 'full' (default): paginated body content. Use page=N to navigate.\n"
    "- 'outline': heading map only — start here for large docs to plan targeted reads. "
    "Defaults to L1-L2 headings; pass outline_max_level=3-6 to see deeper structure.\n"
    "- 'appendix': defined terms, anchors, and cross-reference targets. "
    "Consult before editing legal/technical docs to avoid breaking references."
)

PROCESS_BATCH_COMMON_DESC = (
    "Applies a batch of edits and review actions to a DOCX.\n\n"
    "All changes evaluate against the ORIGINAL document state — do not chain "
    "dependent edits within one batch (e.g. rename X to Y, then modify Y). "
    "Apply the rename first, then send a second batch.\n\n"
)
PROCESS_BATCH_WIN32_EXTRA = (
    "If the file is open in Word, edits run live on the canvas. "
    "Leave original_docx_path empty to edit whatever document is currently active.\n\n"
)
PROCESS_BATCH_OPERATIONS_DESC = (
    "Each item in `changes` must specify a `type`:\n"
    "1. 'modify': Search-and-replace. `target_text` must uniquely match — include "
    "surrounding context if the phrase is ambiguous. `new_text` supports Markdown: "
    "'# Heading 1' through '###### Heading 6', '**bold**', '_italic_', and '\\n\\n' "
    "to split into multiple paragraphs. Empty `new_text` deletes. Do NOT write "
    "CriticMarkup tags ({++, {--, {>>) manually — use the `comment` parameter for comments.\n"
    "2. 'accept' / 'reject': Finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n"
    "3. 'reply': Reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n"
    "4. 'insert_row' / 'delete_row': Table edits. Disk mode only — not supported on Live Word canvas.\n\n"
    "ID VOLATILITY: 'Chg:N' and 'Com:N' shift between document states. "
    "Always call `read_docx` immediately before any accept/reject/reply — "
    "do not reuse IDs from earlier in the conversation.\n\n"
    "`author_name` is used for attribution on all tracked changes and comments, "
    "in both disk and Live Word modes."
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
        tags={"docx"},
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
            Literal["full", "outline", "appendix"],
            "'full' returns body content (paginated). 'outline' returns a structural "
            "heading map. 'appendix' returns defined terms, anchors, and diagnostics — "
            "consult before editing. The page parameter applies to 'full' and 'appendix'.",
        ] = "full",
        page: Annotated[
            int,
            "Page number (1-indexed) for mode='full'. Defaults to 1. Ignored when mode='outline'.",
        ] = 1,
        outline_max_level: Annotated[
            int,
            "For mode='outline' only: only show headings at this level or shallower (1-6). "
            "Default 2 keeps output usable on large documents. Raise to 3-6 to see deeper "
            "headings. Ignored when mode='full'.",
        ] = 2,
        outline_verbose: Annotated[
            bool,
            "For mode='outline' only: when True, includes per-heading style name, table "
            "presence, and footnote IDs. Off by default to minimize payload size. "
            "Ignored when mode='full'.",
        ] = False,
    ) -> ToolResult:
        start_time = time.perf_counter()
        if not file_path:
            # Read active document directly. No disk fallback available if this fails.
            res = await read_active_word_document(
                ctx,
                clean_view,
                None,
                mode=mode,
                page=page,
                outline_max_level=outline_max_level,
                outline_verbose=outline_verbose,
            )
        else:
            # Try Live Word first. Fallback to Disk if Word is closed or document isn't open.
            try:
                res = await read_active_word_document(
                    ctx,
                    clean_view,
                    file_path,
                    mode=mode,
                    page=page,
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                )
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
                res = await _read_docx_disk(
                    file_path,
                    ctx,
                    clean_view,
                    mode,
                    page,
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_WIN32_EXTRA + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
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
        dry_run: Annotated[
            bool,
            "If True, simulates the changes and returns a detailed preview report without modifying any files.",
        ] = False,
    ) -> str:
        start_time = time.perf_counter()
        if dry_run:
            if not original_docx_path:
                return (
                    "Dry-run simulation is only supported for disk-based files (original_docx_path must be specified)."
                )
            res = await _process_document_batch_disk(
                original_docx_path, author_name, ctx, changes, output_path, dry_run=True
            )
        elif not original_docx_path:
            # Edit active document directly. No disk fallback available.
            res = await process_active_word_batch(ctx, changes, author_name, None)
        else:
            # Try Live Word first. Fallback to Disk if Word is closed or document isn't open.
            try:
                res = await process_active_word_batch(ctx, changes, author_name, original_docx_path)
            except Exception:
                await ctx.debug("Document not open in live Word, falling back to disk edit.")
                res = await _process_document_batch_disk(
                    original_docx_path, author_name, ctx, changes, output_path, dry_run=False
                )
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
                if not diff_lines:
                    res = "RESULT: Documents are content-identical."
                else:
                    res = "\n".join(diff_lines)
                    diff_count = len([line for line in diff_lines if line.startswith("+") or line.startswith("-")]) - 2
                    res += f"\n\nRESULT: Found {diff_count} structural XML differences."

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
        tags={"docx"},
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
        outline_max_level: Annotated[
            int,
            "For mode='outline' only: only show headings at this level or shallower (1-6). "
            "Default 2 keeps output usable on large documents. Raise to 3-6 to see deeper "
            "headings. Ignored when mode='full'.",
        ] = 2,
        outline_verbose: Annotated[
            bool,
            "For mode='outline' only: when True, includes per-heading style name, table "
            "presence, and footnote IDs. Off by default to minimize payload size. "
            "Ignored when mode='full'.",
        ] = False,
    ) -> ToolResult:
        start_time = time.perf_counter()
        res = await _read_docx_disk(
            file_path,
            ctx,
            clean_view,
            mode,
            page,
            outline_max_level=outline_max_level,
            outline_verbose=outline_verbose,
        )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
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
        dry_run: Annotated[
            bool,
            "If True, simulates the changes and returns a detailed preview report without modifying any files.",
        ] = False,
    ) -> str:
        start_time = time.perf_counter()
        res = await _process_document_batch_disk(
            original_docx_path, author_name, ctx, changes, output_path, dry_run=dry_run
        )
        return add_timing_if_debug(start_time, res)
