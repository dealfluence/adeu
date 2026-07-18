import argparse
import codecs
import datetime
import getpass
import json
import os
import platform
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Sequence

from pydantic import TypeAdapter, ValidationError

from adeu.markup import apply_edits_to_markdown
from adeu.mcp_components.shared import get_build_info
from adeu.models import BatchChanges, DeleteTableRow, DocumentChange, InsertTableRow, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine, validate_edit_strings
from adeu.sanitize.core import SanitizeError, SanitizeResult, sanitize_docx
from adeu.utils.console import configure_cli_streams, dynamic_stderr


def _get_claude_config_path() -> Path:
    """Determine the location of claude_desktop_config.json based on OS."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            raise OSError("APPDATA environment variable not found.")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    elif system == "Darwin":  # macOS
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def handle_init(args: argparse.Namespace):
    """
    Configures Adeu in the Claude Desktop environment.
    1. Checks for 'uvx'.
    2. Locates config file.
    3. Backs up existing config.
    4. Injects MCP server entry.
    """
    print("🤖 Adeu Agentic Setup", file=sys.stderr)

    try:
        config_path = _get_claude_config_path()
    except Exception as e:
        print(f"❌ Error locating Claude config: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"📍 Config found: {config_path}", file=sys.stderr)

    data: Dict[str, Any] = {"mcpServers": {}}
    existing_valid_json = True
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
        except json.JSONDecodeError:
            existing_valid_json = False
            print("⚠️  Existing config was invalid JSON. Starting fresh.", file=sys.stderr)

    mcp_servers = data.setdefault("mcpServers", {})

    if args.local:
        cwd = Path.cwd().resolve()
        python_exe = sys.executable
        # --local means "run the MCP server from this source checkout". From
        # an arbitrary directory that claim is false and the resulting config
        # is misleading (QA 2026-07-18 L6) — verify before writing.
        looks_like_checkout = (cwd / "src" / "adeu" / "__init__.py").is_file() or (
            (cwd / "pyproject.toml").is_file()
            and 'name = "adeu"' in (cwd / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
        )
        if not looks_like_checkout:
            print(
                f"❌ --local expects to run from an Adeu source checkout, but '{cwd}' contains "
                "no src/adeu package or adeu pyproject.toml.\n"
                "   cd into your adeu repository's python/ directory and re-run, or use plain "
                "'adeu init' to configure the installed package.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("🔧 Configuring in LOCAL DEV mode.", file=sys.stderr)
        print(f"   - CWD: {cwd}", file=sys.stderr)
        print(f"   - Python: {python_exe}", file=sys.stderr)

        mcp_servers["adeu"] = {
            "command": python_exe,
            "args": ["-m", "adeu.server"],
            "cwd": str(cwd),
        }
    else:
        # Resolve the absolute path to uvx so Claude Desktop (which runs
        # with a stripped PATH) can find it even on macOS/Linux where it
        # typically lives in ~/.local/bin — outside the GUI app's PATH.
        uvx_path = shutil.which("uvx")
        if not uvx_path:
            print(
                "❌ Could not find 'uvx' in your PATH.\n"
                "   Install uv first:\n"
                "     macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                "     Windows:     powershell -ExecutionPolicy ByPass -c "
                '"irm https://astral.sh/uv/install.ps1 | iex"',
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"🔍 Found uvx at: {uvx_path}", file=sys.stderr)

        mcp_servers["adeu"] = {
            "command": uvx_path,  # absolute path, not bare "uvx"
            "args": ["--from", "adeu", "adeu-server", "--scope", args.scope],
        }

    new_content = json.dumps(data, indent=2)

    # No-op detection: re-running init with an unchanged result must neither
    # rewrite the config nor pile up .bak files (QA 2026-07-18 L5).
    if config_path.exists():
        try:
            current_content = config_path.read_text(encoding="utf-8")
        except OSError:
            current_content = None
        if current_content is not None and existing_valid_json:
            try:
                unchanged = json.loads(current_content or "{}") == data
            except json.JSONDecodeError:
                unchanged = False
            if unchanged:
                print("✅ Adeu is already configured — config unchanged, no backup needed.", file=sys.stderr)
                return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.{timestamp}.bak")
        shutil.copy2(config_path, backup_path)
        print(f"📦 Backup created: {backup_path.name}", file=sys.stderr)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print("✅ Adeu successfully configured in Claude Desktop.", file=sys.stderr)
    print("   Please restart Claude to load the new toolset.", file=sys.stderr)


# When True (set per-invocation from a subcommand's --json flag), every fatal
# CLI error emits one machine-readable JSON object on stdout in addition to
# the human diagnostics on stderr. Without this, automation had to parse two
# unrelated error protocols (QA 2026-07-18 M8).
_JSON_MODE = False


def _set_json_mode(enabled: bool) -> None:
    global _JSON_MODE
    _JSON_MODE = bool(enabled)


def _cli_error(code: str, message: str, exit_code: int = 1, hint: "str | None" = None) -> None:
    """
    Terminates the CLI with a consistent error contract:
      - human-readable diagnostics on stderr (always)
      - a single {"error": code, "message": ...} JSON object on stdout when
        the invocation asked for --json
    Stable codes: file_not_found, invalid_input, invalid_docx,
    invalid_changes_file, write_failed, unsupported, batch_validation_failed.
    """
    print(f"❌ {message}", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    if _JSON_MODE:
        print(json.dumps({"error": code, "message": message}))
    sys.exit(exit_code)


def _print_sandbox_warning_and_exit(path: Path, exit_code: int = 1):
    _cli_error(
        "file_not_found",
        f"File not found: {path}",
        exit_code=exit_code,
        hint=(
            "Note: If you are running in a sandboxed/containerized environment, "
            "the host application or MCP server may not have access to your local workspace files. "
            "You can resolve this by installing Adeu directly inside your sandboxed environment using "
            "'uv tool install adeu' and executing the commands via the CLI."
        ),
    )


def _require_input_file(path: Path, exit_code: int = 1) -> None:
    """
    Validates that an input path exists AND is a regular file. A directory
    satisfies `.exists()`, so `.is_file()` is what turns `open(dir, 'rb')`'s
    raw IsADirectoryError into a clean CLI error (QA 2026-07-17 F7).
    """
    if not path.exists():
        _print_sandbox_warning_and_exit(path, exit_code)
    if not path.is_file():
        _cli_error("invalid_input", f"'{path}' is a directory, not a file.", exit_code=exit_code)


def _handle_docx_error_and_exit(filename: str, exc: Exception) -> None:
    import re

    err_str = str(exc)
    reason = "got bad zip signature"
    if "not a valid DOCX file" in err_str:
        match = re.search(r"not a valid DOCX file \(([^)]+)\)", err_str)
        if match:
            reason = match.group(1)
    _cli_error("invalid_docx", f"'{filename}' is not a valid DOCX file ({reason}).")


def _write_output_or_exit(path: Path, data: "bytes | str") -> None:
    """
    Writes an output file, converting filesystem failures (name too long,
    permission denied, missing directory, disk full) into the same clean
    exit-code-1 errors every other CLI failure path produces, instead of a
    raw traceback (QA H3).
    """
    try:
        if isinstance(data, bytes):
            with open(path, "wb") as fb:
                fb.write(data)
        else:
            with open(path, "w", encoding="utf-8") as ft:
                ft.write(data)
    except OSError as e:
        _cli_error("write_failed", f"Could not write output file '{path}': {e.strerror or e}")


def _read_docx_text(path: Path, clean_view: bool = False) -> str:
    """Projects a DOCX to text through the single shared open path."""
    doc = _load_docx_or_exit(path)
    try:
        from adeu.ingest import _extract_text_from_doc

        return _extract_text_from_doc(doc, clean_view=clean_view)
    except SystemExit:
        raise
    except Exception as e:
        _cli_error("invalid_docx", f"Error reading DOCX file '{path.name}': {e}")
        raise AssertionError("unreachable") from None


# The decorative header every extract response starts with (see
# _response_builders.py). It is presentation, not document content.
_EXTRACT_HEADER_RE = re.compile(r"^> \*\*File Path:\*\*[^\n]*\n+")

# Pagination chrome emitted by extract around each page (see pagination.py's
# build_page_banner / build_page_footer / build_appendix_pointer). Like the
# file-path header, it is presentation — but unlike the header, a banner or
# footer that names "page N of M" (M > 1) proves the text is only PART of the
# document, which can never round-trip through apply/diff safely
# (QA 2026-07-17 F1).
_PAGE_BANNER_RE = re.compile(r"^> \*\*Page (\d+) of (\d+)\*\*[^\n]*\n+(?:---\n+)?")
_PAGE_FOOTER_RE = re.compile(r"\n+---\n+> \*\*Continues on page (\d+) of (\d+)\.\*\*[^\n]*\s*$")
_APPENDIX_POINTER_RE = re.compile(r"\n+---\n+> \*\*Appendix available\.\*\*[^\n]*\s*$")

# CriticMarkup open tokens; their presence in a round-trip text file means the
# text was extracted in the default markup view (apply/diff compare against
# the CLEAN view, so markup tokens would be diffed INTO the document as prose).
_CRITICMARKUP_TOKENS = ("{++", "{--", "{>>", "{==")

# Guardrail for the text-diff path: refuse to silently delete the majority of
# a document. 2000 chars ≈ one page of prose; below that, halving a document
# in a single edit is a plausible deliberate workflow.
_MAJOR_DELETION_MIN_ORIGINAL_CHARS = 2000
_MAJOR_DELETION_RATIO = 0.5


def _strip_page_chrome(text: str) -> "tuple[str, int | None, int | None]":
    """
    Strips extract's page banner/footer/appendix-pointer chrome from a
    round-trip text file. Returns (stripped_text, page, total_pages);
    page/total_pages are None when the text carries no multi-page markers.
    """
    page = total = None
    banner = _PAGE_BANNER_RE.match(text)
    if banner:
        page, total = int(banner.group(1)), int(banner.group(2))
        text = text[banner.end() :]
    # The appendix pointer trails the footer, so strip it first.
    text = _APPENDIX_POINTER_RE.sub("", text)
    footer = _PAGE_FOOTER_RE.search(text)
    if footer:
        if page is None:
            page = int(footer.group(1)) - 1
        if total is None:
            total = int(footer.group(2))
        text = text[: footer.start()]
    return text, page, total


def _load_roundtrip_text(path: Path, original: Path, command: str) -> str:
    """
    Loads the modified-text file for the apply/diff text paths, stripping
    extract chrome and refusing inputs that cannot round-trip safely:

      - a single page of a multi-page extract (everything absent would be
        diffed as deleted — QA 2026-07-17 F1)
      - markup-view text containing CriticMarkup tokens (apply/diff compare
        against the CLEAN view, so the tokens — including reviewer names and
        change IDs — would be written into the document as literal prose,
        QA 2026-07-17 F8)
    """
    text, page, total = _strip_page_chrome(_read_text_file(path))

    if total is not None and total > 1:
        print(
            f"❌ '{path.name}' looks like page {page or '?'} of {total} of a paginated extract — "
            "it contains only part of the document, and applying it would delete every page "
            "not present.\n"
            "   Re-extract the ENTIRE document first:\n"
            f"     adeu extract {original} --page all --clean-view -o {path.name}\n"
            f"   then edit that file and re-run {command}.",
            file=sys.stderr,
        )
        sys.exit(1)

    if any(tok in text for tok in _CRITICMARKUP_TOKENS):
        print(
            f"❌ '{path.name}' contains CriticMarkup tokens ({{++..++}}, {{--..--}}, {{==..==}}, "
            "{>>..<<}), which means it was extracted in the default markup view. "
            f"`{command}` compares text against the document's CLEAN view, so markup-view text "
            "would be diffed into the document as literal prose (including reviewer names and "
            "change IDs).\n"
            "   Re-extract with --clean-view (add --page all for multi-page documents):\n"
            f"     adeu extract {original} --clean-view --page all -o {path.name}\n"
            "   then edit that file and re-run the command.",
            file=sys.stderr,
        )
        sys.exit(1)

    return text


def _read_text_file(path: Path) -> str:
    """Read a user-supplied text file with encoding tolerance.

    UTF-8 (with or without BOM) and BOM-marked UTF-16/32 decode silently;
    other content falls back to Windows-1252 with a loud warning — such files
    are typically produced by redirecting console output on a legacy Windows
    code page. Content with NUL bytes (BOM-less UTF-16, binaries) gets a
    guided error instead of flowing into edits as mojibake. Newlines are
    normalized to \\n, matching text-mode open(), and a leading extract
    file-path header is dropped so extract output round-trips cleanly.
    """
    _require_input_file(path)
    raw = path.read_bytes()

    bom_encoding = None
    if raw.startswith(codecs.BOM_UTF8):
        bom_encoding = "utf-8-sig"
    elif raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        bom_encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        bom_encoding = "utf-16"

    if bom_encoding is not None:
        try:
            text = raw.decode(bom_encoding)
        except UnicodeDecodeError as e:
            print(
                f"❌ '{path.name}' has a {bom_encoding} byte-order mark but its content "
                f"did not decode as {bom_encoding} ({e}). Re-save the file as UTF-8.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif b"\x00" in raw:
        # NUL bytes never occur in text: this is BOM-less UTF-16/32 or a binary.
        print(
            f"❌ '{path.name}' does not look like a text file (contains NUL bytes). "
            "If it is UTF-16 text (e.g. from a PowerShell '>' redirect), re-save it as UTF-8.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            try:
                text = raw.decode("cp1252")
            except UnicodeDecodeError:
                print(
                    f"❌ Could not decode '{path.name}': not valid UTF-8 "
                    f"(byte 0x{raw[e.start]:02x} at offset {e.start}) and not Windows-1252 either. "
                    "Re-save the file as UTF-8.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"⚠️ '{path.name}' is not valid UTF-8 (byte 0x{raw[e.start]:02x} at offset {e.start}); "
                "decoded as Windows-1252. Re-save the file as UTF-8 to avoid ambiguity.",
                file=sys.stderr,
            )

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Adeu's own extract output prepends a file-path header; strip it on
    # ingestion so the natural extract → edit → diff/apply round trip never
    # reports the header as a document change (QA 2026-07-16 run 2, F1).
    return _EXTRACT_HEADER_RE.sub("", text, count=1)


# One-line usage reference per change type, shown when a batch fails schema
# validation. These are otherwise only discoverable via the MCP schema.
_CHANGE_TYPE_REFERENCE = (
    "Each change must be a JSON object with a 'type' field. Valid types and their required fields:\n"
    '  modify     — {"type": "modify", "target_text": "...", "new_text": "..."}'
    " (optional: comment, match_mode, regex)\n"
    '  accept     — {"type": "accept", "target_id": "Chg:N"}\n'
    '  reject     — {"type": "reject", "target_id": "Chg:N"}\n'
    '  reply      — {"type": "reply", "target_id": "Com:N", "text": "..."}\n'
    '  insert_row — {"type": "insert_row", "target_text": "...", "cells": ["...", "..."]}'
    ' (optional: position "above"/"below")\n'
    '  delete_row — {"type": "delete_row", "target_text": "..."}'
)


def _format_batch_validation_error(exc: "ValidationError") -> str:
    """
    Renders a Pydantic ValidationError on the changes batch as a plain-language
    message. The raw dump leaks discriminated-union internals and a pydantic.dev
    URL without ever naming the valid 'type' values (QA M5).
    """
    lines: List[str] = []
    seen: set = set()
    for err in exc.errors():
        loc = err.get("loc", ())
        item_no = f"Change #{loc[0] + 1}" if loc and isinstance(loc[0], int) else "The batch"
        err_type = err.get("type", "")
        if err_type == "union_tag_not_found":
            msg = f"{item_no} is missing the required 'type' field."
        elif err_type == "union_tag_invalid":
            tag = err.get("ctx", {}).get("tag", "unknown")
            msg = f"{item_no} has an unknown type: '{tag}'."
        elif err_type == "missing":
            # loc is (index, variant_tag, field_name)
            variant = f" (type '{loc[1]}')" if len(loc) >= 2 else ""
            field = loc[-1] if len(loc) >= 3 else "a required field"
            msg = f"{item_no}{variant} is missing required field '{field}'."
        elif err_type == "list_type" and not loc:
            msg = "The JSON root must be a list of change objects."
        else:
            where = ".".join(str(p) for p in loc[1:]) if len(loc) > 1 else ""
            detail = err.get("msg", "is invalid")
            msg = f"{item_no}{f' field {where!r}' if where else ''}: {detail}."
        if msg not in seen:
            seen.add(msg)
            lines.append(f"  - {msg}")
    return "The changes file is not a valid edit batch:\n" + "\n".join(lines) + "\n\n" + _CHANGE_TYPE_REFERENCE


def _load_batch_from_json(path: Path) -> List[DocumentChange]:
    """
    Loads a batch of changes from a JSON file in the unified
    List[DocumentChange] format — the same shape the MCP `changes` parameter
    takes. A dict root carrying 'actions'/'edits' keys is the pre-v1.1.0
    batch shape; it gets a targeted migration error rather than a guess
    (QA 2026-07-17 F2).
    """
    try:
        data = json.loads(_read_text_file(path))

        if isinstance(data, dict) and ("actions" in data or "edits" in data):
            raise ValueError(
                'this file uses the removed pre-v1.1.0 {"actions": [...], "edits": [...]} format. '
                "Provide a flat JSON list of typed changes instead — rename 'action' to 'type', "
                "'original' to 'target_text', 'replace' to 'new_text', and merge both arrays "
                "into one list.\n\n" + _CHANGE_TYPE_REFERENCE
            )
        if not isinstance(data, list):
            raise ValueError("JSON root must be a list of change objects.\n\n" + _CHANGE_TYPE_REFERENCE)

        # BatchChanges (not the bare list) so the CLI tolerates the same LLM
        # quirks the MCP server does: stringified items, inferable missing
        # 'type', malformed match_mode.
        adapter = TypeAdapter(BatchChanges)
        return adapter.validate_python(data)
    except SystemExit:
        raise
    except ValidationError as e:
        _cli_error("invalid_changes_file", _format_batch_validation_error(e))
        raise AssertionError("unreachable") from None
    except Exception as e:
        _cli_error("invalid_changes_file", f"Error parsing JSON batch: {e}")
        raise AssertionError("unreachable") from None


def _warn_ignored_extract_flags(args) -> None:
    """
    Flags that only apply to one extract mode are warned about (never silently
    dropped) when combined with a mode that ignores them (QA 2026-07-18 L2).
    """
    in_search = bool(getattr(args, "search_query", None))
    if in_search and args.mode != "full":
        print(
            f"⚠️  --search-query takes precedence over --mode {args.mode}: "
            "running search mode; the outline/appendix view is not produced.",
            file=sys.stderr,
        )
        return
    if args.mode == "outline" and args.page is not None:
        print(
            "⚠️  --page is ignored with --mode outline (the outline always covers the whole document).",
            file=sys.stderr,
        )
    if args.mode != "outline":
        if args.outline_verbose:
            print(f"⚠️  --outline-verbose is ignored with --mode {args.mode} (outline mode only).", file=sys.stderr)
        # argparse default is 2; only warn when the user explicitly set it.
        if args.outline_max_level != 2:
            print(f"⚠️  --outline-max-level is ignored with --mode {args.mode} (outline mode only).", file=sys.stderr)


def handle_extract(args):
    _set_json_mode(args.json)
    _warn_ignored_extract_flags(args)
    if args.live:
        if sys.platform != "win32":
            _cli_error("unsupported", "--live is only supported on Windows.")
        from adeu.mcp_components.tools.live_word import _read_active_word_document_core

        text, doc, paragraph_offsets = _read_active_word_document_core(clean_view=args.clean_view)
    else:
        if not args.input:
            _cli_error("invalid_input", "Must provide input file or use --live")

        doc = _load_docx_or_exit(args.input)

        # Perform extraction
        needs_appendix = args.mode == "appendix"
        needs_offsets = args.mode == "outline"

        from adeu.ingest import _extract_text_from_doc

        extract_result = _extract_text_from_doc(
            doc,
            clean_view=args.clean_view,
            include_appendix=needs_appendix,
            return_paragraph_offsets=needs_offsets,
        )
        if needs_offsets:
            text, paragraph_offsets = extract_result
        else:
            text = extract_result
            paragraph_offsets = None

    from adeu.mcp_components._response_builders import (
        build_appendix_response,
        build_outline_response,
        build_paginated_response,
        build_search_response,
    )

    try:
        # In search mode, `page` supports 'all' and is validated (with clear
        # errors) inside build_search_response. In full mode, 'all' returns
        # the entire document without page chrome — the round-trip artifact
        # for text-based apply/diff (QA 2026-07-17 F1). For the remaining
        # modes it must be a positive integer; anything else is a hard error,
        # never a silent fallback to page 1 (QA L1).
        page_num = 1
        want_all_pages = False
        if args.page is not None and not getattr(args, "search_query", None):
            page_str = str(args.page).strip()
            if page_str.lower() == "all" and args.mode == "full":
                want_all_pages = True
            else:
                try:
                    page_num = int(page_str)
                except ValueError:
                    print(
                        f"❌ Invalid --page value: '{args.page}'. Provide a positive integer "
                        "(pages are 1-indexed; 'all' is valid for --mode full and --search-query).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if page_num < 1:
                    print(
                        f"❌ Invalid --page value: {page_num}. Pages are 1-indexed positive integers "
                        "(negative page numbers are not supported).",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        if getattr(args, "search_query", None):
            res = build_search_response(
                text,
                args.search_query,
                getattr(args, "search_regex", False),
                not getattr(args, "search_case_insensitive", False),
                args.page,
                "Active Document" if args.live else str(args.input),
                is_cli=True,
            )
        elif args.mode == "outline":
            res = build_outline_response(
                doc,
                text,
                "Active Document" if args.live else str(args.input),
                outline_max_level=args.outline_max_level,
                outline_verbose=args.outline_verbose,
                paragraph_offsets=paragraph_offsets,
                is_cli=True,
            )
        elif args.mode == "appendix":
            res = build_appendix_response(
                text,
                page_num,
                "Active Document" if args.live else str(args.input),
                is_cli=True,
            )
        elif want_all_pages:
            from adeu.mcp_components._response_builders import build_full_document_response

            res = build_full_document_response(
                text,
                "Active Document" if args.live else str(args.input),
            )
        else:
            res = build_paginated_response(
                text,
                page_num,
                "Active Document" if args.live else str(args.input),
                is_cli=True,
            )

        if isinstance(res.content, list):
            output_text = "\n".join(item.text if hasattr(item, "text") else str(item) for item in res.content)
        else:
            output_text = str(res.content)
    except SystemExit:
        raise
    except Exception as e:
        _cli_error("invalid_input", f"Error: {e}")
        raise AssertionError("unreachable") from None

    json_output = json.dumps(res.structured_content or {}) if args.json else None

    if args.output:
        # -o redirects the PRIMARY payload: the JSON object under --json, the
        # extracted text otherwise. stdout stays quiet either way — printing
        # the full payload again surprised pipeline users (QA 2026-07-18 L4).
        _write_output_or_exit(args.output, json_output if json_output is not None else output_text)
        print(f"Extracted {'JSON' if json_output is not None else 'text'} to {args.output}", file=sys.stderr)
    elif json_output is not None:
        print(json_output)
    else:
        print(output_text)


def _load_docx_or_exit(path: Path):
    """Loads a python-docx Document from `path` with the shared error handling."""
    _require_input_file(path)
    # Content beats extension: a valid ZIP is loaded whatever the filename
    # says (temp files, extension-less artifacts). The extension only shapes
    # the error message when the content is NOT a DOCX package (QA L3: a
    # .txt input deserves "must be a DOCX file", not a zip-signature error).
    try:
        with open(path, "rb") as _fh:
            _magic = _fh.read(4)
    except OSError as e:
        _cli_error("invalid_input", f"Could not read '{path.name}': {e.strerror or e}")
        raise AssertionError("unreachable") from None
    if _magic != b"PK\x03\x04" and path.suffix.lower() != ".docx":
        _cli_error(
            "invalid_docx",
            f"'{path.name}' must be a DOCX file (got {path.suffix or 'no extension'}).",
        )
    import zipfile

    try:
        with open(path, "rb") as f:
            stream = BytesIO(f.read())
        from adeu.utils.docx import strip_bom_from_docx_bytes

        sanitized_bytes = strip_bom_from_docx_bytes(stream.getvalue())
        from docx import Document as load_document

        return load_document(BytesIO(sanitized_bytes))
    except Exception as e:
        if (
            "bad zip signature" in str(e)
            or "not a zip file" in str(e).lower()
            or "not a valid DOCX file" in str(e)
            or isinstance(e, zipfile.BadZipFile)
        ):
            _handle_docx_error_and_exit(path.name, e)
        raise


def _open_redline_engine_or_exit(path: Path, author: "str | None" = None) -> RedlineEngine:
    """Opens a RedlineEngine on `path` through the single shared error path."""
    _require_input_file(path)
    import zipfile

    try:
        with open(path, "rb") as f:
            stream = BytesIO(f.read())
        if author is not None:
            return RedlineEngine(stream, author=author)
        return RedlineEngine(stream)
    except SystemExit:
        raise
    except Exception as e:
        if (
            "bad zip signature" in str(e)
            or "not a zip file" in str(e).lower()
            or "not a valid DOCX file" in str(e)
            or isinstance(e, zipfile.BadZipFile)
        ):
            _handle_docx_error_and_exit(path.name, e)
        raise


def handle_diff(args):
    _set_json_mode(args.json)
    from adeu.diff import DiffEdit, make_edits_self_contained

    edits: "Sequence[DiffEdit]"
    if args.modified.suffix.lower() == ".docx":
        compare_clean = getattr(args, "compare_clean", True)
        from adeu.ingest import _extract_text_from_doc

        # Appendix always excluded: its generated text ("used N times",
        # diagnostics) is not writable document content, and diffing it emits
        # edits apply can never resolve (QA 2026-07-18 H1). Extraction is
        # structure-aware so the diff can compare part-by-part (QA C1) and
        # emit structured table-row operations (QA C2).
        try:
            doc_orig = _load_docx_or_exit(args.original)
            text_orig, struct_orig = _extract_text_from_doc(
                doc_orig, clean_view=compare_clean, include_appendix=False, return_structure=True
            )
            doc_mod = _load_docx_or_exit(args.modified)
            text_mod, struct_mod = _extract_text_from_doc(
                doc_mod, clean_view=compare_clean, include_appendix=False, return_structure=True
            )
        except SystemExit:
            raise
        except Exception as e:
            # Projection failures (corrupt comments/notes/numbering parts)
            # must honor the CLI error contract, not dump a traceback (QA M8).
            _cli_error("invalid_docx", f"Could not extract text for comparison: {e}")
            raise AssertionError("unreachable") from None

        from adeu.diff import generate_structured_edits

        edits, diff_warnings = generate_structured_edits(text_orig, struct_orig, text_mod, struct_mod)
        for warning in diff_warnings:
            print(f"⚠️  {warning}", file=sys.stderr)
    else:
        doc = _load_docx_or_exit(args.original)

        from adeu.ingest import _extract_text_from_doc

        text_orig = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)

        text_mod = _load_roundtrip_text(args.modified, args.original, "diff")

        from adeu.diff import generate_edits_via_paragraph_alignment

        text_edits = generate_edits_via_paragraph_alignment(text_orig, text_mod)
        # diff output must be re-appliable by text matching alone: JSON
        # consumers never see the private position index, so widen
        # ambiguous/pure-insertion edits with surrounding context until each
        # target is unique (QA C1).
        edits = make_edits_self_contained(text_edits, text_orig)

    if args.json:
        output = [edit.model_dump(exclude={"_match_start_index"}) for edit in edits]
        print(json.dumps(output, indent=2))
    else:
        print(f"Found {len(edits)} changes:", file=sys.stderr)
        for edit in edits:
            if isinstance(edit, InsertTableRow):
                print(f"[+row] {' | '.join(edit.cells)} ({edit.position} '{edit.target_text[:40]}')")
            elif isinstance(edit, DeleteTableRow):
                print(f"[-row] {edit.target_text}")
            elif not edit.new_text:
                print(f"[-] {edit.target_text}")
            elif not edit.target_text:
                print(f"[+] {edit.new_text}")
            else:
                print(f"[~] '{edit.target_text}' -> '{edit.new_text}'")


def handle_apply(args):
    _set_json_mode(args.json)
    # Author flows into w:author attributes; XML-illegal control characters
    # would otherwise surface as a raw lxml traceback mid-apply (QA F11).
    from adeu.redline.engine import describe_illegal_control_chars

    author_ctrl = describe_illegal_control_chars(args.author or "")
    if author_ctrl:
        _cli_error(
            "invalid_input",
            f"--author contains control character(s) ({author_ctrl}) that cannot be "
            "stored in a DOCX. Remove them and re-run.",
        )

    if args.live:
        if args.changes is None and args.original is not None:
            # Shift positional arguments if only one is provided
            args.changes = args.original
            args.original = None

    if args.live and args.dry_run:
        _cli_error("unsupported", "Dry-run simulation is only supported for disk-based files.")

    if not args.changes:
        _cli_error("invalid_input", "Must provide changes file.")

    changes: List[DocumentChange] = []

    if args.changes.suffix.lower() == ".json":
        if not args.json:
            print(f"Loading structured batch from {args.changes}...", file=sys.stderr)
        changes = _load_batch_from_json(args.changes)
        if not changes:
            print(
                f"⚠️  '{args.changes.name}' contains 0 changes — nothing to do. "
                "The output will be an unmodified copy of the original.",
                file=sys.stderr,
            )
    else:
        if not args.json:
            print(f"Calculating diff from text file {args.changes}...", file=sys.stderr)
        if args.live:
            if sys.platform != "win32":
                _cli_error("unsupported", "--live is only supported on Windows.")
            from adeu.mcp_components.tools.live_word import (
                _read_active_word_document_core,
            )

            text_orig, _, _ = _read_active_word_document_core(clean_view=False)
        else:
            if not args.original:
                _cli_error("invalid_input", "Must provide original file if not using --live")
            doc = _load_docx_or_exit(args.original)

            from adeu.ingest import _extract_text_from_doc

            # Canonical baseline for text-file input: the CLEAN (accepted)
            # view. Extract with --clean-view (and --page all on multi-page
            # documents) to produce a file this path can round-trip.
            text_orig = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)

            text_mod = _load_roundtrip_text(args.changes, args.original, "apply")

            if (
                not args.allow_major_deletions
                and len(text_orig) >= _MAJOR_DELETION_MIN_ORIGINAL_CHARS
                and len(text_mod) < _MAJOR_DELETION_RATIO * len(text_orig)
            ):
                pct = 100 - int(100 * len(text_mod) / len(text_orig))
                print(
                    f"❌ '{args.changes.name}' is ~{pct}% shorter than the document's clean text "
                    f"({len(text_mod):,} vs {len(text_orig):,} characters). Applying it would "
                    "delete the majority of the document as tracked deletions.\n"
                    "   If the file is a partial extract, re-extract the ENTIRE document with "
                    "`--page all --clean-view` and edit that.\n"
                    "   If the mass deletion is intentional, re-run with --allow-major-deletions.",
                    file=sys.stderr,
                )
                sys.exit(1)

            from adeu.diff import generate_edits_via_paragraph_alignment

            changes.extend(generate_edits_via_paragraph_alignment(text_orig, text_mod))

    if args.live:
        if sys.platform != "win32":
            _cli_error("unsupported", "--live is only supported on Windows.")
        from adeu.mcp_components.tools.live_word import _process_active_word_batch_core

        if not args.json:
            print(f"Applying {len(changes)} changes to live Word document...", file=sys.stderr)
        stats = _process_active_word_batch_core(changes, args.author)
        if args.json:
            print(json.dumps(stats))
        else:
            print(
                f"✅ Live Word Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}",
                file=sys.stderr,
            )
        if stats["failed"] > 0:
            sys.exit(1)
        return

    if not args.original:
        _cli_error("invalid_input", "Must provide original file if not using --live")
    _require_input_file(args.original)

    if not args.json:
        print(f"Applying {len(changes)} changes to {args.original.name}...", file=sys.stderr)
    engine = _open_redline_engine_or_exit(args.original, author=args.author)
    try:
        stats = engine.process_batch(changes, dry_run=args.dry_run)
    except BatchValidationError as e:
        if args.json:
            print(json.dumps({"error": "batch_validation_failed", "errors": e.errors}))
        else:
            print(
                f"\n❌ Batch rejected. {len(e.errors)} edits failed validation:\n",
                file=sys.stderr,
            )
            for err in e.errors:
                print(err, file=sys.stderr)
                print("", file=sys.stderr)
        sys.exit(1)

    # A batch with ANY skipped action/edit is a failed batch: writing an
    # output anyway (and calling it "Batch complete") made pipelines treat an
    # unmodified copy as success (QA 2026-07-18 M2). Validation failures are
    # already transactional (BatchValidationError above); this covers
    # apply-stage skips (overlaps, unresolvable anchors).
    batch_failed = stats["actions_skipped"] > 0 or stats["edits_skipped"] > 0

    output_path = None
    if not args.dry_run and not batch_failed:
        output_path = args.output
        if not output_path:
            if args.original.stem.endswith("_redlined") or args.original.stem.endswith("_processed"):
                output_path = args.original
            else:
                output_path = args.original.with_name(f"{args.original.stem}_redlined.docx")

        _write_output_or_exit(output_path, engine.save_to_stream().getvalue())

    stats["dry_run"] = args.dry_run
    stats["output_path"] = str(output_path) if output_path else None

    if args.json:
        print(json.dumps(stats))
    else:
        if batch_failed and not args.dry_run:
            print(
                "❌ Batch failed — no output was written. Fix the failed edits below and re-run.",
                file=sys.stderr,
            )
        elif not args.dry_run:
            print(f"Batch complete. Saved to: {output_path}", file=sys.stderr)
        else:
            print("Dry-run simulation complete.", file=sys.stderr)

        print(
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.\n"
            f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped.",
            file=sys.stderr,
        )

        if stats.get("edits"):
            print("\nDetailed Edit Reports:", file=sys.stderr)
            for i, report in enumerate(stats["edits"]):
                status_indicator = "✅ [applied]" if report["status"] == "applied" else "❌ [failed]"
                print(f"Edit {i + 1} {status_indicator}:", file=sys.stderr)
                print(f"  Target: '{report['target_text']}'", file=sys.stderr)
                print(f"  New text: '{report['new_text']}'", file=sys.stderr)
                if report.get("warning"):
                    print(f"  Warning: {report['warning']}", file=sys.stderr)
                if report.get("error"):
                    print(f"  Error: {report['error']}", file=sys.stderr)
                if report.get("critic_markup"):
                    print(
                        f"  Preview (CriticMarkup): {report['critic_markup']}",
                        file=sys.stderr,
                    )
                if report.get("clean_text"):
                    print(f"  Clean text preview: {report['clean_text']}", file=sys.stderr)

        if stats.get("skipped_details"):
            print("\nSkipped Details:", file=sys.stderr)
            for detail in stats["skipped_details"]:
                print(detail, file=sys.stderr)

    if stats["actions_skipped"] > 0 or stats["edits_skipped"] > 0:
        sys.exit(1)


def handle_accept_all(args: argparse.Namespace):
    """
    Accepts all tracked changes and removes all comments, producing a
    finalized clean document. Mirrors the `accept_all_changes` MCP tool.
    """
    _set_json_mode(args.json)
    engine = _open_redline_engine_or_exit(args.input)

    engine.accept_all_revisions(remove_comments=True)

    output_path = args.output
    if not output_path:
        output_path = args.input.with_name(f"{args.input.stem}_clean{args.input.suffix}")

    _write_output_or_exit(output_path, engine.save_to_stream().getvalue())

    if args.json:
        print(json.dumps({"status": "ok", "output_path": str(output_path)}))
    else:
        print(f"✅ Accepted all changes. Saved to: {output_path}", file=sys.stderr)


def handle_markup(args):
    """Handler for the 'markup' subcommand."""
    if args.input.suffix.lower() == ".docx":
        text = _read_docx_text(args.input)
    else:
        text = _read_text_file(args.input)

    if not args.edits.exists():
        print(f"Error: Edits file not found: {args.edits}", file=sys.stderr)
        sys.exit(1)

    changes = _load_batch_from_json(args.edits)
    edits = [c for c in changes if isinstance(c, ModifyText)]
    non_text = [c for c in changes if not isinstance(c, ModifyText)]

    if non_text:
        # markup is a text-preview tool; review/table actions have no textual
        # rendering here — but they must never vanish silently (QA 2026-07-18 M1).
        type_counts: Dict[str, int] = {}
        for c in non_text:
            type_counts[c.type] = type_counts.get(c.type, 0) + 1
        summary = ", ".join(f"{count}× {name}" for name, count in sorted(type_counts.items()))
        print(
            f"⚠️  {len(non_text)} non-text change(s) ignored by markup ({summary}). "
            "markup only previews 'modify' text edits — run these through `adeu apply`.",
            file=sys.stderr,
        )

    if not edits:
        print("Warning: No text edits found in JSON file.", file=sys.stderr)

    # Same string-shape validation `apply` enforces. Without it, new_text
    # containing raw CriticMarkup tags ({++..++}, {>>..<<}) passes straight
    # into the rendered output, where a downstream CriticMarkup consumer would
    # parse user data as structural markup (QA L3).
    shape_errors = validate_edit_strings(list(edits))
    if shape_errors:
        print(f"❌ {len(shape_errors)} edit(s) failed validation:\n", file=sys.stderr)
        for err in shape_errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    edit_reports: List[Dict[str, Any]] = []
    result = apply_edits_to_markdown(
        markdown_text=text,
        edits=edits,
        include_index=args.index,
        highlight_only=args.highlight,
        edit_reports=edit_reports,
    )

    failed = [r for r in edit_reports if r["status"] == "failed"]
    applied = [r for r in edit_reports if r["status"] == "applied"]

    if failed:
        # Mirror apply's transactional behavior: a preview of half the batch
        # is not a faithful preview (QA 2026-07-18 M1).
        print(f"\n❌ {len(failed)} edit(s) failed — no markup was written:\n", file=sys.stderr)
        for r in failed:
            print(r["error"], file=sys.stderr)
            print("", file=sys.stderr)
        print(
            f"Stats: {len(applied)} applied, {len(failed)} failed"
            + (f", {len(non_text)} non-text actions ignored" if non_text else "")
            + ".",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path = args.output
    if not output_path:
        output_path = args.input.with_suffix(".md")
        if args.input.suffix.lower() == ".md":
            output_path = args.input.with_name(f"{args.input.stem}_markup.md")

    _write_output_or_exit(output_path, result)

    print(f"✅ Saved CriticMarkup to {output_path}", file=sys.stderr)
    print(
        f"Stats: {len(applied)} applied, 0 failed"
        + (f", {len(non_text)} non-text actions ignored" if non_text else "")
        + ".",
        file=sys.stderr,
    )


def handle_sanitize(args: argparse.Namespace):
    from adeu.redline.engine import describe_illegal_control_chars

    author_ctrl = describe_illegal_control_chars(args.author or "")
    if author_ctrl:
        print(
            f"❌ --author contains control character(s) ({author_ctrl}) that cannot be "
            "stored in a DOCX. Remove them and re-run.",
            file=sys.stderr,
        )
        sys.exit(2)

    input_files: List[Path] = args.input
    is_batch = len(input_files) > 1 or args.outdir

    if not is_batch and args.baseline and len(input_files) > 1:
        print("❌ --baseline only works with a single input file.", file=sys.stderr)
        sys.exit(2)

    if not is_batch and len(input_files) == 1:
        # Single file mode
        input_path = input_files[0]
        if not input_path.exists():
            print(f"❌ File not found: {input_path}", file=sys.stderr)
            sys.exit(2)

        output_path = args.output
        if not output_path:
            output_path = input_path.parent / f"{input_path.stem}_sanitized{input_path.suffix}"

        try:
            result = sanitize_docx(
                input_path=str(input_path),
                output_path=str(output_path),
                keep_markup=args.keep_markup,
                baseline_path=str(args.baseline) if args.baseline else None,
                author=args.author,
                accept_all=args.accept_all,
            )
            if args.report or args.report_file:
                if args.report:
                    print(result.report_text, file=sys.stderr)
                if args.report_file:
                    with open(args.report_file, "w", encoding="utf-8") as f:
                        f.write(result.report_text)
                    print(f"📄 Report saved to {args.report_file}", file=sys.stderr)

            print(f"✅ Sanitized → {output_path}", file=sys.stderr)

        except SanitizeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        except FileNotFoundError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            if "bad zip signature" in str(e) or "not a zip file" in str(e).lower() or "not a valid DOCX file" in str(e):
                _handle_docx_error_and_exit(input_path.name, e)
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        # Batch mode
        outdir = args.outdir
        if not outdir:
            print("❌ Batch mode requires --outdir.", file=sys.stderr)
            sys.exit(2)

        # Destination collision check BEFORE any processing: two inputs with
        # the same basename would silently overwrite each other in --outdir
        # while the summary counts both as successes (QA 2026-07-18 H2).
        dest_map: Dict[str, List[Path]] = {}
        for input_path in input_files:
            dest_map.setdefault(input_path.name, []).append(input_path)
        collisions = {name: paths for name, paths in dest_map.items() if len(paths) > 1}
        if collisions:
            print(
                "❌ Output filename collision — refusing to overwrite results silently:",
                file=sys.stderr,
            )
            for name, paths in collisions.items():
                sources = ", ".join(str(p) for p in paths)
                print(f"   {outdir / name}  would collide from: {sources}", file=sys.stderr)
            print(
                "   Rename the inputs, run them in separate batches, or use distinct --outdir targets.",
                file=sys.stderr,
            )
            sys.exit(2)

        outdir.mkdir(parents=True, exist_ok=True)

        all_reports: list[SanitizeResult | SanitizeError] = []
        blocked = 0
        succeeded = 0

        for input_path in input_files:
            if not input_path.exists():
                print(f"❌ File not found: {input_path}", file=sys.stderr)
                blocked += 1
                continue

            output_path = outdir / input_path.name

            # Resolve baseline for batch mode
            baseline = None
            if args.baseline:
                if args.baseline.is_dir():
                    baseline = str(args.baseline / input_path.name)
                else:
                    baseline = str(args.baseline)

            try:
                result = sanitize_docx(
                    input_path=str(input_path),
                    output_path=str(output_path),
                    keep_markup=args.keep_markup,
                    baseline_path=baseline,
                    author=args.author,
                    accept_all=args.accept_all,
                )
                all_reports.append(result)
                succeeded += 1
                status = "clean"
                if result.warnings:
                    status = f"clean ({len(result.warnings)} warning{'s' if len(result.warnings) > 1 else ''})"
                print(f"  ✓ {input_path.name:<30} — {status}", file=sys.stderr)

            except SanitizeError as e:
                blocked += 1
                print(f"  ✗ {input_path.name:<30} — BLOCKED", file=sys.stderr)
                all_reports.append(e)

            except Exception as e:
                blocked += 1
                if (
                    "bad zip signature" in str(e)
                    or "not a zip file" in str(e).lower()
                    or "not a valid DOCX file" in str(e)
                ):
                    _handle_docx_error_and_exit(input_path.name, e)
                print(f"  ✗ {input_path.name:<30} — ERROR: {e}", file=sys.stderr)

        # Batch summary
        total = succeeded + blocked
        summary = f"\nBatch Summary: {total} documents processed, {succeeded} succeeded, {blocked} blocked"
        print(summary, file=sys.stderr)

        # Write reports
        if args.report or args.report_file:
            full_report = []
            for r in all_reports:
                if isinstance(r, SanitizeError):
                    full_report.append(str(r))
                else:
                    full_report.append(r.report_text)
                full_report.append("")

            full_report.append(summary)
            report_text = "\n".join(full_report)

            if args.report:
                print(report_text, file=sys.stderr)
            if args.report_file:
                with open(args.report_file, "w", encoding="utf-8") as f:
                    f.write(report_text)

        if blocked > 0:
            sys.exit(1)


def main():
    # Must run before anything prints and before structlog captures stderr:
    # forces deterministic UTF-8 output and picks emoji-vs-ASCII glyphs.
    configure_cli_streams()

    _version, _sha, _ = get_build_info()
    _ver_str = f"{_version}+{_sha}" if _sha and _sha != "unknown" else _version
    parser = argparse.ArgumentParser(
        prog="adeu",
        description=f"Adeu: Agentic DOCX Redlining Engine (version {_ver_str})",
        epilog=(
            "Track Changes for the LLM era -- LLMs speak Markdown; reviewers speak Track Changes.\n"
            "Built and maintained by the team at Adeu (https://adeu.ai).\n"
            "Docs, MCP server & agent skills: https://github.com/dealfluence/adeu"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_ver_str}")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    p_extract = subparsers.add_parser("extract", help="Extract raw text from a DOCX file")
    p_extract.add_argument("input", type=Path, nargs="?", help="Input DOCX file (omit if --live)")
    p_extract.add_argument(
        "--live",
        action="store_true",
        help="Extract text from live active Word document",
    )
    p_extract.add_argument("-o", "--output", type=Path, help="Output file (default: stdout)")
    p_extract.add_argument(
        "--clean-view",
        action="store_true",
        help="If specified, returns the 'Accepted' text without track changes and comments.",
    )
    p_extract.add_argument(
        "--mode",
        type=str,
        choices=["full", "outline", "appendix"],
        default="full",
        help="Extraction mode: 'full' for body text, 'outline' for headings, 'appendix' for defined terms.",
    )
    p_extract.add_argument(
        "--page",
        type=str,
        default=None,
        help=(
            "Page number (1-indexed) for 'full' and 'appendix' modes (defaults to 1), "
            "or 'all' to emit the entire document in one output (mode 'full' and search). "
            "Use '--page all' when producing a text file for `adeu apply`/`adeu diff` — "
            "a single page of a multi-page document cannot round-trip. Note: pages are "
            "synthetic, length-based content chunks sized for LLM consumption — they do "
            "NOT correspond to printed Word pages or explicit page breaks."
        ),
    )
    p_extract.add_argument("--search-query", type=str, help="The substring or regex pattern to search for.")
    p_extract.add_argument(
        "--search-regex",
        action="store_true",
        help="Set to true to interpret search_query as a regular expression.",
    )
    p_extract.add_argument(
        "--search-case-insensitive",
        action="store_true",
        help="Perform case-insensitive matching.",
    )

    def _outline_level(value: str) -> int:
        level = int(value)
        if not 1 <= level <= 6:
            raise argparse.ArgumentTypeError(f"must be between 1 and 6 (got {value})")
        return level

    p_extract.add_argument(
        "--outline-max-level",
        type=_outline_level,
        default=2,
        help="For mode='outline' only: maximum heading depth to show (1-6).",
    )
    p_extract.add_argument(
        "--outline-verbose",
        action="store_true",
        help="For mode='outline' only: include heading metadata.",
    )
    p_extract.add_argument(
        "--json",
        action="store_true",
        help="Emit the extraction result as a machine-readable JSON object on stdout.",
    )
    p_extract.set_defaults(func=handle_extract)

    p_init = subparsers.add_parser("init", help="Auto-configure Adeu for Claude Desktop")
    p_init.add_argument(
        "--local",
        action="store_true",
        help="Configure to run from current source (for dev/testing)",
    )
    p_init.add_argument(
        "--scope",
        choices=["all", "docx"],
        default="all",
        help="Limit exposed tools to local manipulation ('docx') or everything ('all').",
    )
    p_init.set_defaults(func=handle_init)

    p_diff = subparsers.add_parser("diff", help="Compare two files (DOCX vs DOCX/Text)")
    p_diff.add_argument("original", type=Path, help="Original DOCX")
    p_diff.add_argument("modified", type=Path, help="Modified DOCX or Text file")
    p_diff.add_argument("--json", action="store_true", help="Output raw JSON edits")
    p_diff.add_argument(
        "--compare-clean",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Compare clean/accepted views of documents instead of raw views "
            "including existing track changes/markup (default: True)"
        ),
    )
    p_diff.set_defaults(func=handle_diff)

    try:
        default_author = getpass.getuser()
    except Exception:
        default_author = "Adeu AI"

    p_apply = subparsers.add_parser("apply", help="Apply edits to a DOCX")
    p_apply.add_argument("original", type=Path, nargs="?", help="Original DOCX (omit if --live)")
    p_apply.add_argument("changes", type=Path, nargs="?", help="JSON edits file OR Modified Text file")
    p_apply.add_argument("--live", action="store_true", help="Apply edits to live active Word document")
    p_apply.add_argument("-o", "--output", type=Path, help="Output DOCX path")
    p_apply.add_argument(
        "--author",
        type=str,
        default=default_author,
        help=f"Author name for Track Changes (default: '{default_author}')",
    )
    p_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the changes and return a detailed preview report without modifying any files.",
    )
    p_apply.add_argument(
        "--allow-major-deletions",
        action="store_true",
        help=(
            "Text-file apply only: allow the supplied text to be less than half the length of the "
            "document's clean text. Without this flag such an apply is refused, because a truncated "
            "input (e.g. a single page of a paginated extract) would silently delete everything "
            "it does not contain."
        ),
    )
    p_apply.add_argument(
        "--json",
        action="store_true",
        help="Emit the batch result stats as machine-readable JSON on stdout, suppressing human-readable logs.",
    )
    p_apply.set_defaults(func=handle_apply)

    p_accept = subparsers.add_parser(
        "accept-all",
        help="Accept all tracked changes and remove all comments (finalize a document)",
    )
    p_accept.add_argument("input", type=Path, help="Input DOCX file")
    p_accept.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output DOCX path (default: <input>_clean.docx)",
    )
    p_accept.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result on stdout.",
    )
    p_accept.set_defaults(func=handle_accept_all)

    p_markup = subparsers.add_parser(
        "markup",
        help="Apply edits to a document and output as CriticMarkup Markdown",
    )
    p_markup.add_argument("input", type=Path, help="Input DOCX or Markdown file")
    p_markup.add_argument("edits", type=Path, help="JSON file containing edits")
    p_markup.add_argument("-o", "--output", type=Path, help="Output Markdown path (default: input.md)")
    p_markup.add_argument(
        "-i",
        "--index",
        action="store_true",
        help="Include edit indices [Edit:N] in the output",
    )
    p_markup.add_argument(
        "--highlight",
        action="store_true",
        help="Highlight-only mode: mark targets with {==...==} without applying changes",
    )
    p_markup.set_defaults(func=handle_markup)

    p_sanitize = subparsers.add_parser(
        "sanitize",
        help="Strip metadata and sensitive information from a DOCX file",
    )
    p_sanitize.add_argument("input", type=Path, nargs="+", help="Input DOCX file(s)")
    p_sanitize.add_argument("-o", "--output", type=Path, help="Output DOCX path (single file mode)")
    p_sanitize.add_argument("--outdir", type=Path, help="Output directory (batch mode)")
    p_sanitize.add_argument(
        "--keep-markup",
        action="store_true",
        help="Keep existing track changes and open comments; strip everything else",
    )
    p_sanitize.add_argument(
        "--baseline",
        type=Path,
        help="Baseline document for delta recomputation",
    )
    p_sanitize.add_argument(
        "--author",
        type=str,
        help="Replace all author names with this value",
    )
    p_sanitize.add_argument(
        "--accept-all",
        action="store_true",
        help="Accept all unresolved track changes (full sanitize only)",
    )
    p_sanitize.add_argument(
        "--report",
        action="store_true",
        help="Print sanitization report to stderr",
    )
    p_sanitize.add_argument(
        "--report-file",
        type=Path,
        help="Write report to file",
    )
    p_sanitize.set_defaults(func=handle_sanitize)

    args, unknown_args = parser.parse_known_args()
    if unknown_args:
        # Route the error through the invoked subcommand's parser so the
        # usage hint matches the command actually run — argparse's default
        # reports these against the top-level parser (QA 2026-07-16 run 2, F2).
        invoked = subparsers.choices.get(getattr(args, "command", None) or "")
        (invoked or parser).error(f"unrecognized arguments: {' '.join(unknown_args)}")

    import logging

    import structlog

    log_level = logging.DEBUG if args.debug else logging.WARNING
    # stdout is reserved for document data / JSON results; all logging must
    # stay on stderr so `adeu extract doc.docx > out.md` stays clean. The
    # dynamic proxy (not sys.stderr itself) keeps this global config valid
    # even if the stderr object is replaced or closed after configure time.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=dynamic_stderr),  # type: ignore[arg-type]
    )

    _set_json_mode(bool(getattr(args, "json", False)))
    args.func(args)


if __name__ == "__main__":
    main()
