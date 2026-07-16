import argparse
import codecs
import datetime
import getpass
import json
import os
import platform
import shutil
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

from pydantic import TypeAdapter

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown
from adeu.mcp_components.shared import get_build_info
from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine
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
    if config_path.exists():
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.{timestamp}.bak")
        shutil.copy2(config_path, backup_path)
        print(f"📦 Backup created: {backup_path.name}", file=sys.stderr)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
        except json.JSONDecodeError:
            print("⚠️  Existing config was invalid JSON. Starting fresh.", file=sys.stderr)

    mcp_servers = data.setdefault("mcpServers", {})

    if args.local:
        cwd = Path.cwd().resolve()
        python_exe = sys.executable
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

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print("✅ Adeu successfully configured in Claude Desktop.", file=sys.stderr)
    print("   Please restart Claude to load the new toolset.", file=sys.stderr)


def _print_sandbox_warning_and_exit(path: Path, exit_code: int = 1):
    print(f"❌ File not found: {path}", file=sys.stderr)
    print(
        "Note: If you are running in a sandboxed/containerized environment, "
        "the host application or MCP server may not have access to your local workspace files. "
        "You can resolve this by installing Adeu directly inside your sandboxed environment using "
        "'uv tool install adeu' and executing the commands via the CLI.",
        file=sys.stderr,
    )
    sys.exit(exit_code)


def _handle_docx_error_and_exit(filename: str, exc: Exception) -> None:
    import re

    err_str = str(exc)
    reason = "got bad zip signature"
    if "not a valid DOCX file" in err_str:
        match = re.search(r"not a valid DOCX file \(([^)]+)\)", err_str)
        if match:
            reason = match.group(1)
    print(f"❌ Error: '{filename}' is not a valid DOCX file ({reason}).", file=sys.stderr)
    sys.exit(1)


