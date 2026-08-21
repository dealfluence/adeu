"""
JSON-Lines daemon (`adeu serve`) keeping a single process alive with a warm
`doc_cache` for high-volume stdin/stdout JSON-lines callers.
"""

import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict

from pydantic import TypeAdapter, ValidationError

from adeu.cli import (
    _extract_schema_failures,
    _load_docx_or_exit,
    _open_redline_engine_or_exit,
    _require_docx_output,
    _write_output_or_exit,
    take_last_cli_error,
)
from adeu.mcp_components._response_builders import (
    BuilderError,
    build_appendix_response,
    build_changes_response,
    build_fields_response,
    build_full_document_response,
    build_outline_response,
    build_page_range_response,
    build_paginated_response,
    build_search_response,
)
from adeu.mcp_components.doc_cache import doc_cache
from adeu.models import StrictBatchChanges
from adeu.pagination import parse_page_arg
from adeu.payloads import failure_envelope, shrink_batch_stats
from adeu.redline.engine import BatchValidationError


def _emit_json(data: Dict[str, Any]) -> None:
    """Emits a single unindented JSON string to stdout and flushes."""
    print(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()


def _emit_error(code: str, message: str) -> None:
    """Emits a standard error response JSON object."""
    _emit_json({"status": "error", "error": code, "message": message})


def _emit_cli_error(fallback_code: str, fallback_message: str, **extra: Any) -> None:
    """
    Emits the failure envelope recorded by the CLI helper that raised, falling
    back to `fallback_code`/`fallback_message` for failures that did not come
    from `_cli_error`.

    The in-process CLI helpers (`_load_batch_from_json`, `_write_output_or_exit`)
    report failure by exiting, so the caught exception stringifies to the exit
    code — "Error: 1" — with the code and diagnostics dropped.
    `take_last_cli_error()` recovers both.
    """
    env = take_last_cli_error() or {"error": fallback_code, "message": fallback_message}
    _emit_json({"status": "error", **env, **extra})


def _handle_extract_command(req: Dict[str, Any]) -> None:
    file_path = req.get("file_path") or req.get("input")
    if not file_path:
        _emit_error("invalid_input", "Missing required field 'file_path'")
        return

    path = Path(file_path)
    if not path.exists():
        _emit_error("file_not_found", f"File not found: {file_path}")
        return
    if not path.is_file():
        _emit_error("invalid_input", f"'{file_path}' is a directory, not a file.")
        return

    clean_view = bool(req.get("clean_view", False))
    mode = req.get("mode", "full")
    if mode not in ("full", "outline", "appendix", "changes", "fields"):
        _emit_error("invalid_input", f"Invalid mode: '{mode}'")
        return

    if mode == "changes" and clean_view:
        _emit_error("invalid_input", "--clean-view cannot be combined with --mode changes.")
        return

    page_arg = req.get("page", None)
    search_query = req.get("search_query", None)
    no_chrome = bool(req.get("no_chrome", False))

    try:
        key = doc_cache.stat_key(str(path))
        entry = doc_cache.entry(key)

        page_num = 1
        want_all_pages = False
        is_page_range = False
        range_start = 1
        range_end = 1

        if page_arg is not None and not search_query and mode != "outline":
            try:
                kind, page_val = parse_page_arg(str(page_arg))
            except ValueError:
                _emit_error(
                    "invalid_input",
                    f"Invalid --page value: '{page_arg}'. Provide a positive integer "
                    "(pages are 1-indexed; 'all' is valid for --mode full and --search-query; or range 'N-M').",
                )
                return

            if kind == "range":
                if mode == "appendix":
                    _emit_error(
                        "invalid_input",
                        "Page range pagination is only supported in 'full' mode, not 'appendix' mode.",
                    )
                    return
                assert isinstance(page_val, tuple)
                is_page_range = True
                range_start, range_end = page_val
            elif kind == "all":
                if mode == "full" or mode == "changes":
                    want_all_pages = True
                else:
                    _emit_error(
                        "invalid_input",
                        f"Invalid --page value: '{page_arg}'. Provide a positive integer "
                        "(pages are 1-indexed; 'all' is valid for --mode full and --search-query; or range 'N-M').",
                    )
                    return
            else:
                assert isinstance(page_val, int)
                page_num = page_val

        if search_query:
            text, _ = doc_cache.get_pagination(entry, clean_view=clean_view)
            res = build_search_response(
                text,
                search_query,
                bool(req.get("search_regex", False)),
                not bool(req.get("search_case_insensitive", False)),
                page_arg,
                str(path),
                is_cli=True,
                max_matches=int(req.get("max_matches", 20)),
                match_offset=int(req.get("match_offset", 0)),
                full_paragraph=bool(req.get("full_paragraph", False)),
                no_chrome=no_chrome,
            )
        elif mode == "outline":
            text, _, outline_nodes = doc_cache.get_outline(entry, clean_view=clean_view)
            # `doc_cache.get_outline` always returns the outline nodes (filling
            # them from its own parse when cold), and build_outline_response
            # never consults `doc` once nodes are supplied. Loading the DOCX
            # here would re-parse the package the cache just served.
            res = build_outline_response(
                None,
                text,
                str(path),
                outline_max_level=int(req.get("outline_max_level", 2)),
                outline_verbose=bool(req.get("outline_verbose", False)),
                is_cli=True,
                outline_nodes=outline_nodes,
                no_chrome=no_chrome,
            )
        elif mode == "fields":
            # RAW projection: the ledger previews values from the text between a
            # control's anchors, which the clean view rewrites.
            text, _ = doc_cache.get_pagination(entry, clean_view=False)
            res = build_fields_response(
                _load_docx_or_exit(path),
                text,
                str(path),
                offset=int(req.get("fields_offset", 0)),
                is_cli=True,
                no_chrome=no_chrome,
            )
        elif mode == "changes":
            # Ordered ahead of `want_all_pages` exactly as in cli.py: the ledger
            # is already a bounded summary, so `--page all --mode changes` is
            # exempt from the response budget guard.
            text, _ = doc_cache.get_pagination(entry, clean_view=False)
            doc = _load_docx_or_exit(path)
            from adeu.redline.comments import CommentsManager

            comments_data = CommentsManager(doc).extract_comments_data()
            try:
                engine = _open_redline_engine_or_exit(path)
                existing_change_ids = set(engine._existing_change_ids())
            except (Exception, SystemExit):
                # The ledger degrades gracefully without the id set (cli.py does
                # the same). SystemExit means the CLI helper recorded a failure
                # envelope; drain it so it cannot be misreported as a later error.
                take_last_cli_error()
                existing_change_ids = None

            res = build_changes_response(
                text,
                str(path),
                comments_data=comments_data,
                author_filter=req.get("changes_author", None),
                page=page_arg,
                offset=int(req.get("changes_offset", 0)),
                is_cli=True,
                existing_change_ids=existing_change_ids,
                no_chrome=no_chrome,
            )
        elif is_page_range:
            text, _ = doc_cache.get_pagination(entry, clean_view=clean_view)
            res = build_page_range_response(
                text,
                range_start,
                range_end,
                str(path),
                is_cli=True,
                no_chrome=no_chrome,
            )
        elif mode == "appendix":
            text = doc_cache.get_text_with_appendix(entry, clean_view=clean_view)
            res = build_appendix_response(
                text,
                page_num,
                str(path),
                is_cli=True,
                no_chrome=no_chrome,
            )
        elif want_all_pages:
            text = doc_cache.get_base_text(entry, clean_view=clean_view)
            from adeu.payloads import response_budget_limit

            if not req.get("force", False) and len(text) > response_budget_limit():
                from adeu.mcp_components._response_builders import build_budget_guard_message

                text_out, _, outline_nodes = doc_cache.get_outline(entry, clean_view=clean_view)
                msg = build_budget_guard_message(
                    text_out,
                    str(path),
                    outline_nodes=outline_nodes,
                    is_cli=True,
                )
                _emit_error("response_budget_exceeded", msg)
                return

            res = build_full_document_response(
                text,
                str(path),
                no_chrome=no_chrome,
            )
        else:
            text, pagination = doc_cache.get_pagination(entry, clean_view=clean_view)
            res = build_paginated_response(
                text,
                page_num,
                str(path),
                is_cli=True,
                pagination_result=pagination,
                no_chrome=no_chrome,
            )

        _emit_json(res.structured_content or {})
    except BuilderError as e:
        _emit_error("invalid_input", str(e))
    except (zipfile.BadZipFile, ValueError) as e:
        err_str = str(e)
        if "must be a DOCX file" in err_str or "not a valid DOCX file" in err_str:
            _emit_error("invalid_docx", err_str)
        else:
            _emit_error("invalid_input", f"Error: {e}")
    except (Exception, SystemExit) as e:
        _emit_cli_error("invalid_input", f"Error: {e}")


def _handle_apply_command(req: Dict[str, Any]) -> None:
    file_path = req.get("file_path") or req.get("original") or req.get("input")
    if not file_path:
        _emit_error("invalid_input", "Missing required field 'file_path'")
        return

    path = Path(file_path)
    if not path.exists():
        _emit_error("file_not_found", f"File not found: {file_path}")
        return
    if not path.is_file():
        _emit_error("invalid_input", f"'{file_path}' is a directory, not a file.")
        return

    changes_raw = req.get("changes")
    if changes_raw is None:
        _emit_error("invalid_input", "Must provide changes.")
        return

    author = req.get("author")
    partial = bool(req.get("partial", False))
    terse_errors = bool(req.get("terse_errors", False))
    report_style = req.get("report", "standard")
    output_path_str = req.get("output") or req.get("output_path")
    output_path = Path(output_path_str) if output_path_str else None

    try:
        # Checked up front, exactly where handle_apply in cli.py checks it: a
        # DOCX package hiding behind 'out.txt' breaks every consumer that
        # trusts the extension, batch outcome notwithstanding. The helper
        # signals refusal with SystemExit, recovered below as invalid_input.
        _require_docx_output(output_path)

        if isinstance(changes_raw, list):
            adapter = TypeAdapter(StrictBatchChanges)
            changes = adapter.validate_python(changes_raw)
        elif isinstance(changes_raw, str):
            changes_path = Path(changes_raw)
            if not changes_path.exists():
                _emit_error("file_not_found", f"Changes file not found: {changes_raw}")
                return
            from adeu.cli import _load_batch_from_json

            changes = _load_batch_from_json(changes_path)
        else:
            _emit_error("invalid_input", "Invalid 'changes' format; expected array or file path string.")
            return

        engine = _open_redline_engine_or_exit(path, author=author, terse_errors=terse_errors)
        stats = engine.process_batch(changes, partial=partial)

        applied_count = stats.get("edits_applied", 0) + stats.get("actions_applied", 0)
        is_partial = (stats.get("status") == "partial" or bool(stats.get("failed"))) and partial

        # Same three-way rule as handle_apply in cli.py. In particular an EMPTY
        # batch is not a failure: `adeu apply` writes an unmodified copy and
        # succeeds, so "nothing applied" alone must not become an error.
        if is_partial and applied_count > 0:
            batch_failed = False
        elif partial and applied_count == 0 and len(changes) > 0:
            batch_failed = True
        else:
            batch_failed = stats.get("actions_skipped", 0) > 0 or stats.get("edits_skipped", 0) > 0

        if batch_failed:
            stats["status"] = "error"
            stats["output_path"] = None
        else:
            if output_path is None:
                if path.stem.endswith("_redlined") or path.stem.endswith("_processed"):
                    output_path = path
                else:
                    output_path = path.with_name(f"{path.stem}_redlined.docx")

            try:
                _write_output_or_exit(output_path, engine.save_to_stream().getvalue())
            except (SystemExit, OSError) as e:
                _emit_cli_error("write_failed", f"Could not write output file '{output_path}': {e}", output_path=None)
                return
            stats["output_path"] = str(output_path)

        if report_style == "minimal":
            stats = shrink_batch_stats(stats)

        _emit_json(stats)
    except ValidationError as e:
        failed_pairs, prose_msg = _extract_schema_failures(e)
        env = failure_envelope("invalid_changes_file", failed_pairs, prose_msg)
        env["status"] = "error"
        env["output_path"] = None
        _emit_json(env)
    except BatchValidationError as e:
        env = failure_envelope(
            "batch_validation_failed", e.failed, "Batch rejected. Edits failed validation.", errors=e.errors
        )
        env["status"] = "error"
        env["output_path"] = None
        _emit_json(env)
    except (zipfile.BadZipFile, ValueError) as e:
        err_str = str(e)
        if "must be a DOCX file" in err_str or "not a valid DOCX file" in err_str:
            _emit_error("invalid_docx", err_str)
        else:
            _emit_error("invalid_input", f"Error: {e}")
    except (Exception, SystemExit) as e:
        _emit_cli_error("invalid_input", f"Error: {e}", output_path=None)


def run_serve() -> int:
    """
    Runs the JSON-lines daemon reading from sys.stdin and writing to sys.stdout.
    """
    from adeu.cli import _set_json_mode

    _set_json_mode(False)

    for line in sys.stdin:
        line_str = line.strip()
        if not line_str:
            continue

        try:
            req = json.loads(line_str)
        except Exception as e:
            _emit_error("invalid_input", f"Malformed JSON line: {e}")
            continue

        if not isinstance(req, dict):
            _emit_error("invalid_input", "JSON line must be an object.")
            continue

        cmd = req.get("command")
        if not cmd or not isinstance(cmd, str):
            _emit_error("invalid_input", "Missing or invalid 'command' field.")
            continue

        try:
            if cmd == "ping":
                _emit_json({"status": "ok", "pong": True})
            elif cmd == "exit":
                return 0
            elif cmd == "extract":
                _handle_extract_command(req)
            elif cmd == "apply":
                _handle_apply_command(req)
            else:
                _emit_error("invalid_input", f"Unknown command: '{cmd}'")
        except (Exception, SystemExit) as e:
            _emit_cli_error("error", f"Error executing command '{cmd}': {e}")

    return 0
