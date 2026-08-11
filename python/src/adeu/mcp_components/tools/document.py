import asyncio
import json
import os
import re
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, Union

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult, tool
from pydantic import BeforeValidator, Field, TypeAdapter, WithJsonSchema

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.mcp_components._response_builders import (
    BuilderError,
    BuilderResult,
    build_appendix_response,
    build_changes_response,
    build_full_document_response,
    build_outline_response,
    build_page_range_response,
    build_paginated_response,
    build_search_response,
)
from adeu.mcp_components.shared import (
    MARKDOWN_UI_URI,
    MCP_ID_DISCOVERY_HINT,
    add_timing_if_debug,
    read_file_bytes,
    save_stream,
)
from adeu.models import (
    AcceptChange,
    DeleteTableRow,
    DocumentChange,
    FlatSchemaDocumentChange,
    InsertTableRow,
    ModifyText,
    RejectChange,
    ReplyComment,
    coerce_stringified_changes,
)
from adeu.pagination import parse_page_arg
from adeu.payloads import failure_envelope
from adeu.redline.engine import BatchValidationError, RedlineEngine, describe_illegal_control_chars
from adeu.utils.text import batch_details_header


def _as_tool_result(res: BuilderResult) -> ToolResult:
    """Lifts a framework-free BuilderResult into fastmcp's ToolResult."""
    return ToolResult(content=res.content, structured_content=res.structured_content)


def _overwrite_note(target_path: str, input_path: str) -> str:
    """
    Overwrite disclosure for tool saves (QA 2026-07-23 F17). Must be called
    BEFORE the write: when the target already exists, the response says so —
    "overwritten in place" when the save replaces the input document itself
    (default-output stems ending in _processed, or an explicit output_path
    equal to the input), "replaced existing file" for any other pre-existing
    target (e.g. the previous run's default-named output).
    """
    target = Path(target_path)
    if not target.exists():
        return ""
    try:
        same = os.path.samefile(str(target), input_path)
    except OSError:
        same = str(target.resolve()) == str(Path(input_path).resolve())
    if same:
        return f"\nNote: the source document at {target_path} was overwritten in place."
    return f"\nNote: replaced existing file {target_path}."


# read_docx must DECLARE this schema, not just populate structuredContent.
# The MCP Apps host only forwards `structuredContent` to the UI app when the
# tool advertises an outputSchema; without one it hands the app a result
# carrying `content` alone and the markdown viewer has nothing to render
# (observed in Claude Desktop 2026-07-27: `params=content,isError`).
# Keep `required` minimal and additionalProperties open — clients validate the
# payload against this schema and reject the whole call on a mismatch, so an
# edge path that omits `title` must not fail the read.
READ_DOCX_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "markdown": {
            "type": "string",
            "description": "Document content as Markdown, for display.",
        },
        "title": {
            "type": "string",
            "description": "Display title (the file name, or 'Search: <file name>').",
        },
        "file_path": {
            "type": "string",
            "description": "Absolute path of the document that was read.",
        },
    },
    "required": ["markdown"],
    "additionalProperties": True,
}

_DOCUMENT_CHANGE_LIST_ADAPTER = TypeAdapter(List[DocumentChange])

_SINGLE_CHANGE_ADAPTER: TypeAdapter[DocumentChange] = TypeAdapter(DocumentChange)

# A REQUIRED list. Per-item stringification (the Gemini double-serialize
# quirk) is repaired by coerce_stringified_changes; a WHOLLY stringified
# payload is not accepted, because the Node engine's zod schema cannot accept
# one without dropping `changes` out of its `required` list — and one engine
# silently repairing what the other rejects is the divergence that makes an
# agent's working call break when the backend changes. Both reject it with a
# clear type error the caller can retry from.
McpBatchChanges = Annotated[
    List[Any],
    BeforeValidator(coerce_stringified_changes),
    WithJsonSchema(TypeAdapter(List[FlatSchemaDocumentChange]).json_schema()),
]


class RejectedNotes(List[str]):
    def __init__(
        self,
        notes: List[str],
        pairs: List[tuple[int, str]],
        valid_indices: Optional[List[int]] = None,
    ):
        super().__init__(notes)
        self.pairs = pairs
        self.valid_indices = valid_indices if valid_indices is not None else []


def _normalize_changes(changes: Any) -> tuple[List[DocumentChange], RejectedNotes]:
    """
    Normalize the `changes` argument into a list of validated DocumentChange
    instances, validating each element INDEPENDENTLY so that one malformed
    sub-edit cannot forfeit the whole batch.

    Returns (valid_changes, rejected_notes):
      - valid_changes: every element that validated, in original order.
      - rejected_notes: human-readable "changes[i]: <reason>" strings for every
        element that failed, with `.pairs` containing (index, reason) tuples,
        and `.valid_indices` containing original 0-based indices of valid_changes.

    Tolerates the same three input shapes as before:
      1. List of already-validated DocumentChange instances (fast path; skips
         re-validation to preserve engine PrivateAttrs set by a prior run).
      2. List of plain dicts.
      3. List of JSON-encoded strings (Gemini quirk).

    Mixed lists are handled. Strings are coerced to dicts first (and missing
    `type` / malformed `match_mode` are repaired) via coerce_stringified_changes.

    A WHOLLY stringified payload is deliberately not repaired here — see
    McpBatchChanges for why both engines reject that shape rather than one
    silently accepting it.
    """
    if not isinstance(changes, list):
        # A non-list input can't be salvaged per-element. Let the list adapter
        # produce its canonical "expected a list" error and report it as a
        # whole-batch rejection.
        try:
            validated = _DOCUMENT_CHANGE_LIST_ADAPTER.validate_python(changes)
            return validated, RejectedNotes([], [], valid_indices=list(range(len(validated))))
        except Exception as e:
            msg = _summarize_validation_error(e)
            return [], RejectedNotes([f"changes: {msg}"], [(0, msg)], valid_indices=[])

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
        return changes, RejectedNotes([], [], valid_indices=list(range(len(changes))))  # type: ignore[return-value]

    coerced = coerce_stringified_changes(changes)

    valid: List[DocumentChange] = []
    valid_indices: List[int] = []
    rejected_notes: List[str] = []
    rejected_pairs: List[tuple[int, str]] = []
    for i, item in enumerate(coerced):
        try:
            valid.append(_SINGLE_CHANGE_ADAPTER.validate_python(item))
            valid_indices.append(i)
        except Exception as e:
            reason = _summarize_validation_error(e)
            rejected_notes.append(f"changes[{i}]: {reason}")
            rejected_pairs.append((i, reason))

    return valid, RejectedNotes(rejected_notes, rejected_pairs, valid_indices=valid_indices)