def _read_docx_text(path: Path, clean_view: bool = False) -> str:
    if not path.exists():
        _print_sandbox_warning_and_exit(path)
    if path.suffix.lower() != ".docx":
        print(
            f"❌ Error: '{path.name}' must be a DOCX file (got {path.suffix or 'no extension'})",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with open(path, "rb") as f:
            header = f.read(4)
            if header != b"PK\x03\x04":
                print(
                    f"❌ Error: '{path.name}' is not a valid DOCX file (got bad zip signature).",
                    file=sys.stderr,
                )
                sys.exit(1)
            f.seek(0)
            return extract_text_from_stream(BytesIO(f.read()), filename=path.name, clean_view=clean_view)
    except Exception as e:
        if "bad zip signature" in str(e) or "not a zip file" in str(e).lower() or "not a valid DOCX file" in str(e):
            _handle_docx_error_and_exit(path.name, e)
        print(f"❌ Error reading DOCX file '{path.name}': {e}", file=sys.stderr)
        sys.exit(1)


def _read_text_file(path: Path) -> str:
    """Read a user-supplied text file with encoding tolerance.

    UTF-8 (with or without BOM) and BOM-marked UTF-16/32 decode silently;
    other content falls back to Windows-1252 with a loud warning — such files
    are typically produced by redirecting console output on a legacy Windows
    code page. Content with NUL bytes (BOM-less UTF-16, binaries) gets a
    guided error instead of flowing into edits as mojibake. Newlines are
    normalized to \\n, matching text-mode open().
    """
    if not path.exists():
        _print_sandbox_warning_and_exit(path)
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

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _load_batch_from_json(path: Path) -> List[DocumentChange]:
    """
    Loads a batch of actions and edits from a JSON file.
    Supports the unified List[DocumentChange] format or the legacy dict format.
    """
    try:
        data = json.loads(_read_text_file(path))

        # Legacy dict format support
        if isinstance(data, dict):
            changes_data = []
            for item in data.get("actions", []):
                action_val = item.pop("action", "").lower()
                item["type"] = action_val if action_val in ("accept", "reject", "reply") else "accept"
                changes_data.append(item)
            for item in data.get("edits", []):
                item["type"] = "modify"
                if "original" in item:
                    item["target_text"] = item.pop("original")
                if "replace" in item:
                    item["new_text"] = item.pop("replace")
                changes_data.append(item)
            data = changes_data
        elif not isinstance(data, list):
            raise ValueError("JSON root must be a list of changes or a legacy dict with 'actions' and 'edits'.")

        adapter = TypeAdapter(List[DocumentChange])
        return adapter.validate_python(data)
    except Exception as e:
        print(f"Error parsing JSON batch: {e}", file=sys.stderr)
        sys.exit(1)


def handle_extract(args):
    if args.live:
        if sys.platform != "win32":
            print("❌ --live is only supported on Windows.", file=sys.stderr)
            sys.exit(1)
        from adeu.mcp_components.tools.live_word import _read_active_word_document_core

        text, doc, paragraph_offsets = _read_active_word_document_core(clean_view=args.clean_view)
    else:
        if not args.input:
            print("❌ Must provide input file or use --live", file=sys.stderr)
            sys.exit(1)
        if not args.input.exists():
            _print_sandbox_warning_and_exit(args.input)

        import zipfile

        try:
            with open(args.input, "rb") as f:
                stream = BytesIO(f.read())
            from adeu.utils.docx import strip_bom_from_docx_bytes

            sanitized_bytes = strip_bom_from_docx_bytes(stream.getvalue())
            from docx import Document as load_document

            doc = load_document(BytesIO(sanitized_bytes))
        except Exception as e:
            if (
                "bad zip signature" in str(e)
                or "not a zip file" in str(e).lower()
                or "not a valid DOCX file" in str(e)
                or isinstance(e, zipfile.BadZipFile)
            ):
                _handle_docx_error_and_exit(args.input.name, e)
            raise

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
        page_val = args.page
        page_num = int(page_val) if page_val is not None and str(page_val).isdigit() else 1

        if getattr(args, "search_query", None):
            res = build_search_response(
                text,
                args.search_query,
                getattr(args, "search_regex", False),
                not getattr(args, "search_case_insensitive", False),
                args.page,
                "Active Document" if args.live else str(args.input),
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
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

    json_output = json.dumps(res.structured_content or {}) if args.json else None

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"Extracted text to {args.output}", file=sys.stderr)
        if json_output is not None:
            print(json_output)
    elif json_output is not None:
        print(json_output)
    else:
        print(output_text)


def handle_diff(args):
    if args.modified.suffix.lower() == ".docx":
        compare_clean = getattr(args, "compare_clean", True)
        text_orig = _read_docx_text(args.original, clean_view=compare_clean)
        text_mod = _read_docx_text(args.modified, clean_view=compare_clean)
        edits = generate_edits_from_text(text_orig, text_mod)
    else:
        if not args.original.exists():
            _print_sandbox_warning_and_exit(args.original)
        import zipfile

        try:
            with open(args.original, "rb") as f:
                stream = BytesIO(f.read())
            from adeu.utils.docx import strip_bom_from_docx_bytes

            sanitized_bytes = strip_bom_from_docx_bytes(stream.getvalue())
            from docx import Document as load_document

            doc = load_document(BytesIO(sanitized_bytes))
        except Exception as e:
            if (
                "bad zip signature" in str(e)
                or "not a zip file" in str(e).lower()
                or "not a valid DOCX file" in str(e)
                or isinstance(e, zipfile.BadZipFile)
            ):
                _handle_docx_error_and_exit(args.original.name, e)
            raise

        from adeu.ingest import _extract_text_from_doc

        text_orig = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)

        text_mod = _read_text_file(args.modified)

        from adeu.diff import generate_edits_via_paragraph_alignment

        edits = generate_edits_via_paragraph_alignment(text_orig, text_mod)

    if args.json:
        output = [edit.model_dump(exclude={"_match_start_index"}) for edit in edits]
        print(json.dumps(output, indent=2))
    else:
        print(f"Found {len(edits)} changes:", file=sys.stderr)
        for edit in edits:
            if not edit.new_text:
                print(f"[-] {edit.target_text}")
            elif not edit.target_text:
                print(f"[+] {edit.new_text}")
            else:
                print(f"[~] '{edit.target_text}' -> '{edit.new_text}'")


