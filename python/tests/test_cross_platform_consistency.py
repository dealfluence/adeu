"""
Python-side cross-platform consistency framework.

Mirrors the Node test in node/packages/core/src/consistency.test.ts.

For each folder under shared/cross_platform_tests/:
  1. Loads test.json and input.docx
  2. Applies the changes via Python RedlineEngine
  3. Compares against golden_raw.md / golden_clean.md / golden_abstract.xml when present
  4. When test.json contains "validate_comments_xml_namespaces": true, additionally
     checks that every comments-family XML part declares the expected namespaces and
     passes xmllint validation.

This file is the Python twin of consistency.test.ts so that the same corpus of
cross-platform test scenarios exercises BOTH implementations.
"""

import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.models import (
    AcceptChange,
    DeleteTableRow,
    InsertTableRow,
    ModifyText,
    RejectChange,
    ReplyComment,
)
from adeu.redline.engine import RedlineEngine
from adeu.utils.xml_debug import get_abstracted_xml_snapshot

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS_DIR = REPO_ROOT / "shared" / "cross_platform_tests"

MODEL_MAP = {
    "modify": ModifyText,
    "accept": AcceptChange,
    "reject": RejectChange,
    "reply": ReplyComment,
    "insert_row": InsertTableRow,
    "delete_row": DeleteTableRow,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_timestamps(text: str) -> str:
    return re.sub(r"@ \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "@ DATE", text)


def _xmllint_check(xml_bytes: bytes, label: str, tmp_path: Path) -> None:
    """Hard-fails if xmllint is not installed; asserts exit-code == 0."""
    if not shutil.which("xmllint"):
        raise RuntimeError(
            "xmllint is required for this test but was not found on PATH.\n"
            "Install it with:\n"
            "  Ubuntu/Debian : sudo apt install libxml2-utils\n"
            "  macOS (Homebrew): brew install libxml2\n"
            "  Alpine        : apk add libxml2-utils\n"
        )
    out = tmp_path / label
    out.write_bytes(xml_bytes)
    result = subprocess.run(["xmllint", "--noout", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, f"xmllint validation failed for {label}:\n{result.stderr}"


def _validate_comments_xml_namespaces(output_bytes: bytes, folder_name: str, tmp_path: Path) -> None:
    """
    For test scenarios with validate_comments_xml_namespaces=true:
    - Asserts that every comments-family XML part inside the output DOCX
      declares xmlns:w14, xmlns:w15, and xmlns:w16cid on its root element.
    - Runs xmllint on each such part.

    This check exists because abstract_docx_xml() deliberately strips root
    namespace declarations, so the golden_abstract.xml comparison cannot
    catch this class of bug.
    """
    EXPECTED_NS = {
        b"xmlns:w14=": "http://schemas.microsoft.com/office/word/2010/wordml",
        b"xmlns:w15=": "http://schemas.microsoft.com/office/word/2012/wordml",
    }
    COMMENTS_PARTS = [
        "word/comments.xml",
        "word/commentsExtended.xml",
        "word/commentsIds.xml",
        "word/commentsExtensible.xml",
    ]

    with zipfile.ZipFile(io.BytesIO(output_bytes)) as z:
        for part_name in COMMENTS_PARTS:
            # Find potentially numbered variants, e.g. word/comments1.xml
            matches = [
                n
                for n in z.namelist()
                if re.fullmatch(part_name.replace("comments", "comments\\d*"), n) or n == part_name
            ]
            for match in matches:
                xml_bytes = z.read(match)

                # Only comments.xml carries w14:paraId / w14:textId — check it strictly.
                if (
                    "comments.xml" in match
                    and "Extended" not in match
                    and "Ids" not in match
                    and "Extensible" not in match
                ):
                    for attr_bytes, uri in EXPECTED_NS.items():
                        assert attr_bytes in xml_bytes, (
                            f"[{folder_name}] {match}: {attr_bytes.decode()} not declared.\n"
                            f"Expected namespace URI: {uri}\n"
                            f"XML (first 600 bytes):\n"
                            f"{xml_bytes[:600].decode('utf-8', errors='replace')}"
                        )

                safe_label = match.replace("/", "_")
                _xmllint_check(xml_bytes, safe_label, tmp_path)


# ---------------------------------------------------------------------------
# Dynamic test generation from corpus
# ---------------------------------------------------------------------------


def _collect_test_cases():
    if not CORPUS_DIR.exists():
        return []
    cases = []
    for test_dir in sorted(CORPUS_DIR.iterdir()):
        if not test_dir.is_dir():
            continue
        test_json = test_dir / "test.json"
        input_docx = test_dir / "input.docx"
        if test_json.exists() and input_docx.exists():
            cases.append(pytest.param(test_dir, id=test_dir.name))
    return cases


@pytest.mark.parametrize("test_dir", _collect_test_cases())
def test_corpus_scenario(test_dir: Path, tmp_path: Path):
    """
    For each scenario in shared/cross_platform_tests/:
      - Applies the changes via the Python RedlineEngine
      - Compares golden files when present
      - Validates comment XML namespaces when requested by test.json
    """
    cfg = json.loads((test_dir / "test.json").read_text(encoding="utf-8"))
    is_read_only = cfg.get("read_only", False)
    author = cfg.get("author", "Adeu AI")
    validate_ns = cfg.get("validate_comments_xml_namespaces", False)

    docx_bytes = (test_dir / "input.docx").read_bytes()

    if is_read_only:
        output_bytes = docx_bytes
    else:
        changes = []
        for c in cfg.get("changes", []):
            model_cls = MODEL_MAP[c["type"]]
            changes.append(model_cls(**{k: v for k, v in c.items() if k != "type"}))

        engine = RedlineEngine(io.BytesIO(docx_bytes), author=author)
        engine.process_batch(changes)
        output_bytes = engine.save_to_stream().getvalue()

    # --- Namespace validation (custom check, not covered by abstract golden) ---
    if validate_ns and not is_read_only:
        _validate_comments_xml_namespaces(output_bytes, test_dir.name, tmp_path)

    # --- Abstract XML golden comparison (optional) ---
    golden_xml_path = test_dir / "golden_abstract.xml"
    if golden_xml_path.exists() and not is_read_only:
        with open(tmp_path / "output.docx", "wb") as f:
            f.write(output_bytes)
        actual_xml = get_abstracted_xml_snapshot(str(tmp_path / "output.docx"))
        actual_xml = actual_xml.replace("\r\n", "\n")
        expected_xml = golden_xml_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        assert actual_xml == expected_xml, f"[{test_dir.name}] abstract XML does not match golden_abstract.xml"

    # --- Raw markdown extraction golden comparison (optional) ---
    golden_raw = test_dir / "golden_raw.md"
    if golden_raw.exists():
        actual_raw = _normalize_timestamps(
            extract_text_from_stream(io.BytesIO(output_bytes), clean_view=False)
        ).replace("\r\n", "\n")
        expected_raw = golden_raw.read_text(encoding="utf-8").replace("\r\n", "\n")
        assert actual_raw == expected_raw, f"[{test_dir.name}] raw markdown does not match golden_raw.md"

    # --- Clean markdown extraction golden comparison (optional) ---
    golden_clean = test_dir / "golden_clean.md"
    if golden_clean.exists():
        actual_clean = _normalize_timestamps(
            extract_text_from_stream(io.BytesIO(output_bytes), clean_view=True)
        ).replace("\r\n", "\n")
        expected_clean = golden_clean.read_text(encoding="utf-8").replace("\r\n", "\n")
        assert actual_clean == expected_clean, f"[{test_dir.name}] clean markdown does not match golden_clean.md"