def _summarize_validation_error(exc: Exception) -> str:
    """
    Condense a Pydantic ValidationError into a short, model-actionable line.
    Falls back to str(exc) for non-Pydantic errors.
    """
    from pydantic import ValidationError

    from adeu.payloads import FUSED_JSON_HINT, has_fused_json_marker

    if not isinstance(exc, ValidationError):
        return str(exc)
    parts: List[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        if err.get("type") == "union_tag_invalid":
            tag = err.get("ctx", {}).get("tag", "")
            if has_fused_json_marker(str(tag)):
                msg = f"{msg}. {FUSED_JSON_HINT}" if not msg.endswith(".") else f"{msg} {FUSED_JSON_HINT}"
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) if parts else str(exc)


class _ProgressRelay:
    """
    Bridges the projection cache's synchronous progress callbacks (invoked in
    a worker thread) onto MCP progress notifications. Started only when the
    client supplied a progress token AND the document is cold — a warm read
    finishes in milliseconds. The Node cache shipped the same lesson: report
    parse progress during cold ingests and keep the event loop free so the
    notifications actually flush.
    """

    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._pct: int = 0
        self._msg: str = ""
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _has_progress_token(ctx: Context) -> bool:
        try:
            rc = ctx.request_context
            if not rc or not rc.meta:
                return False
            if isinstance(rc.meta, dict):
                return rc.meta.get("progressToken") is not None or rc.meta.get("progress_token") is not None
            return (
                getattr(rc.meta, "progressToken", None) is not None
                or getattr(rc.meta, "progress_token", None) is not None
            )
        except Exception:
            return False

    def callback(self, pct: int, msg: str) -> None:
        # Called from the worker thread — mutate only; the poller flushes.
        self._pct = pct
        self._msg = msg

    async def _poll(self) -> None:
        last = -1
        while not self._stop.is_set():
            pct = self._pct
            if pct != last:
                try:
                    await self._ctx.report_progress(pct, 100, self._msg or None)
                except Exception:
                    return
                last = pct
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.6)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._has_progress_token(self._ctx):
            self._task = asyncio.create_task(self._poll())

    async def finish(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await self._task
            await self._ctx.report_progress(100, 100, None)
        except Exception:
            pass


def _schedule_background_fill(entry, clean_view: bool) -> None:
    """
    Warms one view of a cache entry in the background ONCE the server has
    been quiet for a few hundred ms (an immediate multi-second fill would
    contend with the request that typically follows — Node cache lesson).
    Used for the clean view after a cold raw read, and for the OUTPUT file's
    raw view after a batch save (read-after-edit priming). A client that
    explicitly requests the view never waits on this — its own call computes
    directly, and the per-entry lock deduplicates the work. Gives up if the
    server never goes quiet; never lets an exception escape into the loop.
    """
    from adeu.mcp_components.doc_cache import doc_cache

    view = entry.view(clean_view)
    if view.base_text is not None:
        return
    if clean_view:
        if entry.clean_fill_scheduled:
            return
        entry.clean_fill_scheduled = True

    async def _fill() -> None:
        try:
            for _ in range(16):
                await asyncio.sleep(0.5)
                if doc_cache.quiet_for(0.45):
                    break
            else:
                return
            if view.base_text is None:
                await asyncio.to_thread(doc_cache.get_base_text, entry, clean_view, None)
        except Exception:
            pass

    try:
        asyncio.create_task(_fill())
    except RuntimeError:
        # No running loop (sync test harness) — skip the warm-up.
        if clean_view:
            entry.clean_fill_scheduled = False


async def _read_docx_disk(
    file_path: str,
    ctx: Context,
    clean_view: bool,
    mode: str = "full",
    page: Optional[Union[int, str]] = None,
    force: bool = False,
    outline_max_level: int = 2,
    outline_verbose: bool = False,
    search_query: Optional[str] = None,
    search_regex: bool = False,
    search_case_sensitive: bool = True,
    changes_author: Optional[str] = None,
    changes_offset: int = 0,
    max_matches: int = 20,
    match_offset: int = 0,
    full_paragraph: bool = False,
) -> ToolResult:
    """
    Core logic for reading a DOCX from disk. Dispatches on `mode`.

    All parse/projection work runs in a worker thread against the stat-keyed
    projection cache (adeu.mcp_components.doc_cache): the cost of parsing a
    document version is paid once, page turns/search/outline on a warm
    version are string work over cached projections, and the event loop
    stays responsive during cold ingests (heartbeats/progress flush instead
    of the client timing out at the transport level).
    """
    from adeu.mcp_components.doc_cache import doc_cache

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
        if not Path(file_path).exists():
            # Called only to raise: doc_cache reads its own bytes, so this
            # keeps missing-file reporting in one place — shared's lean
            # FileNotFoundError with close-match sibling suggestions and the
            # relative-path hint, echoing the path exactly as the caller gave
            # it (QA round 3, findings 3.11 and F16). The returned buffer is
            # deliberately discarded.
            read_file_bytes(file_path)

        doc_cache.mark_activity()
        key = doc_cache.stat_key(file_path)
        entry = doc_cache.entry(key)
        was_cold = doc_cache.is_cold(entry, clean_view)
        if was_cold:
            await ctx.debug("Projection cache cold for this document version; ingesting")

        relay = _ProgressRelay(ctx)
        if was_cold:
            relay.start()
        ingest_started = time.perf_counter()
        try:
            if search_query is not None:
                # `page` is a doc-page filter (None == search all pages).
                # Search only ever consumes the appendix-free body, so it is
                # served from the cached base projection regardless of mode.
                text, pagination = await asyncio.to_thread(doc_cache.get_pagination, entry, clean_view, relay.callback)
                await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
                return _as_tool_result(
                    build_search_response(
                        text,
                        search_query,
                        search_regex,
                        search_case_sensitive,
                        page,
                        file_path,
                        pagination_result=pagination,
                        max_matches=max_matches,
                        match_offset=match_offset,
                        full_paragraph=full_paragraph,
                    )
                )

            if mode == "changes":
                if clean_view:
                    raise ToolError("--clean-view cannot be used with mode='changes'.")
                text, pagination = await asyncio.to_thread(
                    doc_cache.get_pagination, entry, clean_view=False, cb=relay.callback
                )
                await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
                from adeu.cli import _load_docx_or_exit
                from adeu.redline.comments import CommentsManager

                try:
                    doc = await asyncio.to_thread(_load_docx_or_exit, Path(file_path))
                    comments_data = await asyncio.to_thread(CommentsManager(doc).extract_comments_data)
                except Exception:
                    comments_data = None

                try:

                    def _get_change_ids():
                        with open(file_path, "rb") as f:
                            eng = RedlineEngine(BytesIO(f.read()), id_discovery_hint=MCP_ID_DISCOVERY_HINT)
                            return set(eng._existing_change_ids())

                    existing_change_ids = await asyncio.to_thread(_get_change_ids)
                except Exception:
                    existing_change_ids = None

                return _as_tool_result(
                    build_changes_response(
                        text,
                        file_path,
                        comments_data=comments_data,
                        author_filter=changes_author,
                        page=page,
                        offset=changes_offset,
                        is_cli=False,
                        pagination_result=pagination,
                        existing_change_ids=existing_change_ids,
                    )
                )

            if mode == "appendix":
                text = await asyncio.to_thread(doc_cache.get_text_with_appendix, entry, clean_view, relay.callback)
                await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
                page_num = 1
                if page is not None:
                    try:
                        kind, page_val = parse_page_arg(page)
                    except ValueError as e:
                        raise ToolError(str(e)) from e
                    if kind == "range":
                        raise ToolError("Page range pagination is only supported in 'full' mode, not 'appendix' mode.")
                    if kind == "all":
                        raise ToolError(f"Invalid page parameter: '{page}'. Provide a positive integer.")
                    assert isinstance(page_val, int)
                    page_num = page_val
                return _as_tool_result(build_appendix_response(text, page_num, file_path))

            if mode == "outline":
                text, pagination, nodes = await asyncio.to_thread(
                    doc_cache.get_outline, entry, clean_view, relay.callback
                )
                await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
                return _as_tool_result(
                    build_outline_response(
                        None,
                        text,
                        file_path,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        pagination_result=pagination,
                        outline_nodes=nodes,
                    )
                )

            # mode == "full"
            text, pagination = await asyncio.to_thread(doc_cache.get_pagination, entry, clean_view, relay.callback)
            await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})

            page_num = 1
            if page is not None:
                try:
                    kind, page_val = parse_page_arg(page)
                except ValueError as e:
                    raise ToolError(str(e)) from e

                if kind == "all":
                    from adeu.payloads import response_budget_limit

                    if not force and len(text) > response_budget_limit():
                        from adeu.mcp_components._response_builders import build_budget_guard_message

                        _, _outline_pagination, nodes = await asyncio.to_thread(
                            doc_cache.get_outline, entry, clean_view, relay.callback
                        )
                        raise ToolError(
                            build_budget_guard_message(
                                text,
                                file_path,
                                outline_nodes=nodes,
                                pagination_result=pagination,
                            )
                        )
                    return _as_tool_result(build_full_document_response(text, file_path))
                if kind == "range":
                    assert isinstance(page_val, tuple)
                    start_p, end_p = page_val
                    return _as_tool_result(
                        build_page_range_response(
                            text,
                            start_p,
                            end_p,
                            file_path,
                            pagination_result=pagination,
                        )
                    )
                assert isinstance(page_val, int)
                page_num = page_val
            return _as_tool_result(build_paginated_response(text, page_num, file_path, pagination_result=pagination))
        finally:
            await relay.finish()
            # Warm the clean view in the background after a cold RAW ingest —
            # the next thing agents commonly ask for (Node cache lesson).
            # Only for documents where the ingest actually hurt (>= 2s):
            # small documents recompute in noise time, and skipping them
            # keeps short-lived event loops (tests, one-shot CLIs) free of
            # dangling background tasks.
            if was_cold and not clean_view and (time.perf_counter() - ingest_started) >= 2.0:
                _schedule_background_fill(entry, clean_view=True)

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
    rejected_notes: Optional[RejectedNotes] = None,
    partial: bool = True,
) -> str:
    """Core logic for modifying a DOCX on disk."""
    # Batches are heavy CPU: let the projection cache's background fills see
    # them as activity so they defer instead of contending.
    from adeu.mcp_components.doc_cache import doc_cache

    doc_cache.mark_activity()
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
            env = failure_envelope(
                "invalid_changes_file",
                getattr(rejected_notes, "pairs", []),
                "All submitted changes failed validation.",
                errors=rejected_notes,
            )
            json_block = f"\n\n```json\n{json.dumps(env, ensure_ascii=False)}\n```"
            return (
                "Error: No valid changes to apply. All submitted changes failed validation.\n"
                + "\n".join(f"- {n}" for n in rejected_notes)
                + json_block
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

    def _run_batch_sync() -> tuple[bool, Any, str, str]:
        stream = read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name, id_discovery_hint=MCP_ID_DISCOVERY_HINT)

        valid_indices = getattr(rejected_notes, "valid_indices", None)
        try:
            stats = engine.process_batch(changes, original_indices=valid_indices, partial=partial)
        except BatchValidationError as e:
            return False, e, "", ""

        applied_count = stats.get("edits_applied", 0) + stats.get("actions_applied", 0)
        engine_failed = list(stats.get("failed", []))
        schema_failed = getattr(rejected_notes, "pairs", []) if rejected_notes else []

        if applied_count == 0 and (engine_failed or schema_failed):
            err_list: list[str] = []
            failed_pairs: list[tuple[int, str]] = []
            for idx, note in schema_failed:
                err_list.append(f"changes[{idx}]: {note}")
                failed_pairs.append((idx, str(note)))
            for item in engine_failed:
                err_list.append(item["reason"])
                failed_pairs.append((item["index"], str(item["reason"])))

            env = failure_envelope(
                "batch_validation_failed",
                failed_pairs,
                "Batch rejected. Some edits failed validation.",
                errors=err_list,
            )
            return False, env, "", ""

        final_output = output_path
        if not final_output:
            p = Path(original_docx_path)
            if p.stem.endswith("_processed") or p.stem.endswith("_redlined"):
                final_output = str(p)
            else:
                final_output = str(p.parent / f"{p.stem}_processed{p.suffix}")

        result_stream = engine.save_to_stream()
        # Disclose overwrites BEFORE writing (QA 2026-07-23 F17): repeated
        # default-named runs, _processed-stem inputs saving in place, and
        # output_path == input all silently replaced an existing file.
        overwrite_note = _overwrite_note(final_output, original_docx_path)
        save_stream(result_stream, final_output)
        return True, stats, final_output, overwrite_note

    try:
        await ctx.debug("Offloading RedlineEngine to background thread")
        batch_started = time.perf_counter()
        success, result_data, final_output_path, overwrite_note = await asyncio.to_thread(_run_batch_sync)

        if not success:
            exc = result_data
            if isinstance(exc, BatchValidationError):
                err_list = list(exc.errors)
                failed_pairs = list(exc.failed)
            elif isinstance(exc, dict):
                json_block = f"\n\n```json\n{json.dumps(exc, ensure_ascii=False)}\n```"
                err_list = exc.get("errors", [exc.get("message", "Batch rejected")])
                return "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(err_list) + json_block
            else:
                err_list = exc if isinstance(exc, list) else [str(exc)]
                failed_pairs = []

            if rejected_notes and hasattr(rejected_notes, "pairs") and rejected_notes.pairs:
                seen_indices = {i for i, _ in failed_pairs}
                for i, reason in rejected_notes.pairs:
                    if i not in seen_indices:
                        failed_pairs.append((i, reason))
                        seen_indices.add(i)
                failed_pairs.sort(key=lambda x: x[0])
                rej_notes_list = (
                    getattr(rejected_notes, "notes", []) if hasattr(rejected_notes, "notes") else list(rejected_notes)
                )
                err_list = list(rej_notes_list) + [e for e in err_list if e not in rej_notes_list]

            await ctx.error("Batch validation failed", extra={"error_count": len(err_list)})
            env = failure_envelope(
                "batch_validation_failed",
                failed_pairs,
                "Batch rejected. Some edits failed validation.",
                errors=err_list,
            )
            json_block = f"\n\n```json\n{json.dumps(env, ensure_ascii=False)}\n```"
            return "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(err_list) + json_block

        await ctx.info("Batch process complete and saved", extra={"output_path": final_output_path})

        # Output priming: the agent's next move after an edit is almost
        # always a read of the output file. For documents where the batch
        # was expensive (a proxy for "the cold read will be too"), warm the
        # output's raw projection in the background once the server goes
        # quiet — the follow-up read then hits a warm (or in-flight, via the
        # per-entry lock) cache entry instead of paying a full cold ingest.
        if final_output_path and (time.perf_counter() - batch_started) >= 4.0:
            try:
                out_key = doc_cache.stat_key(final_output_path)
                _schedule_background_fill(doc_cache.entry(out_key), clean_view=False)
            except OSError:
                pass

        stats = result_data
        applied_count = stats.get("edits_applied", 0) + stats.get("actions_applied", 0)
        engine_failed = list(stats.get("failed", []))
        schema_failed = getattr(rejected_notes, "pairs", []) if rejected_notes else []

        is_partial_success = (
            partial
            and (bool(schema_failed) or bool(engine_failed) or stats.get("status") == "partial")
            and applied_count > 0
        )

        # Partial success is a SUCCESS response with the failures hoisted to
        # the top — never a failure envelope. A batch envelope carries
        # BATCH_RECOVERY_PROTOCOL ("Nothing was written"), which contradicts
        # the saved output path printed in the same response.
        partial_header = ""
        if is_partial_success:
            combined_fails: list[tuple[int, str]] = []
            for idx, note in schema_failed:
                combined_fails.append((idx, f"changes[{idx}]: {note}"))
            for item in engine_failed:
                combined_fails.append((item["index"], item["reason"]))
            combined_fails.sort(key=lambda x: x[0])

            max_idx = max([idx for idx, _ in combined_fails] + [0])
            total_n = max(max_idx + 1, len(changes) + len(schema_failed))

            partial_header = (
                f"PARTIAL: applied {applied_count} of {total_n} changes. {len(combined_fails)} failed validation:\n\n"
            )
            for idx, reason in combined_fails:
                partial_header += f"- Change #{idx + 1} Failed: {reason}\n"
            partial_header += "\n"

        # The partial header already lists the schema rejections, so it
        # replaces (never stacks with) the rejection preamble.
        res = (partial_header or rejection_prefix) + f"Batch complete. Saved to: {final_output_path}{overwrite_note}\n"

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

                # An edit's comment must be visible in the rendered report —
                # it is where an agent verifies the comment it wrote
                # (QA 2026-07-23 F7). The engine supplies the per-edit
                # `comment` field; .get() keeps this safe to render even
                # against engine builds that predate the field.
                if report.get("comment"):
                    res += f'**Comment:** "{report["comment"]}"\n'

                if report.get("warning"):
                    res += f"*Warning:* {report['warning']}\n"
                if report.get("error"):
                    res += f"*Error:* {report['error']}\n"
                # One preview per edit: the clean preview is the CriticMarkup
                # preview with the same markup resolved, so sending both bills
                # the agent twice for one span (B1, minimal report). The
                # CriticMarkup form is the one that carries the evidence, and
                # it is rendered in full — a shortened preview is not
                # verification, and a cut through a bubble is not even valid
                # CriticMarkup.
                if report.get("critic_markup"):
                    res += f"*Preview (CriticMarkup):*\n> {report['critic_markup']}\n"
                res += "\n"

        if stats.get("skipped_details"):
            res += "\n\n" + batch_details_header(stats["skipped_details"]) + "\n" + "\n".join(stats["skipped_details"])
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
    reasoning: Annotated[
        Optional[str],
        "Why do I need to diff these two documents? State this reason before any other parameter.",
    ] = "",
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
        # atomic_criticmarkup: diff-display only — hunks must never cut into a
        # {--/{++/{>>/{== block (QA 2026-07-23 F15). No-op on clean text.
        edits = generate_edits_from_text(text_orig, text_mod, atomic_criticmarkup=True)

        if not edits:
            await ctx.warning("No text differences found between the documents.")
            return add_timing_if_debug(start_time, "No text differences found between the documents.")

        await ctx.info(f"Diff complete. Found {len(edits)} differences.")
        res = _create_diff_output(original_path, modified_path, text_orig, edits)
        return add_timing_if_debug(start_time, res)

    except Exception as e:
        await ctx.error("Failed to compute diff", extra={"error": str(e)})
        return add_timing_if_debug(start_time, f"Error computing diff: {str(e)}")