def handle_apply(args):
    if args.live:
        if args.changes is None and args.original is not None:
            # Shift positional arguments if only one is provided
            args.changes = args.original
            args.original = None

    if args.live and args.dry_run:
        print(
            "❌ Dry-run simulation is only supported for disk-based files.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.changes:
        print("❌ Must provide changes file.", file=sys.stderr)
        sys.exit(1)

    changes: List[DocumentChange] = []

    if args.changes.suffix.lower() == ".json":
        if not args.json:
            print(f"Loading structured batch from {args.changes}...", file=sys.stderr)
        changes = _load_batch_from_json(args.changes)
    else:
        if not args.json:
            print(f"Calculating diff from text file {args.changes}...", file=sys.stderr)
        if args.live:
            if sys.platform != "win32":
                print("❌ --live is only supported on Windows.", file=sys.stderr)
                sys.exit(1)
            from adeu.mcp_components.tools.live_word import (
                _read_active_word_document_core,
            )

            text_orig, _, _ = _read_active_word_document_core(clean_view=False)
        else:
            if not args.original:
                print("❌ Must provide original file if not using --live", file=sys.stderr)
                sys.exit(1)
            if not args.original.exists():
                _print_sandbox_warning_and_exit(args.original)
            import zipfile

            try:
                with open(args.original, "rb") as f:
                    stream = BytesIO(f.read())
                from adeu.utils.docx import strip_bom_from_docx_bytes

                sanitized_bytes = strip_bom_from_docx_bytes(stream.getvalue())
                from docx import Document as load_document

                doc = load_document(BytesIO(sanitized_bytes))
            except Exception as e:
                if (
                    "bad zip signature" in str(e)
                    or "not a zip file" in str(e).lower()
                    or "not a valid DOCX file" in str(e)
                    or isinstance(e, zipfile.BadZipFile)
                ):
                    _handle_docx_error_and_exit(args.original.name, e)
                raise

            from adeu.ingest import _extract_text_from_doc

            text_orig = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)

            text_mod = _read_text_file(args.changes)

            from adeu.diff import generate_edits_via_paragraph_alignment

            changes.extend(generate_edits_via_paragraph_alignment(text_orig, text_mod))

    if args.live:
        if sys.platform != "win32":
            print("❌ --live is only supported on Windows.", file=sys.stderr)
            sys.exit(1)
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
        print("❌ Must provide original file if not using --live", file=sys.stderr)
        sys.exit(1)

    import zipfile

    if not args.json:
        print(f"Applying {len(changes)} changes to {args.original.name}...", file=sys.stderr)
    try:
        with open(args.original, "rb") as f:
            stream = BytesIO(f.read())

        engine = RedlineEngine(stream, author=args.author)
    except Exception as e:
        if (
            "bad zip signature" in str(e)
            or "not a zip file" in str(e).lower()
            or "not a valid DOCX file" in str(e)
            or isinstance(e, zipfile.BadZipFile)
        ):
            _handle_docx_error_and_exit(args.original.name, e)
        raise
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

    output_path = None
    if not args.dry_run:
        output_path = args.output
        if not output_path:
            if args.original.stem.endswith("_redlined") or args.original.stem.endswith("_processed"):
                output_path = args.original
            else:
                output_path = args.original.with_name(f"{args.original.stem}_redlined.docx")

        with open(output_path, "wb") as f:
            f.write(engine.save_to_stream().getvalue())

    stats["dry_run"] = args.dry_run
    stats["output_path"] = str(output_path) if output_path else None

    if args.json:
        print(json.dumps(stats))
    else:
        if not args.dry_run:
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
    if not args.input.exists():
        _print_sandbox_warning_and_exit(args.input)

    import zipfile

    try:
        with open(args.input, "rb") as f:
            stream = BytesIO(f.read())
        engine = RedlineEngine(stream)
    except Exception as e:
        if (
            "bad zip signature" in str(e)
            or "not a zip file" in str(e).lower()
            or "not a valid DOCX file" in str(e)
            or isinstance(e, zipfile.BadZipFile)
        ):
            _handle_docx_error_and_exit(args.input.name, e)
        raise

    engine.accept_all_revisions(remove_comments=True)

    output_path = args.output
    if not output_path:
        output_path = args.input.with_name(f"{args.input.stem}_clean{args.input.suffix}")

    with open(output_path, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

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

    if not edits:
        print("Warning: No text edits found in JSON file.", file=sys.stderr)

    result = apply_edits_to_markdown(
        markdown_text=text,
        edits=edits,
        include_index=args.index,
        highlight_only=args.highlight,
    )

    output_path = args.output
    if not output_path:
        output_path = args.input.with_suffix(".md")
        if args.input.suffix.lower() == ".md":
            output_path = args.input.with_name(f"{args.input.stem}_markup.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"✅ Saved CriticMarkup to {output_path}", file=sys.stderr)
    print(f"Stats: {len(edits)} edits processed.", file=sys.stderr)


def handle_sanitize(args: argparse.Namespace):
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

    parser = argparse.ArgumentParser(prog="adeu", description="Adeu: Agentic DOCX Redlining Engine")
    _version, _sha, _ = get_build_info()
    _ver_str = f"{_version}+{_sha}" if _sha and _sha != "unknown" else _version
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
            "or 'all' for search (defaults to all)."
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
    p_extract.add_argument(
        "--outline-max-level",
        type=int,
        default=2,
        help="For mode='outline' only: maximum heading depth to show.",
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

    args = parser.parse_args()

    import logging

    import structlog

    log_level = logging.DEBUG if args.debug else logging.WARNING
    # stdout is reserved for document data / JSON results; all logging must
    # stay on stderr so `adeu extract doc.docx > out.md` stays clean. The
    # dynamic proxy (not sys.stderr itself) keeps this global config valid
    # even if the stderr object is replaced or closed after configure time.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=dynamic_stderr),
    )

    args.func(args)


if __name__ == "__main__":
    main()
