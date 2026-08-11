import json
import re
import sys
from io import BytesIO
from typing import List
from unittest.mock import patch

import pytest
from docx import Document

from adeu.cli import main
from adeu.mcp_components.tools.document import process_document_batch
from adeu.models import DocumentChange, ModifyText
from adeu.payloads import BATCH_RECOVERY_PROTOCOL, failure_envelope
from adeu.redline.engine import BatchValidationError, RedlineEngine
from tests.utils import approx_tokens


def _create_simple_docx() -> BytesIO:
    doc = Document()
    for i in range(25):
        doc.add_paragraph(f"Paragraph {i}.")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def test_cli_batch_failure_carries_protocol_json(tmp_path, capsys):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify", "target_text": "Non-existent target", "new_text": "Fail."},
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "apply", str(doc_path), str(p), "--json"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "batch_validation_failed"
    assert BATCH_RECOVERY_PROTOCOL in data["message"]


def test_cli_batch_failure_carries_protocol_human(tmp_path, capsys):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify", "target_text": "Non-existent target", "new_text": "Fail."},
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "apply", str(doc_path), str(p)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert BATCH_RECOVERY_PROTOCOL in captured.err


def test_cli_schema_failure_carries_protocol_json(tmp_path, capsys):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify"},  # Schema error: missing target_text & new_text
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "apply", str(doc_path), str(p), "--json"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "invalid_changes_file"
    assert BATCH_RECOVERY_PROTOCOL in data["message"]


def test_cli_schema_failure_carries_protocol_human(tmp_path, capsys):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify"},  # Schema error: missing target_text & new_text
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "apply", str(doc_path), str(p)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert BATCH_RECOVERY_PROTOCOL in captured.err


def test_cli_markup_batch_failure_carries_protocol_json(tmp_path, capsys):
    md_path = tmp_path / "test.md"
    md_path.write_text("Paragraph 0.\nParagraph 1.\n", encoding="utf-8")

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify", "target_text": "Non-existent target", "new_text": "Fail."},
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "markup", str(md_path), str(p), "--json"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "batch_validation_failed"
    assert BATCH_RECOVERY_PROTOCOL in data["message"]


def test_cli_markup_batch_failure_carries_protocol_human(tmp_path, capsys):
    md_path = tmp_path / "test.md"
    md_path.write_text("Paragraph 0.\nParagraph 1.\n", encoding="utf-8")

    batch_json = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify", "target_text": "Non-existent target", "new_text": "Fail."},
    ]
    p = tmp_path / "changes.json"
    p.write_text(json.dumps(batch_json), encoding="utf-8")

    test_args = ["adeu", "markup", str(md_path), str(p)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert BATCH_RECOVERY_PROTOCOL in captured.err


@pytest.mark.anyio
async def test_mcp_batch_failure_carries_protocol(tmp_path):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    changes = [
        {"type": "modify", "target_text": "Paragraph 0.", "new_text": "Updated 0."},
        {"type": "modify", "target_text": "Non-existent target", "new_text": "Fail."},
    ]

    class FakeContext:
        async def info(self, *a, **kw):
            pass

        async def warning(self, *a, **kw):
            pass

        async def debug(self, *a, **kw):
            pass

        async def error(self, *a, **kw):
            pass

    result = await process_document_batch(
        reasoning="Testing protocol presence",
        original_docx_path=str(doc_path),
        author_name="Tester",
        ctx=FakeContext(),  # type: ignore
        changes=changes,
    )

    assert BATCH_RECOVERY_PROTOCOL in result


def test_envelope_message_carries_protocol():
    env = failure_envelope("batch_validation_failed", [(0, "Reason 0")], "Batch failed.")
    assert env["message"].endswith(BATCH_RECOVERY_PROTOCOL)


@pytest.mark.anyio
async def test_failure_payload_size_budget(tmp_path):
    doc_path = tmp_path / "test.docx"
    doc_path.write_bytes(_create_simple_docx().getvalue())

    # 20-edit batch with 1 bad edit (index 10)
    changes = []
    for i in range(20):
        if i == 10:
            changes.append({"type": "modify", "target_text": f"Non-existent {i}.", "new_text": f"Updated {i}."})
        else:
            changes.append({"type": "modify", "target_text": f"Paragraph {i}.", "new_text": f"Updated {i}."})

    class FakeContext:
        async def info(self, *a, **kw):
            pass

        async def warning(self, *a, **kw):
            pass

        async def debug(self, *a, **kw):
            pass

        async def error(self, *a, **kw):
            pass

    result = await process_document_batch(
        reasoning="Testing failure payload token budget",
        original_docx_path=str(doc_path),
        author_name="Tester",
        ctx=FakeContext(),  # type: ignore
        changes=changes,
    )

    assert approx_tokens(result) <= 500

    m = re.search(r"```json\s*(\{.*?\})\s*```", result, re.DOTALL)
    assert m is not None
    json_str = m.group(1)
    assert approx_tokens(json_str) <= 500


def test_sequential_state_hint_preserved():
    stream = _create_simple_docx()
    engine = RedlineEngine(stream, author="TestAuthor")

    # Edit 0 modifies "Paragraph 0." to "Paragraph zero updated."
    # Edit 1 targets "Paragraph 0." (which no longer exists after Edit 0 applies)
    changes: List[DocumentChange] = [
        ModifyText(type="modify", target_text="Paragraph 0.", new_text="Paragraph zero updated."),
        ModifyText(type="modify", target_text="Paragraph 0.", new_text="Should fail."),
    ]

    with pytest.raises(BatchValidationError) as exc_info:
        engine.process_batch(changes)

    err = exc_info.value
    # Ensure sequential state hint is present in errors
    assert any("intermediate document state" in e for e in err.errors)