def _clamp_display_to_criticmarkup_blocks(
    raw_target: str, raw_new: str, prefix_len: int, suffix_len: int
) -> tuple[int, int]:
    """
    Backtracks trim_common_context's cut points so neither display string is
    sliced INSIDE a CriticMarkup block: trimming the common `{--A ` prefix of
    `{--A B--}` -> `{--A C--}` would emit the orphaned-delimiter payloads
    `B--}` / `C--}` (QA 2026-07-23 F15). Cuts only ever move OUTWARD (prefix
    shrinks to the block start, suffix shrinks to just past the block end), so
    the display region stays a superset of the semantic change.
    """
    from adeu.diff import CRITICMARKUP_BLOCK_RE

    span_cache = {s: [(m.start(), m.end()) for m in CRITICMARKUP_BLOCK_RE.finditer(s)] for s in (raw_target, raw_new)}

    changed = True
    while changed:
        changed = False
        for s in (raw_target, raw_new):
            for b_start, b_end in span_cache[s]:
                if b_start < prefix_len < b_end:
                    prefix_len = b_start
                    changed = True
                suffix_cut = len(s) - suffix_len
                if b_start < suffix_cut < b_end:
                    suffix_len = max(0, len(s) - b_end)
                    changed = True
    return prefix_len, suffix_len


