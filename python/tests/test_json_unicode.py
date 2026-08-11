import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document

from adeu.cli import main


def make_docx_with_text(path: Path, text: str) -> Path:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)
    return path


def test_extract_json_preserves_unicode(tmp_path, capsys):
    text = "It’s a test — with “smart quotes” and em–dashes."
    docx_path = make_docx_with_text(tmp_path / "test_extract.docx", text)

    with patch.object(sys, "argv", ["adeu", "extract", str(docx_path), "--json"]):
        main()

    out = capsys.readouterr().out
    assert "It’s a test — with “smart quotes” and em–dashes." in out
    assert r"\u2019" not in out
    assert r"\u2014" not in out
    data = json.loads(out)
    assert data is not None


def test_apply_stats_json_preserves_unicode(tmp_path, capsys):
    doc_text = "The original contract text."
    docx_path = make_docx_with_text(tmp_path / "test_apply.docx", doc_text)
    out_path = tmp_path / "out.docx"

    edits = [
        {
            "type": "modify",
            "target_text": "original contract text",
            "new_text": "updated “curly” text — with’s quote",
        }
    ]
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")

    with patch.object(
        sys,
        "argv",
        [
            "adeu",
            "apply",
            str(docx_path),
            str(edits_path),
            "-o",
            str(out_path),
            "--json",
        ],
    ):
        main()

    out = capsys.readouterr().out
    assert "updated “curly” text — with’s quote" in out
    assert r"\u201c" not in out
    assert r"\u201d" not in out
    assert r"\u2019" not in out
    data = json.loads(out)
    assert data["edits_applied"] == 1


def test_error_envelope_preserves_unicode(tmp_path, capsys):
    doc_text = "Some text."
    docx_path = make_docx_with_text(tmp_path / "test_err.docx", doc_text)

    # Supply an edit with target text that won't match, including smart quotes in error
    edits = [
        {
            "type": "modify",
            "target_text": "Missing ‘curly’ target text — not found",
            "new_text": "replacement",
        }
    ]
    edits_path = tmp_path / "edits_err.json"
    edits_path.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")

    with patch.object(
        sys,
        "argv",
        [
            "adeu",
            "markup",
            str(docx_path),
            str(edits_path),
            "--json",
        ],
    ):
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code != 0

    out = capsys.readouterr().out
    assert "Missing ‘curly’ target text — not found" in out
    assert r"\u2018" not in out
    assert r"\u2019" not in out
    assert r"\u2014" not in out
    data = json.loads(out)
    assert data["error"] == "batch_validation_failed"


def test_no_escaped_sequences_anywhere(tmp_path, capsys):
    text = "Section 1 — It’s “vital” to verify."
    docx_path = make_docx_with_text(tmp_path / "test_sub.docx", text)

    # 1. extract --json
    with patch.object(sys, "argv", ["adeu", "extract", str(docx_path), "--json"]):
        main()
    out1 = capsys.readouterr().out
    assert r"\u" not in out1

    # 2. diff --json
    mod_path = tmp_path / "mod.txt"
    mod_path.write_text("Section 1 — It’s “essential” to verify.", encoding="utf-8")
    with patch.object(sys, "argv", ["adeu", "diff", str(docx_path), str(mod_path), "--json"]):
        main()
    out2 = capsys.readouterr().out
    assert r"\u" not in out2

    # 3. accept-all --json
    with patch.object(
        sys,
        "argv",
        ["adeu", "accept-all", str(docx_path), "-o", str(tmp_path / "acc.docx"), "--json"],
    ):
        main()
    out3 = capsys.readouterr().out
    assert r"\u" not in out3


def test_output_is_utf8_decodable(tmp_path):
    text = "Heading — “curly quotes” & it’s fine"
    docx_path = make_docx_with_text(tmp_path / "utf8.docx", text)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run(
        [sys.executable, "-m", "adeu.cli", "extract", str(docx_path), "--json"],
        capture_output=True,
        env=env,
    )
    assert res.returncode == 0
    raw_stdout = res.stdout
    decoded = raw_stdout.decode("utf-8")
    assert "Heading — “curly quotes” & it’s fine" in decoded
    assert b"\\u2014" not in raw_stdout
    assert b"\\u201c" not in raw_stdout
    assert b"\\u2019" not in raw_stdout
    # Non-ASCII UTF-8 bytes must be present in raw output
    assert "—".encode("utf-8") in raw_stdout
    assert "“".encode("utf-8") in raw_stdout
