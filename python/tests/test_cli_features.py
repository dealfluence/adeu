import json
import sys
from pathlib import Path

import pytest

from adeu.mcp_components.shared import read_file_bytes


def get_fixture_path(name: str) -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "shared" / "fixtures").is_dir():
            return parent / "shared" / "fixtures" / name
    raise FileNotFoundError(f"Could not find fixtures directory for {name}")


def test_sandbox_warning_on_read_failure():
    with pytest.raises(FileNotFoundError) as exc_info:
        read_file_bytes("definitely_non_existent_file_path_123456.docx")

    msg = str(exc_info.value)
    assert "If you are running in a sandboxed/containerized environment" in msg
    assert "uv tool install adeu" in msg


def test_cli_extract_modes(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # Test extract mode=outline
    test_args = ["adeu", "extract", str(fixture_path), "--mode", "outline"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0

    captured = capsys.readouterr()
    assert "#" in captured.out
    assert "Outline view" in captured.out

    # Test extract mode=appendix
    test_args = ["adeu", "extract", str(fixture_path), "--mode", "appendix"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0

    captured = capsys.readouterr()
    assert "Appendix" in captured.out


def test_cli_apply_dry_run(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"

    # Create an edit
    changes_data = [
        {
            "type": "modify",
            "target_text": "document",
            "new_text": "simulated modified document",
        }
    ]
    with open(changes_file, "w") as f:
        json.dump(changes_data, f)

    # Execute adeu apply with --dry-run
    test_args = ["adeu", "apply", str(fixture_path), str(changes_file), "--dry-run"]

    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    # Verify no file was created next to golden.docx
    processed_expected = fixture_path.parent / "golden_processed.docx"
    assert not processed_expected.exists()

    captured = capsys.readouterr()
    # Check that detailed reports are printed to stderr
    err_output = captured.err
    assert "Dry-run simulation complete." in err_output
    assert "Actions:" in err_output
    assert "Edits:" in err_output
    assert "Detailed Edit Reports:" in err_output


def test_cli_debug_logging(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # 1. Test WITHOUT --debug flag
    test_args_no_debug = ["adeu", "extract", str(fixture_path), "--mode", "full"]
    with patch.object(sys, "argv", test_args_no_debug):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "Initializing CommentsManager" not in combined_output

    # 2. Test WITH --debug flag
    test_args_with_debug = [
        "adeu",
        "--debug",
        "extract",
        str(fixture_path),
        "--mode",
        "full",
    ]
    with patch.object(sys, "argv", test_args_with_debug):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "Initializing CommentsManager" in combined_output


def test_cli_pagination_parity(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # 1. Test CLI extract --mode outline contains 'adeu extract' and 'Run `adeu extract'
    test_args_outline = ["adeu", "extract", str(fixture_path), "--mode", "outline"]
    with patch.object(sys, "argv", test_args_outline):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0

    captured_outline = capsys.readouterr()
    assert "adeu extract" in captured_outline.out
    assert "read_docx" not in captured_outline.out

    # 2. Test CLI extract --mode full (shows page 1 with CLI navigation instructions)
    test_args_full = [
        "adeu",
        "extract",
        str(fixture_path),
        "--mode",
        "full",
        "--page",
        "1",
    ]
    with patch.object(sys, "argv", test_args_full):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0

    captured_full = capsys.readouterr()
    # Check that any pagination banner or footer points to 'adeu extract'
    if "Page 1 of" in captured_full.out:
        assert "adeu extract" in captured_full.out
        assert "read_docx" not in captured_full.out

    # 3. Verify MCP-specific builders (default is_cli=False) still output 'read_docx'
    from adeu.mcp_components._response_builders import build_paginated_response

    large_text = "A\n\n" * 10000  # Exceeds PAGE_TARGET_CHARS to force pagination
    mcp_paginated = build_paginated_response(large_text, 1, "test_doc.docx", is_cli=False)
    mcp_markdown = mcp_paginated.structured_content["markdown"]
    assert "read_docx" in mcp_markdown
    assert "adeu extract" not in mcp_markdown

    # 4. Verify CLI builders (is_cli=True) output 'adeu extract'
    cli_paginated = build_paginated_response(large_text, 1, "test_doc.docx", is_cli=True)
    cli_markdown = cli_paginated.structured_content["markdown"]
    assert "adeu extract" in cli_markdown
    assert "read_docx" not in cli_markdown


def test_cli_stdout_unwrapped(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    test_args = ["adeu", "extract", str(fixture_path), "--mode", "full", "--page", "1"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    stdout_output = captured.out.strip()
    assert "TextContent(" not in stdout_output
    assert not (stdout_output.startswith("[") and stdout_output.endswith("]"))
    assert "annotations=" not in stdout_output


def test_cli_corrupt_docx_errors(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    # 1. Zero-byte file
    empty_docx = tmp_path / "empty.docx"
    empty_docx.write_bytes(b"")

    # 2. Garbage file
    garbage_docx = tmp_path / "garbage.docx"
    garbage_docx.write_bytes(b"not a zip at all")

    # Test extract empty.docx
    test_args = ["adeu", "extract", str(empty_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (got bad zip signature)" in captured.err

    # Test extract garbage.docx
    test_args = ["adeu", "extract", str(garbage_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (got bad zip signature)" in captured.err

    # Test apply empty.docx
    fake_changes = tmp_path / "changes.json"
    fake_changes.write_text("[]", encoding="utf-8")
    test_args = ["adeu", "apply", str(empty_docx), str(fake_changes), "--dry-run"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (got bad zip signature)" in captured.err

    # Test sanitize empty.docx
    test_args = ["adeu", "sanitize", str(empty_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (got bad zip signature)" in captured.err


def test_docx_vs_text_diff_precision(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # 1. Extract the original text in clean-view mode
    from io import BytesIO

    with open(fixture_path, "rb") as f:
        sanitized_bytes = f.read()
    from docx import Document

    doc = Document(BytesIO(sanitized_bytes))
    from adeu.ingest import _extract_text_from_doc

    clean_text = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)

    # 2. Modify one specific word in a paragraph dynamically
    import re

    orig_paragraphs = clean_text.split("\n\n")
    target_idx = -1
    for idx, p in enumerate(orig_paragraphs):
        if len(p) > 10 and re.search(r"\bthe\b", p, re.IGNORECASE):
            target_idx = idx
            break
    assert target_idx != -1

    p_orig = orig_paragraphs[target_idx]
    p_mod = re.sub(r"\bthe\b", "the governing and crucial", p_orig, count=1, flags=re.IGNORECASE)
    orig_paragraphs[target_idx] = p_mod
    modified_text = "\n\n".join(orig_paragraphs)

    # Save to a text file
    mod_txt = tmp_path / "mod.txt"
    mod_txt.write_text(modified_text, encoding="utf-8")

    # Run CLI diff
    test_args = ["adeu", "diff", str(fixture_path), str(mod_txt), "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    edits = json.loads(captured.out.strip())

    # We should have exactly 1 localized edit rather than a massive whole-document replacement!
    assert len(edits) == 1
    edit = edits[0]
    assert edit["type"] == "modify"
    # Localized edit size should be small
    assert len(edit["target_text"]) < 50
    assert len(edit["new_text"]) < 50


def test_cli_valid_zip_but_not_docx_errors(tmp_path, capsys):
    import zipfile
    from unittest.mock import patch

    from adeu.cli import main

    # Create a valid zip file named fake.docx
    fake_docx = tmp_path / "fake.docx"
    with zipfile.ZipFile(fake_docx, "w") as z:
        z.writestr("a.txt", "hi")
        z.writestr("b.txt", "yo")

    # 1. extract
    test_args = ["adeu", "extract", str(fake_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (missing required Word parts)" in captured.err

    # 2. apply
    fake_changes = tmp_path / "changes.json"
    fake_changes.write_text("[]", encoding="utf-8")
    test_args = ["adeu", "apply", str(fake_docx), str(fake_changes), "--dry-run"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (missing required Word parts)" in captured.err

    # 3. diff (fake.docx vs mod.txt)
    mod_txt = tmp_path / "mod.txt"
    mod_txt.write_text("some modification text", encoding="utf-8")
    test_args = ["adeu", "diff", str(fake_docx), str(mod_txt)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (missing required Word parts)" in captured.err

    # 4. sanitize
    test_args = ["adeu", "sanitize", str(fake_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (missing required Word parts)" in captured.err


def test_cli_diff_corrupt_docx_regression(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    empty_docx = tmp_path / "empty.docx"
    empty_docx.write_bytes(b"")

    mod_txt = tmp_path / "mod.txt"
    mod_txt.write_text("some text", encoding="utf-8")

    # Run diff with corrupt/empty original
    test_args = ["adeu", "diff", str(empty_docx), str(mod_txt)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (got bad zip signature)" in captured.err


def test_cli_deeply_malformed_docx_errors(tmp_path, capsys):
    import zipfile
    from unittest.mock import patch

    from adeu.cli import main

    # Create a zip containing [Content_Types].xml but invalid word/document.xml
    fake2_docx = tmp_path / "fake2.docx"
    with zipfile.ZipFile(fake2_docx, "w") as z:
        content_types = (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("word/document.xml", "<<<not valid xml>>>")

    # 1. extract
    test_args = ["adeu", "extract", str(fake2_docx)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (corrupted or invalid OOXML structure)" in captured.err

    # 2. apply
    fake_changes = tmp_path / "changes.json"
    fake_changes.write_text("[]", encoding="utf-8")
    test_args = ["adeu", "apply", str(fake2_docx), str(fake_changes), "--dry-run"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (corrupted or invalid OOXML structure)" in captured.err

    # 3. diff (fake2.docx vs mod.txt)
    mod_txt = tmp_path / "mod.txt"
    mod_txt.write_text("some modification text", encoding="utf-8")
    test_args = ["adeu", "diff", str(fake2_docx), str(mod_txt)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is not a valid DOCX file (corrupted or invalid OOXML structure)" in captured.err


def test_cli_version(capsys):
    from unittest.mock import patch

    from adeu.cli import main
    from adeu.mcp_components.shared import get_build_info

    test_args = ["adeu", "--version"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    output = captured.out or captured.err
    assert "adeu" in output

    # Assert against the dynamically resolved version to avoid hardcoding stale versions
    version, _, _ = get_build_info()
    assert version in output
    assert "+" in output
    assert "unknown" not in output


def test_cli_help_shows_version_and_attribution(capsys):
    from unittest.mock import patch

    from adeu.cli import main
    from adeu.mcp_components.shared import get_build_info

    test_args = ["adeu", "--help"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    out = capsys.readouterr().out

    # The purpose statement stays up front, now carrying the build version.
    assert "Agentic DOCX Redlining Engine" in out
    version, _, _ = get_build_info()
    assert version in out
    assert "unknown" not in out

    # Team/product attribution, mirroring the README.
    assert "https://adeu.ai" in out
    assert "https://github.com/dealfluence/adeu" in out


def test_cli_extract_search_query(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # Test basic search query
    test_args = ["adeu", "extract", str(fixture_path), "--search-query", "golden"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    assert "**Search Results**" in captured.out
    assert "match" in captured.out


def test_cli_extract_search_regex_and_case(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # Test regex search with case insensitivity
    test_args = [
        "adeu",
        "extract",
        str(fixture_path),
        "--search-query",
        "g[oO]lden",
        "--search-regex",
        "--search-case-insensitive",
    ]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    assert "**Search Results**" in captured.out
    assert "golden" in captured.out.lower()


def test_cli_extract_json(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    test_args = ["adeu", "extract", str(fixture_path), "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["title"] == "golden.docx"
    assert Path(payload["file_path"]).name == "golden.docx"
    assert len(payload["markdown"]) > 0


def test_cli_apply_json(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"
    changes_file.write_text(
        json.dumps([{"type": "modify", "target_text": "document", "new_text": "modified document"}]),
        encoding="utf-8",
    )
    out_path = tmp_path / "out.docx"

    test_args = ["adeu", "apply", str(fixture_path), str(changes_file), "-o", str(out_path), "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    stats = json.loads(captured.out.strip())
    assert stats["edits_applied"] == 1
    assert stats["edits_skipped"] == 0
    assert stats["dry_run"] is False
    assert stats["output_path"] == str(out_path)
    assert stats["edits"][0]["status"] == "applied"
    assert out_path.exists()

    # --json suppresses the human-readable progress logs
    assert "Loading structured batch" not in captured.err
    assert "Applying" not in captured.err
    assert "Batch complete" not in captured.err
    assert "Detailed Edit Reports" not in captured.err


def test_cli_apply_json_dry_run(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"
    changes_file.write_text(
        json.dumps([{"type": "modify", "target_text": "document", "new_text": "modified document"}]),
        encoding="utf-8",
    )

    test_args = ["adeu", "apply", str(fixture_path), str(changes_file), "--dry-run", "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    stats = json.loads(captured.out.strip())
    assert stats["dry_run"] is True
    assert stats["output_path"] is None
    assert stats["edits_applied"] == 1
    assert not (fixture_path.parent / "golden_redlined.docx").exists()


def test_cli_apply_json_validation_error(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"
    changes_file.write_text(
        json.dumps([{"type": "modify", "target_text": "THIS TEXT EXISTS NOWHERE 987654321", "new_text": "x"}]),
        encoding="utf-8",
    )
    out_path = tmp_path / "out.docx"

    test_args = ["adeu", "apply", str(fixture_path), str(changes_file), "-o", str(out_path), "--json"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["error"] == "batch_validation_failed"
    assert len(payload["errors"]) == 1
    assert not out_path.exists()


def test_cli_accept_all_workflow(tmp_path, capsys):
    from io import BytesIO
    from unittest.mock import patch

    from adeu.cli import main
    from adeu.ingest import extract_text_from_stream

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"
    changes_file.write_text(
        json.dumps(
            [
                {
                    "type": "modify",
                    "target_text": "document",
                    "new_text": "dossier",
                    "comment": "Review note to be stripped",
                }
            ]
        ),
        encoding="utf-8",
    )
    redlined_path = tmp_path / "redlined.docx"

    # 1. Apply an edit to create tracked changes + a comment
    test_args = ["adeu", "apply", str(fixture_path), str(changes_file), "-o", str(redlined_path), "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None
    capsys.readouterr()
    assert redlined_path.exists()

    # 2. Accept all — default output mirrors the MCP tool (<stem>_clean.docx)
    test_args = ["adeu", "accept-all", str(redlined_path), "--json"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    clean_path = tmp_path / "redlined_clean.docx"
    assert result["status"] == "ok"
    assert result["output_path"] == str(clean_path)
    assert result["accepted_insertions"] == 2
    assert result["accepted_deletions"] == 2
    assert result["removed_comments"] == 4
    assert clean_path.exists()

    # 3. The finalized document has no redlines or comments left
    with open(clean_path, "rb") as f:
        raw_text = extract_text_from_stream(BytesIO(f.read()), clean_view=False)
    assert "dossier" in raw_text
    assert "{++" not in raw_text
    assert "{--" not in raw_text
    assert "Review note to be stripped" not in raw_text


def test_cli_accept_all_human_output(tmp_path, capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")
    out_path = tmp_path / "final.docx"

    test_args = ["adeu", "accept-all", str(fixture_path), "-o", str(out_path)]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    assert out_path.exists()
    assert captured.out == ""
    assert "Accepted all changes" in captured.err


def test_cli_accept_all_missing_file(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    test_args = ["adeu", "accept-all", "definitely_non_existent_file_path_123456.docx"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "sandboxed" in captured.err


def test_cli_debug_logs_go_to_stderr_only(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    test_args = ["adeu", "--debug", "extract", str(fixture_path), "--mode", "full"]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    # Debug logs must never pollute stdout: `adeu extract doc.docx > out.md`
    # has to produce a clean file even with --debug.
    assert "Initializing CommentsManager" in captured.err
    assert "Initializing CommentsManager" not in captured.out


def test_cli_extract_search_page_filter(capsys):
    from unittest.mock import patch

    from adeu.cli import main

    fixture_path = get_fixture_path("golden.docx")

    # Test search with page filtering
    test_args = [
        "adeu",
        "extract",
        str(fixture_path),
        "--search-query",
        "golden",
        "--page",
        "1",
    ]
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None

    captured = capsys.readouterr()
    assert "**Search Results**" in captured.out
    assert "on document page 1" in captured.out
