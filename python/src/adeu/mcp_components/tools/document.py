import asyncio
import os
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, Union

from docx import Document as load_document
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult
from pydantic import Field, TypeAdapter

from adeu.diff import generate_edits_from_text
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.mcp_components._response_builders import (
    BuilderError,
    BuilderResult,
    build_appendix_response,
    build_full_document_response,
    build_outline_response,
    build_paginated_response,
    build_search_response,
)
from adeu.mcp_components.shared import (
    MARKDOWN_UI_URI,
    add_timing_if_debug,
    read_file_bytes,
    save_stream,
)
from adeu.models import (
    AcceptChange,
    BatchChanges,
    DeleteTableRow,
    DocumentChange,
    InsertTableRow,
    ModifyText,
    RejectChange,
    ReplyComment,
    coerce_stringified_changes,
    const_to_enum,
)
from adeu.redline.engine import BatchValidationError, RedlineEngine, describe_illegal_control_chars
from adeu.utils.docx import strip_bom_from_docx_bytes


def _as_tool_result(res: BuilderResult) -> ToolResult:
    """Lifts a framework-free BuilderResult into fastmcp's ToolResult."""
    return ToolResult(content=res.content, structured_content=res.structured_content)


_DOCUMENT_CHANGE_LIST_ADAPTER = TypeAdapter(List[DocumentChange])

_SINGLE_CHANGE_ADAPTER: TypeAdapter[DocumentChange] = TypeAdapter(DocumentChange)


def _normalize_changes(changes: Any) -> tuple[List[DocumentChange], List[str]]:
    """
    Normalize the `changes` argument into a list of validated DocumentChange
    instances, validating each element INDEPENDENTLY so that one malformed
    sub-edit cannot forfeit the whole batch.

    Returns (valid_changes, rejected_notes):
      - valid_changes: every element that validated, in original order.
      - rejected_notes: human-readable "changes[i]: <reason>" strings for every
        element that failed, for surfacing back to the model.

    Tolerates the same three input shapes as before:
      1. List of already-validated DocumentChange instances (fast path; skips
         re-validation to preserve engine PrivateAttrs set during a dry-run).
      2. List of plain dicts.
      3. List of JSON-encoded strings (Gemini quirk).

    Mixed lists are handled. Strings are coerced to dicts first (and missing
    `type` / malformed `match_mode` are repaired) via coerce_stringified_changes.
    """
    if not isinstance(changes, list):
        # A non-list input can't be salvaged per-element. Let the list adapter
        # produce its canonical "expected a list" error and report it as a
        # whole-batch rejection.
        try:
            validated = _DOCUMENT_CHANGE_LIST_ADAPTER.validate_python(changes)
            return validated, []
        except Exception as e:
            return [], [f"changes: {_summarize_validation_error(e)}"]

    # If every element is already a DocumentChange instance, skip revalidation.
    if changes and all(
        isinstance(
            c,
            (
                AcceptChange,
                RejectChange,
                ReplyComment,
                ModifyText,
                InsertTableRow,
                DeleteTableRow,
            ),
        )
        for c in changes
    ):
        return changes, []  # type: ignore[return-value]

    coerced = coerce_stringified_changes(changes)

    valid: List[DocumentChange] = []
    rejected: List[str] = []
    for i, item in enumerate(coerced):
        try:
            valid.append(_SINGLE_CHANGE_ADAPTER.validate_python(item))
        except Exception as e:
            rejected.append(f"changes[{i}]: {_summarize_validation_error(e)}")

    return valid, rejected


def _summarize_validation_error(exc: Exception) -> str:
    """
    Condense a Pydantic ValidationError into a short, model-actionable line.
    Falls back to str(exc) for non-Pydantic errors.
    """
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return str(exc)
    parts: List[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) if parts else str(exc)