def _escape_newlines_inside_blocks(s: str) -> str:
    """
    Keeps each CriticMarkup block on ONE diff payload line by escaping its
    internal newlines — a multi-line {>>…<<} bubble otherwise ends a `+` line
    mid-block and leaks bare closers onto continuation lines (F15).
    """
    from adeu.diff import CRITICMARKUP_BLOCK_RE

    return CRITICMARKUP_BLOCK_RE.sub(lambda m: m.group(0).replace("\n", "\\n"), s)


_CM_DELIMITER_RE = re.compile(r"\{--|\{\+\+|\{>>|\{==|--\}|\+\+\}|<<\}|==\}")


def _balance_context_line(s: str) -> str:
    """
    Trims a display-only context window so every CriticMarkup delimiter on the
    line is paired: the fixed-width window legally slices mid-block, leaving
    an opener whose closer (or a closer whose opener) lies outside the window
    (F15). Drops the line's head through the last opener-less closer and
    truncates at the first closer-less opener.
    """
    # Head: drop through the LAST closer with no opener inside the window.
    depth = 0
    cut_start = 0
    for m in _CM_DELIMITER_RE.finditer(s):
        if m.group(0).startswith("{"):
            depth += 1
        elif depth == 0:
            cut_start = m.end()
        else:
            depth -= 1
    s = s[cut_start:]

    # Tail: truncate at the FIRST opener whose closer is beyond the window.
    stack: list[int] = []
    for m in _CM_DELIMITER_RE.finditer(s):
        if m.group(0).startswith("{"):
            stack.append(m.start())
        elif stack:
            stack.pop()
    if stack:
        s = s[: stack[0]]
    return s


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
        # Never cut a display string inside a CriticMarkup block (F15).
        prefix_len, suffix_len = _clamp_display_to_criticmarkup_blocks(raw_target, raw_new, prefix_len, suffix_len)

        target_end_in_target = len(raw_target) - suffix_len
        new_end_in_new = len(raw_new) - suffix_len

        display_target = _escape_newlines_inside_blocks(raw_target[prefix_len:target_end_in_target])
        display_new = _escape_newlines_inside_blocks(raw_new[prefix_len:new_end_in_new])

        # Shift the anchor point in the original text by the stripped prefix.
        change_start = raw_start + prefix_len
        change_end = change_start + (target_end_in_target - prefix_len)

        # Compute context windows around the SEMANTIC change region.
        pre_start = max(0, change_start - CONTEXT_SIZE)
        pre_context = text_orig[pre_start:change_start]
        pre_context = _balance_context_line(pre_context.replace("\n", " ").replace("\r", ""))
        if pre_start > 0:
            pre_context = "..." + pre_context

        post_end = min(len(text_orig), change_end + CONTEXT_SIZE)
        post_context = text_orig[change_end:post_end]
        post_context = _balance_context_line(post_context.replace("\n", " ").replace("\r", ""))
        if post_end < len(text_orig):
            post_context = post_context + "..."

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
        "Accepts every tracked change in the document, producing a finalized clean document. "
        "Use this when a document review is entirely complete. "
        "For selective acceptance/rejection of specific changes, use `process_document_batch` instead. "
        "\n\n"
        "remove_comments (boolean, DEFAULT TRUE): also delete every comment. The default is TRUE "
        "because this tool's purpose is a distributable clean document, and comments are internal "
        "review notes that must not travel to a counterparty. Pass remove_comments=false to accept "
        "the tracked changes while KEEPING the comments — use that when the review conversation is "
        "still live. Either way the response reports how many comments were deleted and names each "
        "one with its author, and comments whose anchored text an accepted deletion consumes are "
        "removed regardless, exactly as Word does."
    ),
    tags={"docx"},
    annotations={"destructiveHint": True},
)
async def accept_all_changes(
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
    remove_comments: Annotated[
        bool,
        "Also delete every comment in the document. Defaults to True (finalized clean document); "
        "pass false to keep comments while accepting the tracked changes.",
    ] = True,
    reasoning: Annotated[
        Optional[str],
        "Why do I need to accept all changes in this document? State this reason before any other parameter.",
    ] = "",
) -> str:
    start_time = time.perf_counter()
    del reasoning  # reason-first UX; not used by the tool.
    await ctx.info(f"Accepting all changes for document: {Path(docx_path).name}")
    try:
        stream = read_file_bytes(docx_path)
        engine = RedlineEngine(stream, id_discovery_hint=MCP_ID_DISCOVERY_HINT)

        # This surface DELIBERATELY defaults to True while the library API
        # defaults to False: `accept_all_changes` exists to produce a
        # distributable clean document, and shipping a counterparty a file that
        # still carries internal review notes is the more expensive failure
        # (QA_ISSUES_DISCOVERED #10, "Confidentiality risk"). What
        # BUG_comment_threading_anchoring_and_typography.md B2 correctly
        # objected to was that the inversion was SILENT and unavoidable — the
        # caller now has an explicit parameter, the published description states
        # the default, and every deleted comment is named with its author.
        await ctx.debug(f"Engine loaded, executing accept_all_revisions(remove_comments={remove_comments})")
        counts = engine.accept_all_revisions(remove_comments=remove_comments)
        removed_comment_notes = list(engine.removed_comment_notes)

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_clean{p.suffix}")

        overwrite_note = _overwrite_note(output_path, docx_path)
        save_stream(engine.save_to_stream(), output_path)
        await ctx.info("Clean document saved successfully", extra={"output_path": output_path})

        accepted_ins = counts.get("accepted_insertions", 0)
        accepted_del = counts.get("accepted_deletions", 0)
        accepted_fmt = counts.get("accepted_formatting", 0)
        removed_comments = counts.get("removed_comments", 0)

        # A no-op must be reported as one (QA 2026-07-23 F18), and comment
        # removal is destructive review-content cleanup that the response
        # must disclose, never perform silently (F12) — naming each comment AND
        # its author, because a comment the caller did not write is somebody
        # else's work product (B2).
        if accepted_ins + accepted_del + accepted_fmt + removed_comments == 0:
            res = (
                "No tracked changes or comments to accept — the document is "
                f"already clean. Saved to: {output_path}{overwrite_note}"
            )
        else:
            res = (
                f"Accepted all changes. Saved to: {output_path}\n"
                f"Insertions accepted: {accepted_ins}\n"
                f"Deletions accepted: {accepted_del}\n"
                f"Formatting changes accepted: {accepted_fmt}\n"
                f"Comments removed: {removed_comments}"
            )
            if removed_comment_notes:
                res += "\nComments deleted: " + ", ".join(removed_comment_notes)
                if not remove_comments:
                    res += (
                        "\nNote: these comments were anchored to text an accepted deletion "
                        "consumed, so Word removes them too. Nothing else was deleted."
                    )
            elif not remove_comments:
                res += "\nComments kept (remove_comments=false)."
            res += overwrite_note
        return add_timing_if_debug(start_time, res)
    except Exception as e:
        await ctx.error(
            "Failed to accept all changes",
            extra={"error": str(e), "docx_path": docx_path},
        )
        return add_timing_if_debug(start_time, f"Error accepting changes: {str(e)}")


