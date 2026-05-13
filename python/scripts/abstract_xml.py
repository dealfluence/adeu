import sys
from pathlib import Path

# Expose local source to the Node.js sub-process
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from adeu.utils.xml_debug import get_abstracted_xml_snapshot

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python abstract_xml.py <path_to_docx>")
        sys.exit(1)

    docx_path = sys.argv[1]
    if not Path(docx_path).exists():
        print(f"Error: Target file not found: {docx_path}", file=sys.stderr)
        sys.exit(1)

    try:
        abstracted = get_abstracted_xml_snapshot(docx_path)
        print(abstracted)
    except Exception as e:
        print(f"Abstraction Engine Error: {e}", file=sys.stderr)
        sys.exit(1)