async def _read_docx_disk(
    file_path: str,
    ctx: Context,
    clean_view: bool,
    mode: str = "full",
    page: Optional[Union[int, str]] = None,
    outline_max_level: int = 2,
    outline_verbose: bool = False,
    search_query: Optional[str] = None,
    search_regex: bool = False,
    search_case_sensitive: bool = True,
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
        # paragraph.
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

        if search_query is not None:
            # `page` is a doc-page filter (None == search all pages).
            return _as_tool_result(
                build_search_response(text, search_query, search_regex, search_case_sensitive, page, file_path)
            )

        # In full mode, page='all' returns the entire document without page
        # chrome — the round-trip artifact for text-based apply/diff
        # (QA 2026-07-17 F1; mirrors the CLI's --page all). Dispatched before
        # the isdigit() check below, which would silently render page 1.
        if mode == "full" and page is not None and str(page).strip().lower() == "all":
            return _as_tool_result(build_full_document_response(text, file_path))

        # Non-search modes: `page` means document page; default to 1.
        page_num = int(page) if (page is not None and str(page).isdigit()) else 1
        if mode == "outline":
            return _as_tool_result(
                build_outline_response(
                    doc,
                    text,
                    file_path,
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                    paragraph_offsets=paragraph_offsets,
                )
            )
        if mode == "appendix":
            return _as_tool_result(build_appendix_response(text, page_num, file_path))
        # mode == "full"
        return _as_tool_result(build_paginated_response(text, page_num, file_path))

    except BuilderError as e:
        # Builder validation failures are user-facing tool errors.
        raise ToolError(str(e)) from None
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
    rejected_notes: Optional[List[str]] = None,
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

    author_ctrl = describe_illegal_control_chars(author_name)
    if author_ctrl:
        await ctx.warning("Batch processing rejected: author_name contains control characters.")
        return (
            f"Error: author_name contains control character(s) ({author_ctrl}) that cannot be "
            "stored in a DOCX. Remove them and retry."
        )

    if not changes:
        await ctx.warning("Batch processing rejected: No actions or edits provided.")
        if rejected_notes:
            return "Error: No valid changes to apply. All submitted changes failed validation:\n" + "\n".join(
                f"- {n}" for n in rejected_notes
            )
        return "Error: No changes provided."

    rejection_prefix = ""
    if rejected_notes:
        rejection_prefix = (
            "Note: some submitted changes were skipped because they failed validation. "
            "The valid changes below were still applied. Resubmit the skipped ones corrected:\n"
            + "\n".join(f"- {n}" for n in rejected_notes)
            + "\n\n"
        )

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
            res = rejection_prefix + "Dry-run simulation complete.\n"
        else:
            res = rejection_prefix + f"Batch complete. Saved to: {final_output_path}\n"

        total_occurrences = sum(
            e.get("occurrences_modified", 1) for e in stats.get("edits", []) if e.get("status") == "applied"
        )
        occ_text = f" ({total_occurrences} occurrences)" if total_occurrences > stats["edits_applied"] else ""
        already = stats.get("actions_already_resolved", 0)
        already_text = f", {already} already resolved (no effect)" if already else ""
        res += (
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped{already_text}.\n"
            f"Edits: {stats['edits_applied']} applied{occ_text}, {stats['edits_skipped']} skipped.\n"
        )

        if stats.get("edits"):
            res += "\nDetailed Edit Reports:\n"
            for i, report in enumerate(stats["edits"]):
                status_indicator = "✅ [applied]" if report["status"] == "applied" else "❌ [failed]"
                pages_str = ", ".join(f"p{p}" for p in report.get("pages", []))
                page_suffix = f" ({pages_str})" if pages_str else ""
                res += f"### Edit {i + 1} {status_indicator}{page_suffix}\n"
                if report.get("heading_path"):
                    res += f"**Path:** `{report['heading_path']}`\n"

                occ = report.get("occurrences_modified", 0)
                occ_text = f"{occ} occurrence{'s' if occ != 1 else ''} modified"
                res += f"**Mode:** `{report.get('match_mode', 'strict')}` ({occ_text})\n"

                if report.get("warning"):
                    res += f"*Warning:* {report['warning']}\n"
                if report.get("error"):
                    res += f"*Error:* {report['error']}\n"
                if report.get("critic_markup"):
                    res += f"*Preview (CriticMarkup):*\n> {report['critic_markup']}\n"
                if report.get("clean_text"):
                    res += f"*Preview (Clean):*\n> {report['clean_text']}\n"
                res += "\n"

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
    reasoning: Annotated[
        str,
        "Why do I need to diff these two documents? State this reason before any other parameter.",
    ],
    original_path: Annotated[str, "Path to the base document."],
    modified_path: Annotated[str, "Path to the new document."],
    ctx: Context,
    compare_clean: Annotated[bool, "If True, compares 'Accepted' state. If False, compares raw text."] = True,
) -> str:
    start_time = time.perf_counter()
    del reasoning
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
        # include_appendix=False: the generated appendix ("used N times",
        # diagnostics) is not document content — diffing it produces phantom
        # changes no apply can consume (QA 2026-07-18 H1).
        text_orig = extract_text_from_stream(
            stream_orig, filename=Path(original_path).name, clean_view=compare_clean, include_appendix=False
        )

        await ctx.debug("Extracting text from modified document")
        stream_mod = read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(
            stream_mod, filename=Path(modified_path).name, clean_view=compare_clean, include_appendix=False
        )

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
    reasoning: Annotated[
        str,
        "Why do I need to accept all changes in this document? State this reason before any other parameter.",
    ],
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    start_time = time.perf_counter()
    del reasoning  # reason-first UX; not used by the tool.
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
    reasoning: Annotated[
        str,
        "Why do I need to open this file in its native app? State this reason before any other parameter.",
    ],
    file_path: Annotated[str, "Absolute path to the file to open."],
    ctx: Context,
) -> str:
    start_time = time.perf_counter()
    del reasoning  # reason-first UX; not used by the tool.
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
    "Batches apply SEQUENTIALLY: each change is validated and applied against "
    "the document state produced by the changes before it, so you may chain "
    "dependent edits within one batch (e.g. rename X to Y, then modify Y — "
    "the second edit must target Y, the text as it reads after the rename). "
    "Validation failures reject the whole batch transactionally: nothing is "
    "applied until every change resolves.\n\n"
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
        LiveWordUnavailableError,
        is_document_open_in_word,
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
        reasoning: Annotated[
            str,
            "Why do I need to read this docx document? State this reason before any other parameter.",
        ],
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
            Optional[Union[int, Literal["all"]]],
            Field(
                description=(
                    "Without `search_query`: 1-indexed document page to display (defaults to 1) "
                    "for mode='full' and mode='appendix'; pass `page='all'` with mode='full' to "
                    "get the ENTIRE document in one response without page banners. With "
                    "`search_query`: restricts matches to that document page (defaults to "
                    "searching all pages; pass `page='all'` to be explicit)."
                ),
                # Render Literal["all"] as enum, not const, for Gemini. See issue #37.
                json_schema_extra=const_to_enum,
            ),
        ] = None,
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
        search_query: Annotated[Optional[str], "The substring or regex pattern to search for."] = None,
        search_regex: Annotated[bool, "Set to true to interpret search_query as a regular expression."] = False,
        search_case_sensitive: Annotated[bool, "Set to false to perform case-insensitive matching."] = True,
    ) -> ToolResult:
        start_time = time.perf_counter()
        del reasoning
        # Outside of search mode, `page` semantically means "document page" and
        # defaults to 1. In search mode, `page` is a document-page filter and
        # `None` means "search all pages" — we leave it as None to let the
        # response builder distinguish "omitted" from "explicit 1".
        if search_query is None and page is None:
            page = 1
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
                search_query=search_query,
                search_regex=search_regex,
                search_case_sensitive=search_case_sensitive,
            )
        else:
            # An explicit file_path means the file on disk is authoritative:
            # read from disk UNLESS Word already has that exact file open (in
            # which case the canvas may hold unsaved edits the agent expects to
            # see). The probe is a cheap COM connect + open-documents scan with
            # NO document extraction, and returns False when Word isn't running
            # — so a headless environment never pays the failed-extraction cost
            # or leaks a COM connection error to the model.
            if is_document_open_in_word(file_path):
                await ctx.debug("Document is open in live Word; reading from the canvas.")
                try:
                    res = await read_active_word_document(
                        ctx,
                        clean_view,
                        file_path,
                        mode=mode,
                        page=page,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        search_query=search_query,
                        search_regex=search_regex,
                        search_case_sensitive=search_case_sensitive,
                    )
                except LiveWordUnavailableError:
                    # The probe reported the file open, but Word/COM turned out to
                    # be unusable (dead or zombie instance). Since we hold an
                    # explicit file_path, the disk copy is authoritative — fall
                    # back to it silently rather than surfacing -2147221021 to the
                    # model. Scoped to THIS error so genuine post-read failures
                    # (page out of range, etc. — raised as ToolError) still
                    # propagate. Only reachable when a file_path exists; the
                    # active-document mode above has no disk fallback by design.
                    await ctx.debug("Live Word probe matched but COM was unavailable; falling back to disk read.")
                    res = await _read_docx_disk(
                        file_path,
                        ctx,
                        clean_view,
                        mode,
                        page,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        search_query=search_query,
                        search_regex=search_regex,
                        search_case_sensitive=search_case_sensitive,
                    )
            else:
                res = await _read_docx_disk(
                    file_path,
                    ctx,
                    clean_view,
                    mode,
                    page,
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                    search_query=search_query,
                    search_regex=search_regex,
                    search_case_sensitive=search_case_sensitive,
                )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_WIN32_EXTRA + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        reasoning: Annotated[
            str,
            "Why do I need to apply these changes to the document? State this reason before any other parameter.",
        ],
        author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
        ctx: Context,
        changes: Annotated[
            BatchChanges,
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
        del reasoning  # reason-first UX; not used by the tool.
        # FastMCP's parameter validation does not always honor the BeforeValidator
        # attached to BatchChanges (it flattens the Annotated chain and validates
        # against the bare list type), so coerce here as a defensive second pass.
        # This is also what catches stringified-object lists emitted by some LLM
        # clients (notably Gemini under load).
        changes, rejected_notes = _normalize_changes(changes)
        if not changes and rejected_notes:
            return add_timing_if_debug(
                start_time,
                "Error: No valid changes to apply. All submitted changes failed validation:\n"
                + "\n".join(f"- {n}" for n in rejected_notes),
            )
        if dry_run:
            if not original_docx_path:
                return (
                    "Dry-run simulation is only supported for disk-based files (original_docx_path must be specified)."
                )
            res = await _process_document_batch_disk(
                original_docx_path,
                author_name,
                ctx,
                changes,
                output_path,
                dry_run=True,
                rejected_notes=rejected_notes,
            )
        elif not original_docx_path:
            # Edit active document directly. No disk fallback available.
            res = await process_active_word_batch(ctx, changes, author_name, None)
        elif is_document_open_in_word(original_docx_path):
            # The file is open in Word: apply edits to the live canvas so the
            # agent's changes land where the user is looking. If the probe matched
            # but COM is actually unusable, fall back to editing the disk copy
            # (which the explicit path makes authoritative) instead of erroring.
            await ctx.debug("Document is open in live Word; editing the canvas.")
            try:
                res = await process_active_word_batch(ctx, changes, author_name, original_docx_path)
            except LiveWordUnavailableError:
                await ctx.debug("Live Word probe matched but COM was unavailable; falling back to disk edit.")
                res = await _process_document_batch_disk(
                    original_docx_path,
                    author_name,
                    ctx,
                    changes,
                    output_path,
                    dry_run=False,
                    rejected_notes=rejected_notes,
                )
        else:
            # Not open in Word (or Word not running): the file on disk is
            # authoritative — edit it directly. This is also the headless path.
            res = await _process_document_batch_disk(
                original_docx_path,
                author_name,
                ctx,
                changes,
                output_path,
                dry_run=False,
                rejected_notes=rejected_notes,
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
            reasoning: Annotated[
                str,
                "Why do I need this structural XML diff? State this reason before any other parameter.",
            ],
            file_a: Annotated[str, "Absolute path to the first/baseline DOCX file."],
            file_b: Annotated[str, "Absolute path to the second/modified DOCX file."],
            ctx: Context,
        ) -> str:
            start_time = time.perf_counter()
            del reasoning  # reason-first UX; not used by the tool.
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
            reasoning: Annotated[
                str,
                "Why do I need to open this document in Word? State this reason before any other parameter.",
            ],
            ctx: Context,
            file_path: Annotated[str, "Absolute path to the DOCX file to open in Word."],
            visible: Annotated[bool, "Whether to make the Word application window visible."] = True,
        ) -> str:
            start_time = time.perf_counter()
            del reasoning  # reason-first UX; not used by the tool.
            res = await open_word_document_impl(ctx, file_path, visible)
            return add_timing_if_debug(start_time, res)

        @tool(
            description="Saves the currently active Microsoft Word document to disk. Optionally closes it after saving."
        )
        async def save_active_word_document(
            reasoning: Annotated[
                str,
                "Why do I need to save the active document? State this reason before any other parameter.",
            ],
            ctx: Context,
            output_path: Annotated[
                Optional[str],
                "Optional absolute path to 'Save As'. If omitted, overwrites the current file.",
            ] = None,
            close: Annotated[bool, "Whether to close the document in Word after saving."] = False,
        ) -> str:
            start_time = time.perf_counter()
            del reasoning  # reason-first UX; not used by the tool.
            res = await save_active_word_document_impl(ctx, output_path, close)
            return add_timing_if_debug(start_time, res)