@tool(
    description=(
        "Applies whole-text revised text to a DOCX document by computing a diff and generating tracked changes. "
        "Includes a clean-text verification gate to ensure the applied document matches the supplied text."
    ),
    tags={"docx"},
    annotations={"destructiveHint": True},
)
async def apply_text_revision(
    file_path: Annotated[str, "Absolute path to the source DOCX file."],
    revised_text: Annotated[str, "The complete revised clean text of the document."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path for the modified DOCX."] = None,
    author: Annotated[Optional[str], "Author name for Track Changes."] = None,
    allow_major_deletions: Annotated[
        bool,
        "Allow deleting >50% of characters (>75% for documents under 2000 characters).",
    ] = False,
    reasoning: Annotated[
        Optional[str],
        "Why do I need to apply this text revision? State this reason before any other parameter.",
    ] = "",
) -> str:
    start_time = time.perf_counter()
    del reasoning  # reason-first UX; not used by the tool.
    await ctx.info(f"Applying text revision to document: {Path(file_path).name}")

    from adeu.text_revision import (
        TextRevisionVerificationError,
        apply_text_revision_core,
    )

    try:
        stats, out_path = await asyncio.to_thread(
            apply_text_revision_core,
            file_path,
            revised_text,
            output_path,
            author,
            allow_major_deletions,
        )
        applied_count = stats.get("edits_applied", 0)
        res = f"Text revision complete. Saved to: {out_path}\nEdits: {applied_count} applied."
        return add_timing_if_debug(start_time, res)
    except TextRevisionVerificationError as e:
        await ctx.error(
            "Text revision verification failed",
            extra={
                "unverified_path": str(e.unverified_path),
                "output_path": str(e.output_path),
            },
        )
        raise ToolError(str(e)) from e
    except (ValueError, FileNotFoundError) as e:
        await ctx.error(
            "Failed to apply text revision",
            extra={"error": str(e), "file_path": file_path},
        )
        raise ToolError(str(e)) from e
    except Exception as e:
        await ctx.error(
            "Failed to apply text revision",
            extra={"error": str(e), "file_path": file_path},
        )
        raise ToolError(f"Error applying text revision: {str(e)}") from e


@tool(
    description="Opens a local file in its native desktop application (e.g., Microsoft Word for DOCX files).",
    tags={"docx"},
    annotations={"openWorldHint": True},
)
async def open_local_file(
    file_path: Annotated[str, "Absolute path to the file to open."],
    ctx: Context,
    reasoning: Annotated[
        Optional[str],
        "Why do I need to open this file in its native app? State this reason before any other parameter.",
    ] = "",
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
        output_schema=READ_DOCX_OUTPUT_SCHEMA,
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
            Literal["full", "outline", "appendix", "changes"],
            "'full' returns body content (paginated). 'outline' returns a structural "
            "heading map. 'appendix' returns defined terms, anchors, and diagnostics. "
            "'changes' returns a tracked changes & comments ledger.",
        ] = "full",
        page: Annotated[
            Optional[Union[int, str]],
            Field(
                description=(
                    "Without `search_query`: 1-indexed document page number or page range "
                    "(e.g. 1 or '2-6', defaults to 1) "
                    "for mode='full' and mode='appendix'; pass `page='all'` with mode='full' to "
                    "get the ENTIRE document in one response without page banners. With "
                    "`search_query`: restricts matches to that document page (defaults to "
                    "searching all pages; pass `page='all'` to be explicit)."
                ),
            ),
        ] = None,
        force: Annotated[
            bool,
            "If True, overrides response budget limit and returns full document text even if oversized.",
        ] = False,
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
        max_matches: Annotated[
            int,
            "For search queries: maximum number of search matches to return (default 20).",
        ] = 20,
        match_offset: Annotated[
            int,
            "For search queries: 0-based match offset to start search results from for pagination (default 0).",
        ] = 0,
        full_paragraph: Annotated[
            bool,
            "For search queries: return full paragraph for search matches instead of clamping snippets to ±120 chars.",
        ] = False,
        changes_author: Annotated[
            Optional[str],
            "For mode='changes' only: filter tracked changes ledger by author name.",
        ] = None,
        changes_offset: Annotated[
            int,
            "For mode='changes' only: entry offset for paginating tracked changes ledger.",
        ] = 0,
        reasoning: Annotated[
            Optional[str],
            "Why do I need to read this docx document? State this reason before any other parameter.",
        ] = "",
    ) -> ToolResult:
        start_time = time.perf_counter()
        del reasoning
        # Outside of search mode and changes mode, `page` semantically means "document page" and
        # defaults to 1. In search mode and changes mode, `page` is a document-page filter and
        # `None` means "search all pages" or "return all changes" — we leave it as None to let the
        # response builder distinguish "omitted" from "explicit 1".
        if search_query is None and mode != "changes" and page is None:
            page = 1
        if not file_path:
            # Read active document directly. No disk fallback available if this fails.
            res = await read_active_word_document(
                ctx,
                clean_view,
                None,
                mode=mode,
                page=page,
                force=force,
                outline_max_level=outline_max_level,
                outline_verbose=outline_verbose,
                search_query=search_query,
                search_regex=search_regex,
                search_case_sensitive=search_case_sensitive,
                changes_author=changes_author,
                changes_offset=changes_offset,
                max_matches=max_matches,
                match_offset=match_offset,
                full_paragraph=full_paragraph,
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
                        force=force,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        search_query=search_query,
                        search_regex=search_regex,
                        search_case_sensitive=search_case_sensitive,
                        changes_author=changes_author,
                        changes_offset=changes_offset,
                        max_matches=max_matches,
                        match_offset=match_offset,
                        full_paragraph=full_paragraph,
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
                        force=force,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        search_query=search_query,
                        search_regex=search_regex,
                        search_case_sensitive=search_case_sensitive,
                        changes_author=changes_author,
                        changes_offset=changes_offset,
                        max_matches=max_matches,
                        match_offset=match_offset,
                        full_paragraph=full_paragraph,
                    )
            else:
                res = await _read_docx_disk(
                    file_path,
                    ctx,
                    clean_view,
                    mode,
                    page,
                    force=force,
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                    search_query=search_query,
                    search_regex=search_regex,
                    search_case_sensitive=search_case_sensitive,
                    changes_author=changes_author,
                    changes_offset=changes_offset,
                    max_matches=max_matches,
                    match_offset=match_offset,
                    full_paragraph=full_paragraph,
                )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_WIN32_EXTRA + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        ctx: Context,
        changes: Annotated[
            McpBatchChanges,
            "List of changes to apply. Each change must specify 'type'.",
        ],
        author_name: Annotated[
            str,
            "Name to appear in Track Changes (e.g., 'Reviewer AI'). Defaults to 'Adeu AI' when omitted.",
        ] = "Adeu AI",
        original_docx_path: Annotated[
            Optional[str],
            "Path to source file. LEAVE EMPTY (Null) to edit the live Word document!",
        ] = None,
        output_path: Annotated[
            Optional[str],
            "Optional output path (only used if original_docx_path is provided).",
        ] = None,
        partial: Annotated[
            bool,
            "Whether to apply valid edits when some fail (salvage mode). Defaults to True.",
        ] = True,
        reasoning: Annotated[
            Optional[str],
            "Why do I need to apply these changes to the document? State this reason before any other parameter.",
        ] = "",
    ) -> str:
        start_time = time.perf_counter()
        del reasoning  # reason-first UX; not used by the tool.
        # FastMCP's parameter validation does not always honor the BeforeValidator
        # attached to McpBatchChanges (it flattens the Annotated chain and validates
        # against the bare list type), so coerce here as a defensive second pass.
        # This is also what catches stringified-object lists emitted by some LLM
        # clients (notably Gemini under load).
        changes, rejected_notes = _normalize_changes(changes)
        if not changes and rejected_notes:
            env = failure_envelope(
                "invalid_changes_file",
                getattr(rejected_notes, "pairs", []),
                "All submitted changes failed validation.",
                errors=rejected_notes,
            )
            json_block = f"\n\n```json\n{json.dumps(env, ensure_ascii=False)}\n```"
            return add_timing_if_debug(
                start_time,
                "Error: No valid changes to apply. All submitted changes failed validation.\n"
                + "\n".join(f"- {n}" for n in rejected_notes)
                + json_block,
            )
        if not original_docx_path:
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
                    rejected_notes=rejected_notes,
                    partial=partial,
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
                rejected_notes=rejected_notes,
                partial=partial,
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
            reasoning: Annotated[
                Optional[str],
                "Why do I need this structural XML diff? State this reason before any other parameter.",
            ] = "",
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
            file_path: Annotated[str, "Absolute path to the DOCX file to open in Word."],
            ctx: Context,
            visible: Annotated[bool, "Whether to make the Word application window visible."] = True,
            reasoning: Annotated[
                Optional[str],
                "Why do I need to open this document in Word? State this reason before any other parameter.",
            ] = "",
        ) -> str:
            start_time = time.perf_counter()
            del reasoning  # reason-first UX; not used by the tool.
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
            reasoning: Annotated[
                Optional[str],
                "Why do I need to save the active document? State this reason before any other parameter.",
            ] = "",
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
        output_schema=READ_DOCX_OUTPUT_SCHEMA,
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
            Literal["full", "outline", "appendix", "changes"],
            "'full' returns body content (paginated for large docs). 'outline' returns "
            "a structural heading map with page numbers; body content is omitted. "
            "'appendix' returns defined terms, anchors, and diagnostics — consult before "
            "editing. 'changes' returns a tracked changes & comments ledger.",
        ] = "full",
        page: Annotated[
            Optional[Union[int, str]],
            Field(
                description=(
                    "Without `search_query`: 1-indexed document page number or page range "
                    "(e.g. 1 or '2-6', defaults to 1) "
                    "for mode='full' and mode='appendix'; pass `page='all'` to get the ENTIRE document in one "
                    "response without page banners. With `search_query`: restricts matches to "
                    "that document page (defaults to searching all pages; pass `page='all'` to "
                    "be explicit)."
                ),
            ),
        ] = None,
        force: Annotated[
            bool,
            "If True, overrides response budget limit and returns full document text even if oversized.",
        ] = False,
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
        max_matches: Annotated[
            int,
            "For search queries: maximum number of search matches to return (default 20).",
        ] = 20,
        match_offset: Annotated[
            int,
            "For search queries: 0-based match offset to start search results from for pagination (default 0).",
        ] = 0,
        full_paragraph: Annotated[
            bool,
            "For search queries: return full paragraph for search matches instead of clamping snippets to ±120 chars.",
        ] = False,
        changes_author: Annotated[
            Optional[str],
            "For mode='changes' only: filter tracked changes ledger by author name.",
        ] = None,
        changes_offset: Annotated[
            int,
            "For mode='changes' only: entry offset for paginating tracked changes ledger.",
        ] = 0,
        reasoning: Annotated[
            Optional[str],
            "Why do I need to read this docx document? State this reason before any other parameter.",
        ] = "",
    ) -> ToolResult:
        start_time = time.perf_counter()
        del reasoning  # reason-first UX; not used by the tool.
        if search_query is None and mode != "changes" and page is None:
            page = 1
        res = await _read_docx_disk(
            file_path,
            ctx,
            clean_view,
            mode,
            page,
            force=force,
            outline_max_level=outline_max_level,
            outline_verbose=outline_verbose,
            search_query=search_query,
            search_regex=search_regex,
            search_case_sensitive=search_case_sensitive,
            changes_author=changes_author,
            changes_offset=changes_offset,
            max_matches=max_matches,
            match_offset=match_offset,
            full_paragraph=full_paragraph,
        )
        return add_timing_if_debug(start_time, res)

    @tool(
        description=PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
        tags={"docx"},
        annotations={"destructiveHint": True},
    )
    async def process_document_batch(
        original_docx_path: Annotated[str, "Absolute path to the source file."],
        ctx: Context,
        changes: Annotated[
            McpBatchChanges,
            "List of changes to apply. Each change must specify 'type'.",
        ],
        author_name: Annotated[
            str,
            "Name to appear in Track Changes (e.g., 'Reviewer AI'). Defaults to 'Adeu AI' when omitted.",
        ] = "Adeu AI",
        output_path: Annotated[Optional[str], "Optional output path."] = None,
        partial: Annotated[
            bool,
            "Whether to apply valid edits when some fail (salvage mode). Defaults to True.",
        ] = True,
        reasoning: Annotated[
            Optional[str],
            "Why do I need to apply these changes to the document? State this reason before any other parameter.",
        ] = "",
    ) -> str:
        start_time = time.perf_counter()
        del reasoning
        # See win32 branch above for why we re-coerce here.
        changes, rejected_notes = _normalize_changes(changes)
        if not changes and rejected_notes:
            env = failure_envelope(
                "invalid_changes_file",
                getattr(rejected_notes, "pairs", []),
                "All submitted changes failed validation.",
                errors=rejected_notes,
            )
            json_block = f"\n\n```json\n{json.dumps(env, ensure_ascii=False)}\n```"
            return add_timing_if_debug(
                start_time,
                "Error: No valid changes to apply. All submitted changes failed validation.\n"
                + "\n".join(f"- {n}" for n in rejected_notes)
                + json_block,
            )
        res = await _process_document_batch_disk(
            original_docx_path,
            author_name,
            ctx,
            changes,
            output_path,
            rejected_notes=rejected_notes,
            partial=partial,
        )
        return add_timing_if_debug(start_time, res)
