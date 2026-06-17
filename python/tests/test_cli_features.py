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
    changes_data = [{"type": "modify", "target_text": "document", "new_text": "simulated modified document"}]
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
    test_args_with_debug = ["adeu", "--debug", "extract", str(fixture_path), "--mode", "full"]
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
    test_args_full = ["adeu", "extract", str(fixture_path), "--mode", "full", "--page", "1"]
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
