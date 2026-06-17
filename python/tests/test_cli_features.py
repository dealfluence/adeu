import sys
from io import BytesIO
import json
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
    from adeu.cli import main
    import argparse
    from unittest.mock import patch

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
    from adeu.cli import main
    from unittest.mock import patch

    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "changes.json"
    
    # Create an edit
    changes_data = [
        {
            "type": "modify",
            "target_text": "document",
            "new_text": "simulated modified document"
        }
    ]
    with open(changes_file, "w") as f:
        json.dump(changes_data, f)
        
    # Execute adeu apply with --dry-run
    test_args = [
        "adeu", "apply", str(fixture_path), str(changes_file),
        "--dry-run"
    ]
    
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
    from adeu.cli import main
    from unittest.mock import patch

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