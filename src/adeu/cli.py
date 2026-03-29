import argparse
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

from adeu import __version__
from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine


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
            "args": ["--from", "adeu", "adeu-server"],
        }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print("✅ Adeu successfully configured in Claude Desktop.", file=sys.stderr)
    print("   Please restart Claude to load the new toolset.", file=sys.stderr)


def _read_docx_text(path: Path) -> str:
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "rb") as f:
        return extract_text_from_stream(BytesIO(f.read()), filename=path.name)


def _load_batch_from_json(path: Path) -> List[DocumentChange]:
    """
    Loads a batch of actions and edits from a JSON file.
    Supports the unified List[DocumentChange] format or the legacy dict format.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

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
    text = _read_docx_text(args.input)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Extracted text to {args.output}", file=sys.stderr)
    else:
        print(text)


def handle_diff(args):
    text_orig = _read_docx_text(args.original)

    if args.modified.suffix == ".docx":
        text_mod = _read_docx_text(args.modified)
    else:
        with open(args.modified, "r", encoding="utf-8") as f:
            text_mod = f.read()

    edits = generate_edits_from_text(text_orig, text_mod)

    if args.json:
        output = [e.model_dump(exclude={"_match_start_index"}) for e in edits]
        print(json.dumps(output, indent=2))
    else:
        print(f"Found {len(edits)} changes:", file=sys.stderr)
        for e in edits:
            if not e.new_text:
                print(f"[-] {e.target_text}")
            elif not e.target_text:
                print(f"[+] {e.new_text}")
            else:
                print(f"[~] '{e.target_text}' -> '{e.new_text}'")


def handle_apply(args):
    changes: List[DocumentChange] = []

    if args.changes.suffix.lower() == ".json":
        print(f"Loading structured batch from {args.changes}...", file=sys.stderr)
        changes = _load_batch_from_json(args.changes)
    else:
        print(f"Calculating diff from text file {args.changes}...", file=sys.stderr)
        text_orig = _read_docx_text(args.original)
        with open(args.changes, "r", encoding="utf-8") as f:
            text_mod = f.read()
        changes = generate_edits_from_text(text_orig, text_mod)

    print(f"Applying {len(changes)} changes...", file=sys.stderr)
    with open(args.original, "rb") as f:
        stream = BytesIO(f.read())

    engine = RedlineEngine(stream, author=args.author)
    try:
        stats = engine.process_batch(changes)
    except BatchValidationError as e:
        print(f"\n❌ Batch rejected. {len(e.errors)} edits failed validation:\n", file=sys.stderr)
        for err in e.errors:
            print(err, file=sys.stderr)
            print("", file=sys.stderr)
        sys.exit(1)

    output_path = args.output
    if not output_path:
        if args.original.stem.endswith("_redlined") or args.original.stem.endswith("_processed"):
            output_path = args.original
        else:
            output_path = args.original.with_name(f"{args.original.stem}_redlined.docx")

    with open(output_path, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

    print(f"✅ Saved to {output_path}", file=sys.stderr)
    print(
        f"Stats: Actions ({stats['actions_applied']} applied, {stats['actions_skipped']} skipped). "
        f"Edits ({stats['edits_applied']} applied, {stats['edits_skipped']} skipped).",
        file=sys.stderr,
    )
    if stats["actions_skipped"] > 0 or stats["edits_skipped"] > 0:
        sys.exit(1)


def handle_markup(args):
    """Handler for the 'markup' subcommand."""
    if args.input.suffix.lower() == ".docx":
        text = _read_docx_text(args.input)
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            text = f.read()

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


def main():
    parser = argparse.ArgumentParser(prog="adeu", description="Adeu: Agentic DOCX Redlining Engine")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    p_extract = subparsers.add_parser("extract", help="Extract raw text from a DOCX file")
    p_extract.add_argument("input", type=Path, help="Input DOCX file")
    p_extract.add_argument("-o", "--output", type=Path, help="Output file (default: stdout)")
    p_extract.set_defaults(func=handle_extract)

    p_init = subparsers.add_parser("init", help="Auto-configure Adeu for Claude Desktop")
    p_init.add_argument(
        "--local",
        action="store_true",
        help="Configure to run from current source (for dev/testing)",
    )
    p_init.set_defaults(func=handle_init)

    p_diff = subparsers.add_parser("diff", help="Compare two files (DOCX vs DOCX/Text)")
    p_diff.add_argument("original", type=Path, help="Original DOCX")
    p_diff.add_argument("modified", type=Path, help="Modified DOCX or Text file")
    p_diff.add_argument("--json", action="store_true", help="Output raw JSON edits")
    p_diff.set_defaults(func=handle_diff)

    try:
        default_author = getpass.getuser()
    except Exception:
        default_author = "Adeu AI"

    p_apply = subparsers.add_parser("apply", help="Apply edits to a DOCX")
    p_apply.add_argument("original", type=Path, help="Original DOCX")
    p_apply.add_argument("changes", type=Path, help="JSON edits file OR Modified Text file")
    p_apply.add_argument("-o", "--output", type=Path, help="Output DOCX path")
    p_apply.add_argument(
        "--author",
        type=str,
        default=default_author,
        help=f"Author name for Track Changes (default: '{default_author}')",
    )
    p_apply.set_defaults(func=handle_apply)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
