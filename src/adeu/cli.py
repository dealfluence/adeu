import argparse
import getpass
import json
import sys
from io import BytesIO
from pathlib import Path
from typing import List

from adeu import __version__
from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine


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


def main():
    parser = argparse.ArgumentParser(prog="adeu", description="Adeu: Agentic DOCX Redlining Engine")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    p_extract = subparsers.add_parser("extract", help="Extract raw text from a DOCX file")
    p_extract.add_argument("input", type=Path, help="Input DOCX file")
    p_extract.add_argument("-o", "--output", type=Path, help="Output file (default: stdout)")
    p_extract.set_defaults(func=handle_extract)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