else:
    from adeu.models import DocumentChange

    class LiveWordUnavailableError(Exception):
        pass

    @tool(
        description=READ_DOCX_COMMON_DESC + READ_DOCX_TAIL,
        tags={"docx"},
        annotations={"readOnlyHint": True},
        meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
    )
    async def read_docx(
        reasoning: Annotated[
            str,
            "Why do I need to read this docx document? State this reason before any other parameter.",
        ],
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
            Optional[Union[int, Literal["all"]]],
            Field(
                description=(
                    "Without `search_query`: 1-indexed document page to display (defaults to 1) "
                    "for mode='full'; pass `page='all'` to get the ENTIRE document in one "
                    "response without page banners. With `search_query`: restricts matches to "
                    "that document page (defaults to searching all pages; pass `page='all'` to "
                    "be explicit)."
                ),
                # Render Literal["all"] as enum, not const, for Gemini. See issue #37.
                json_schema_extra=const_to_enum,
            ),
        ] = None,
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
        search_query: Annotated[Optional[str], "The substring or regex pattern to search for."] = None,
        search_regex: Annotated[bool, "Set to true to interpret search_query as a regular expression."] = False,
        search_case_sensitive: Annotated[bool, "Set to false to perform case-insensitive matching."] = True,
    ) -> ToolResult:
        start_time = time.perf_counter()
        del reasoning  # reason-first UX; not used by the tool.
        if search_query is None and page is None:
            page = 1
        res = await _read_docx_disk(
            file_path,
            ctx,
            clean_view,
            mode,
            page,
            outline_max_level=outline_max_level,
            outline_verbose=outline_verbose,
            search_query=search_query,
            search_regex=search_regex,
            search_case_sensitive=search_case_sensitive,
        )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        reasoning: Annotated[
            str,
            "Why do I need to apply these changes to the document? State this reason before any other parameter.",
        ],
        original_docx_path: Annotated[str, "Absolute path to the source file."],
        author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
        ctx: Context,
        changes: Annotated[
            BatchChanges,
            "List of changes to apply. Each change must specify 'type'.",
        ],
        output_path: Annotated[Optional[str], "Optional output path."] = None,
        dry_run: Annotated[
            bool,
            "If True, simulates the changes and returns a detailed preview report without modifying any files.",
        ] = False,
    ) -> str:
        start_time = time.perf_counter()
        del reasoning
        # See win32 branch above for why we re-coerce here.
        changes, rejected_notes = _normalize_changes(changes)
        if not changes and rejected_notes:
            return add_timing_if_debug(
                start_time,
                "Error: No valid changes to apply. All submitted changes failed validation:\n"
                + "\n".join(f"- {n}" for n in rejected_notes),
            )
        res = await _process_document_batch_disk(
            original_docx_path,
            author_name,
            ctx,
            changes,
            output_path,
            dry_run=dry_run,
            rejected_notes=rejected_notes,
        )
        return add_timing_if_debug(start_time, res)
