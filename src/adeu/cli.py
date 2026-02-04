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
from typing import List, Any, Dict

from adeu import __version__
from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine


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
        # Fallback for Linux or others, though Claude Desktop is primarily Win/Mac
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def handle_init(args: argparse.Namespace):
    """
    Configures Adeu in the Claude Desktop environment.
    1. Checks for 'uv'.
    2. Locates config file.
    3. Backs up existing config.
    4. Injects MCP server entry.
    """
    print("🤖 Adeu Agentic Setup", file=sys.stderr)

    # 2. Locate Config
    try:
        config_path = _get_claude_config_path()
    except Exception as e:
        print(f"❌ Error locating Claude config: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"📍 Config found: {config_path}", file=sys.stderr)

    # 3. Load or Create Config
    data: Dict[str, Any] = {"mcpServers": {}}
    if config_path.exists():
        # Backup
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

    # 4. Inject Adeu Server
    mcp_servers = data.setdefault("mcpServers", {})

    if args.local:
        # LOCAL DEV MODE: Point to the current running python environment + code
        # This is critical for testing changes before publishing to PyPI.
        cwd = Path.cwd().resolve()
        python_exe = sys.executable
        print("🔧 Configuring in LOCAL DEV mode.", file=sys.stderr)
        print(f"   - CWD: {cwd}", file=sys.stderr)
        print(f"   - Python: {python_exe}", file=sys.stderr)

        mcp_servers["adeu"] = {"command": python_exe, "args": ["-m", "adeu.server"], "cwd": str(cwd)}
    else:
        # PRODUCTION MODE: Zero-Install via uvx
        uv_path = shutil.which("uv") or shutil.which("uvx")
        if not uv_path:
            print("⚠️  Warning: 'uv' tool not found. Install it for production use.", file=sys.stderr)

        mcp_servers["adeu"] = {"command": "uvx", "args": ["--from", "adeu", "adeu-server"]}

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


def _load_edits_from_json(path: Path) -> List[DocumentEdit]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        edits = []
        for item in data:
            target = item.get("target_text") or item.get("original")
            new_val = item.get("new_text") or item.get("replace")
            comment = item.get("comment")

            edits.append(DocumentEdit(target_text=target or "", new_text=new_val or "", comment=comment))
        return edits
    except Exception as e:
        print(f"Error parsing JSON edits: {e}", file=sys.stderr)
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
    edits = []
    if args.changes.suffix.lower() == ".json":
        print(f"Loading structured edits from {args.changes}...", file=sys.stderr)
        edits = _load_edits_from_json(args.changes)
    else:
        print(f"Calculating diff from text file {args.changes}...", file=sys.stderr)
        text_orig = _read_docx_text(args.original)
        with open(args.changes, "r", encoding="utf-8") as f:
            text_mod = f.read()
        edits = generate_edits_from_text(text_orig, text_mod)

    print(f"Applying {len(edits)} edits...", file=sys.stderr)

    with open(args.original, "rb") as f:
        stream = BytesIO(f.read())

    engine = RedlineEngine(stream, author=args.author)
    applied, skipped = engine.apply_edits(edits)

    output_path = args.output
    if not output_path:
        if args.original.stem.endswith("_redlined"):
            output_path = args.original
        else:
            output_path = args.original.with_name(f"{args.original.stem}_redlined.docx")

    with open(output_path, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

    print(f"✅ Saved to {output_path}", file=sys.stderr)
    print(f"Stats: {applied} applied, {skipped} skipped.", file=sys.stderr)
    if skipped > 0:
        sys.exit(1)


def handle_markup(args):
    """Handler for the 'markup' subcommand."""
    # 1. Read the source document
    if args.input.suffix.lower() == ".docx":
        text = _read_docx_text(args.input)
    else:
        # Assume it's already a text/markdown file
        with open(args.input, "r", encoding="utf-8") as f:
            text = f.read()

    # 2. Load edits from JSON
    if not args.edits.exists():
        print(f"Error: Edits file not found: {args.edits}", file=sys.stderr)
        sys.exit(1)

    edits = _load_edits_from_json(args.edits)

    if not edits:
        print("Warning: No edits found in JSON file.", file=sys.stderr)

    # 3. Apply edits as CriticMarkup
    result = apply_edits_to_markdown(
        markdown_text=text,
        edits=edits,
        include_index=args.index,
        highlight_only=args.highlight,
    )

    # 4. Determine output path
    output_path = args.output
    if not output_path:
        output_path = args.input.with_suffix(".md")
        if args.input.suffix.lower() == ".md":
            # Avoid overwriting source if it's already .md
            output_path = args.input.with_name(f"{args.input.stem}_markup.md")

    # 5. Save result
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

    # init command
    p_init = subparsers.add_parser("init", help="Auto-configure Adeu for Claude Desktop")
    p_init.add_argument("--local", action="store_true", help="Configure to run from current source (for dev/testing)")
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
