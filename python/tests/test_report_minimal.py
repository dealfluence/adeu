import io
import json

from adeu.payloads import shrink_batch_stats
from tests.utils import approx_tokens, extract_content, get_mock_ctx, run_async


def test_minimal_report_drops_echoes():
    stats = {
        "engine": "python",
        "version": "2.0.0",
        "edits": [
            {
                "status": "applied",
                "type": "modify",
                "target_text": "The quick brown fox jumps over the lazy dog.",
                "new_text": "The fast brown fox jumps over the lazy dog.",
                "clean_text": "The fast brown fox jumps over the lazy dog.",
                "critic_markup": "{--quick--}{++fast++}",
                "pages": [1],
                "heading_path": "Introduction",
                "occurrences_modified": 1,
                "match_mode": "strict",
            }
        ],
    }
    shrunk = shrink_batch_stats(stats)
    edit = shrunk["edits"][0]
    assert "target_text" not in edit
    assert "new_text" not in edit
    assert "clean_text" not in edit


def test_minimal_report_keeps_verification_fields():
    stats = {
        "engine": "python",
        "version": "2.0.0",
        "edits": [
            {
                "status": "applied",
                "type": "modify",
                "target_text": "old",
                "new_text": "new",
                "clean_text": "new",
                "critic_markup": "{--old--}{++new++}",
                "pages": [1, 2],
                "heading_path": "Section 1 > Subsection A",
                "occurrences_modified": 1,
                "match_mode": "strict",
            }
        ],
    }
    shrunk = shrink_batch_stats(stats)
    edit = shrunk["edits"][0]
    assert edit["status"] == "applied"
    assert edit["type"] == "modify"
    assert edit["pages"] == [1, 2]
    assert edit["heading_path"] == "Section 1 > Subsection A"
    assert edit["occurrences_modified"] == 1
    assert edit["critic_markup"] == "{--old--}{++new++}"


def test_minimal_report_keeps_match_mode_only_when_non_strict():
    stats = {
        "edits": [
            {"status": "applied", "match_mode": "strict"},
            {"status": "applied", "match_mode": "first"},
            {"status": "applied", "match_mode": "all"},
        ]
    }
    shrunk = shrink_batch_stats(stats)
    assert "match_mode" not in shrunk["edits"][0]
    assert shrunk["edits"][1]["match_mode"] == "first"
    assert shrunk["edits"][2]["match_mode"] == "all"


def test_failed_edit_keeps_full_error_and_stub_target():
    long_target = "A" * 120
    full_error = "- Edit 1 Failed: Target text 'AAAA...' was not found anywhere in the active document projection."
    stats = {
        "edits": [
            {
                "status": "failed",
                "type": "modify",
                "target_text": long_target,
                "new_text": "replacement",
                "clean_text": "clean",
                "error": full_error,
                "match_mode": "strict",
            }
        ]
    }
    shrunk = shrink_batch_stats(stats)
    edit = shrunk["edits"][0]
    assert edit["status"] == "failed"
    assert edit["error"] == full_error
    assert "target_text" in edit
    assert len(edit["target_text"]) <= 80
    assert "new_text" not in edit
    assert "clean_text" not in edit


def test_minimal_report_token_budget(tmp_path):
    from docx import Document

    from adeu.models import DocumentChange, ModifyText
    from adeu.redline.engine import RedlineEngine

    doc_path = tmp_path / "test_budget.docx"
    doc = Document()
    doc.add_heading("Section 1 Intro", level=1)
    doc.add_paragraph("The quick brown fox jumps over the lazy dog in a very detailed manner.")
    doc.save(doc_path)

    engine = RedlineEngine(io.BytesIO(doc_path.read_bytes()), author="Tester")
    changes: list[DocumentChange] = [
        ModifyText(
            type="modify",
            target_text="The quick brown fox jumps over the lazy dog in a very detailed manner.",
            new_text="The fast reddish fox leaps over the sleeping hound in a concise manner.",
            comment=None,
        ),
        ModifyText(type="modify", target_text="Section 1 Intro", new_text="Overview", comment=None),
    ]
    stats = engine.process_batch(changes)
    shrunk = shrink_batch_stats(stats)

    for edit in shrunk["edits"]:
        dumped = json.dumps(edit, ensure_ascii=False)
        assert approx_tokens(dumped) <= 40, (
            f"Edit JSON exceeded 40 approx-tokens budget ({len(dumped)} chars): {dumped}"
        )


def test_standard_report_is_unchanged(tmp_path, capsys):
    import sys
    from unittest.mock import patch

    from docx import Document

    from adeu.cli import main
    from adeu.models import DocumentChange, ModifyText
    from adeu.redline.engine import RedlineEngine

    doc_path = tmp_path / "test_std.docx"
    doc = Document()
    doc.add_paragraph("The quick brown fox jumps over the lazy dog.")
    doc.save(doc_path)

    engine = RedlineEngine(io.BytesIO(doc_path.read_bytes()), author="Tester")
    changes: list[DocumentChange] = [
        ModifyText(type="modify", target_text="quick", new_text="fast", comment=None),
        ModifyText(type="modify", target_text="fox", new_text="", comment=None),
    ]
    stats = engine.process_batch(changes)

    assert stats["engine"] == "python"
    edit1 = stats["edits"][0]
    assert edit1["target_text"] == "quick"
    assert edit1["new_text"] == "fast"
    assert "fast" in edit1["clean_text"]

    edit2 = stats["edits"][1]
    assert edit2["target_text"] == "fox"
    assert edit2["new_text"] == ""
    assert isinstance(edit2["clean_text"], str)

    changes_file = tmp_path / "changes.json"
    out_path = tmp_path / "out.docx"
    with open(changes_file, "w") as f:
        json.dump(
            [
                {"type": "modify", "target_text": "quick", "new_text": "fast"},
                {"type": "modify", "target_text": "fox", "new_text": ""},
            ],
            f,
        )

    test_args = [
        "adeu",
        "apply",
        str(doc_path),
        str(changes_file),
        "-o",
        str(out_path),
        "--report",
        "standard",
    ]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    err_output = captured.err
    assert "Target: 'fox'" in err_output
    assert "New text: ''" in err_output


def test_batch_level_keeps_version_drops_engine():
    stats = {
        "engine": "python",
        "version": "2.0.0",
        "actions_applied": 1,
        "edits_applied": 1,
    }
    shrunk = shrink_batch_stats(stats)
    assert "version" in shrunk
    assert shrunk["version"] == "2.0.0"
    assert "engine" not in shrunk


def test_skipped_details_deduped_against_edit_errors():
    err_msg = "- Edit 1 Failed: target text not found"
    stats = {
        "skipped_details": [err_msg, "Other skipped detail"],
        "edits": [
            {
                "status": "failed",
                "error": err_msg,
            }
        ],
    }
    shrunk = shrink_batch_stats(stats)
    assert shrunk["skipped_details"] == ["Other skipped detail"]


def test_mcp_default_is_minimal(tmp_path):
    from docx import Document

    from adeu.mcp_components.tools.document import process_document_batch
    from adeu.models import ModifyText

    doc_path = tmp_path / "test.docx"
    d = Document()
    d.add_paragraph("Hello world")
    d.save(doc_path)

    ctx = get_mock_ctx()
    changes = [ModifyText(type="modify", target_text="world", new_text="earth", comment=None)]
    res = run_async(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(doc_path),
            author_name="Tester",
            changes=changes,
            ctx=ctx,
        )
    )
    res_text = extract_content(res)
    assert "*Preview (Clean):*" not in res_text
