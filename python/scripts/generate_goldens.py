import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

# Insert the src directory into path so we can run directly via `uv run`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from adeu.redline.engine import RedlineEngine
from adeu.ingest import extract_text_from_stream
from adeu.utils.xml_debug import get_abstracted_xml_snapshot
from adeu.models import (
    ModifyText,
    AcceptChange,
    RejectChange,
    ReplyComment,
    InsertTableRow,
    DeleteTableRow,
)

MODEL_MAP = {
    "modify": ModifyText,
    "accept": AcceptChange,
    "reject": RejectChange,
    "reply": ReplyComment,
    "insert_row": InsertTableRow,
    "delete_row": DeleteTableRow,
}


def normalize_md_timestamps(md_text: str) -> str:
    """
    Replaces volatile ISO-8601 timestamps in CriticMarkup projections
    with a static string to ensure deterministic cross-platform diffing.
    """
    import re

    return re.sub(r"@ \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "@ DATE", md_text)


def main():
    base_dir = Path(__file__).resolve().parent.parent.parent / "shared" / "cross_platform_tests"
    if not base_dir.exists():
        print(f"Creating test directory at {base_dir}")
        base_dir.mkdir(parents=True, exist_ok=True)
        return

    for test_dir in base_dir.iterdir():
        if not test_dir.is_dir():
            continue

        test_json_path = test_dir / "test.json"
        input_docx_path = test_dir / "input.docx"

        if not test_json_path.exists():
            print(f"Skipping {test_dir.name} - missing test.json")
            continue

        if not input_docx_path.exists():
            print(f"Skipping {test_dir.name} - Please copy a sample DOCX to input.docx to seed this test.")
            continue

        print(f"Generating Goldens for [{test_dir.name}]...")
        with open(test_json_path, "r", encoding="utf-8") as f:
            test_config = json.load(f)

        with open(input_docx_path, "rb") as f:
            docx_bytes = f.read()

        author = test_config.get("author", "Adeu AI")
        read_only = test_config.get("read_only", False)

        if read_only:
            stream = BytesIO(docx_bytes)
            raw_md = normalize_md_timestamps(extract_text_from_stream(stream, clean_view=False))
            clean_md = normalize_md_timestamps(extract_text_from_stream(stream, clean_view=True))

            (test_dir / "golden_raw.md").write_text(raw_md, encoding="utf-8")
            (test_dir / "golden_clean.md").write_text(clean_md, encoding="utf-8")
            print("  -> Generated read-only goldens (XML abstraction skipped).")
            continue

        # Map JSON payload to unified DocumentChange models
        changes = []
        for c in test_config.get("changes", []):
            model_cls = MODEL_MAP[c["type"]]
            changes.append(model_cls(**c))

        # 1. Apply changes via Python RedlineEngine
        engine = RedlineEngine(BytesIO(docx_bytes), author=author)
        engine.process_batch(changes)
        mutated_stream = engine.save_to_stream()
        mutated_bytes = mutated_stream.getvalue()

        # 2. Generate XML Abstraction Master
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(mutated_bytes)
            tmp_path = tmp.name

        try:
            abstract_xml = get_abstracted_xml_snapshot(tmp_path)
            # Normalize to LF to avoid git/cross-platform diffing issues
            abstract_xml = abstract_xml.replace("\r\n", "\n")
            (test_dir / "golden_abstract.xml").write_text(abstract_xml, encoding="utf-8")
        finally:
            os.unlink(tmp_path)

        # 3. Generate Markdown Extraction Masters
        raw_md = normalize_md_timestamps(extract_text_from_stream(BytesIO(mutated_bytes), clean_view=False))
        clean_md = normalize_md_timestamps(extract_text_from_stream(BytesIO(mutated_bytes), clean_view=True))

        (test_dir / "golden_raw.md").write_text(raw_md.replace("\r\n", "\n"), encoding="utf-8")
        (test_dir / "golden_clean.md").write_text(clean_md.replace("\r\n", "\n"), encoding="utf-8")

        print("  -> Generated 3/3 goldens (Raw MD, Clean MD, Abstract XML).")


if __name__ == "__main__":
    main()
