import json
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from adeu.cli import _print_sandbox_warning_and_exit, _set_json_mode
from adeu.mcp_components.shared import MCP_ID_DISCOVERY_HINT, _not_found_error
from adeu.models import AcceptChange
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import suggest_sibling_docx


def _create_simple_docx() -> BytesIO:
    doc = Document()
    doc.add_paragraph("Hello world.")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def test_cli_missing_file_suggests_siblings_and_drops_the_sandbox_essay(tmp_path: Path, capsys):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sibling = workdir / "contract_v1.docx"
    sibling.write_bytes(b"dummy")
    missing = workdir / "contract_v2.docx"

    with pytest.raises(SystemExit) as exc_info:
        _print_sandbox_warning_and_exit(missing)
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    err_text = captured.err

    # Must suggest sibling file
    assert "contract_v1.docx" in err_text
    # Must drop the sandbox warning essay
    assert "sandboxed" not in err_text.lower()
    assert "containerized" not in err_text.lower()
    assert "host application" not in err_text.lower()


def test_cli_missing_file_json_mode_still_emits_the_error_contract(tmp_path: Path, capsys):
    missing = tmp_path / "nonexistent.docx"
    _set_json_mode(True)
    try:
        with pytest.raises(SystemExit) as exc_info:
            _print_sandbox_warning_and_exit(missing)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] == "file_not_found"
        assert "failed" in data
        assert isinstance(data["failed"], list)
        assert f"File not found: {missing}" in data["message"]
    finally:
        _set_json_mode(False)


def test_cli_stale_id_error_names_the_changes_ledger():
    stream = _create_simple_docx()
    engine = RedlineEngine(stream)
    act = AcceptChange(type="accept", target_id="Chg:999", comment=None)
    err_msg = engine._action_not_found_error("Chg:999", "999", act)

    assert "--mode changes" in err_msg
    assert "adeu extract" in err_msg


def test_mcp_hint_names_the_changes_ledger_and_never_the_cli():
    stream = _create_simple_docx()
    engine = RedlineEngine(stream, id_discovery_hint=MCP_ID_DISCOVERY_HINT)
    act = AcceptChange(type="accept", target_id="Chg:999", comment=None)
    err_msg = engine._action_not_found_error("Chg:999", "999", act)

    assert "read_docx" in err_msg
    assert "mode='changes'" in err_msg or 'mode="changes"' in err_msg
    assert "adeu" not in err_msg.lower()


def test_suggest_sibling_docx_accepts_limit_and_string_or_path(tmp_path: Path):
    workdir = tmp_path / "test_siblings"
    workdir.mkdir()
    for i in range(12):
        (workdir / f"doc_{i:02d}.docx").write_bytes(b"dummy")

    missing = workdir / "doc_00_missing.docx"

    # Test limit cap
    res5 = suggest_sibling_docx(missing, limit=5)
    assert len(res5) == 5

    res10 = suggest_sibling_docx(str(missing), limit=10)
    assert len(res10) == 10


def test_not_found_error_uses_suggest_sibling_docx(tmp_path: Path):
    workdir = tmp_path / "test_mcp_siblings"
    workdir.mkdir()
    (workdir / "sample_v1.docx").write_bytes(b"dummy")
    missing = workdir / "sample_v2.docx"

    err = _not_found_error(str(missing))
    assert isinstance(err, FileNotFoundError)
    assert "available files: [sample_v1.docx]" in str(err)
