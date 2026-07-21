# FILE: tests/test_cli_bug_repro.py
"""
Regression test for the adeu apply text-file verification failure on table/structural deletion.
"""

import sys
from unittest.mock import patch

from docx import Document


def _run_cli(argv: list[str]) -> int:
    """Runs the adeu CLI in-process; returns the exit code."""
    from adeu.cli import main

    with patch.object(sys, "argv", ["adeu"] + argv):
        try:
            main()
        except SystemExit as e:
            return int(e.code or 0)
    return 0


def test_apply_table_deletion_repro(tmp_path):
    """
    Test that adeu apply can successfully delete table/structural elements from a text file,
    computes the textual differences, marks the table rows as deleted, and outputs a valid .docx file
    without failing the post-apply verification check.
    """
    docx_path = tmp_path / "original.docx"
    txt_path = tmp_path / "clean.txt"
    edited_txt_path = tmp_path / "edited_clean.txt"
    out_path = tmp_path / "applied.docx"

    # 1. Create original.docx containing a heading and a standard 3x3 table
    doc = Document()
    doc.add_heading("My Document Heading", level=1)

    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "Header A"
    table.cell(0, 1).text = "Header B"
    table.cell(0, 2).text = "Header C"

    for r in range(1, 3):
        for c in range(3):
            table.cell(r, c).text = f"Row{r} Col{c}"

    doc.save(docx_path)

    # 2. Extract clean text using the CLI
    rc_extract = _run_cli(["extract", "--clean-view", str(docx_path), "-o", str(txt_path)])
    assert rc_extract == 0, "CLI extract failed"
    assert txt_path.exists(), "Clean text file was not created"

    # 3. Read the clean text and delete the lines representing the table
    clean_text = txt_path.read_text(encoding="utf-8")

    # Filter out table lines (containing headers or row contents or structural pipes)
    filtered_lines = []
    for line in clean_text.splitlines():
        if "|" in line or "Row" in line or "Header" in line:
            continue
        filtered_lines.append(line)

    edited_clean_text = "\n".join(filtered_lines)
    edited_txt_path.write_text(edited_clean_text, encoding="utf-8")

    # 4. Apply back using the CLI with --allow-major-deletions
    rc_apply = _run_cli(["apply", str(docx_path), str(edited_txt_path), "-o", str(out_path), "--allow-major-deletions"])

    # This asserts the CORRECT expected behavior:
    # Under the bug, apply fails (returns non-zero) and does not write out
    # applied.docx because of verification mismatch.
    # When fixed, it should succeed (return 0) and write out the file.
    assert rc_apply == 0, "adeu apply failed with exit code 1 due to post-apply validation mismatch"
    assert out_path.exists(), "Applied output docx was not written"


def test_apply_comment_reply_order_repro(tmp_path):
    """
    Test that adeu apply successfully processes a batch of actions containing
    both an AcceptChange on a tracked change and a ReplyComment on the wrapping comment,
    even when the AcceptChange is ordered before the ReplyComment in the batch array.

    The correct expected behavior is that both the AcceptChange and ReplyComment are
    successfully applied. Under the bug, the accept is executed first, which deletes the
    associated comment from the document structure, causing the reply to fail validation and
    reverting/failing the entire batch application.
    """
    import io
    import json
    import re

    from adeu.ingest import extract_text_from_stream
    from adeu.models import ModifyText
    from adeu.redline.engine import RedlineEngine

    docx_path = tmp_path / "original.docx"
    updated_docx_path = tmp_path / "updated.docx"
    batch_json_path = tmp_path / "batch.json"
    out_path = tmp_path / "applied.docx"

    # 1. Create a baseline docx file
    doc = Document()
    doc.add_paragraph("Text with comment.")
    doc.save(docx_path)

    # 2. Add track change + comment to create a document with pending review actions
    with open(docx_path, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Author1")
    edit = ModifyText(target_text="Text", new_text="TextModified", comment="Initial Comment")
    engine.apply_edits([edit])

    stream_mid = engine.save_to_stream()
    with open(updated_docx_path, "wb") as f:
        f.write(stream_mid.getbuffer())

    # 3. Extract the comment ID and change ID to prepare our batch actions
    text_mid = extract_text_from_stream(stream_mid)
    com_match = re.search(r"\[Com:(\d+)\]", text_mid)
    chg_match = re.search(r"\[Chg:(\d+)", text_mid)

    assert com_match, "Comment ID not found in document text"
    assert chg_match, "Change ID not found in document text"

    com_id = f"Com:{com_match.group(1)}"
    chg_id = f"Chg:{chg_match.group(1)}"

    # 4. Construct a batch array where AcceptChange comes BEFORE ReplyComment
    batch_data = [
        {"type": "accept", "target_id": chg_id},
        {"type": "reply", "target_id": com_id, "text": "This reply is evaluated too late."},
    ]
    with open(batch_json_path, "w", encoding="utf-8") as f:
        json.dump(batch_data, f)

    # 5. Run the apply CLI command to apply this batch of review actions
    rc_apply = _run_cli(["apply", str(updated_docx_path), str(batch_json_path), "-o", str(out_path)])

    # This asserts the CORRECT expected behavior:
    # Under the bug, apply fails (returns non-zero exit code 1) because
    # the comment is deleted prior to the reply being applied.
    # When fixed, it should succeed (return 0) and write out the file.
    assert rc_apply == 0, f"adeu apply failed with exit code {rc_apply} due to strict action ordering constraint"
    assert out_path.exists(), "Applied output docx was not written"


def test_silent_comment_deletion_on_accept(tmp_path):
    """
    Test that accepting a tracked change (e.g., an insertion or deletion)
    successfully preserves any associated comment thread attached to that text block.

    Under the bug, the accept action silently deletes the comment range anchors
    from the main document body, causing quiet data loss. When fixed, accepting
    the tracked change should promote/commit the text but keep the comments and
    all of their existing replies.
    """
    import io
    import json
    import re

    from adeu.ingest import extract_text_from_stream
    from adeu.models import ModifyText
    from adeu.redline.engine import RedlineEngine

    docx_path = tmp_path / "original.docx"
    updated_docx_path = tmp_path / "updated.docx"
    batch_json_path = tmp_path / "batch.json"
    out_path = tmp_path / "applied.docx"

    # 1. Create a baseline docx file
    doc = Document()
    doc.add_paragraph("The quick brown fox jumps.")
    doc.save(docx_path)

    # 2. Add a tracked change + comment
    with open(docx_path, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Author1")
    edit = ModifyText(target_text="fox", new_text="cat", comment="This is our critical comment thread context.")
    engine.apply_edits([edit])

    stream_mid = engine.save_to_stream()
    with open(updated_docx_path, "wb") as f:
        f.write(stream_mid.getbuffer())

    # 3. Extract the comment ID and change ID to prepare our batch actions
    text_mid = extract_text_from_stream(stream_mid)
    com_match = re.search(r"\[Com:(\d+)\]", text_mid)
    chg_match = re.search(r"\[Chg:(\d+)", text_mid)

    assert com_match, "Comment ID not found in intermediate text"
    assert chg_match, "Change ID not found in intermediate text"

    com_id = f"Com:{com_match.group(1)}"
    chg_id = f"Chg:{chg_match.group(1)}"

    # 4. Construct a batch array containing only the accept action for the change
    batch_data = [{"type": "accept", "target_id": chg_id}]
    with open(batch_json_path, "w", encoding="utf-8") as f:
        json.dump(batch_data, f)

    # 5. Run the apply CLI command to apply this accept action
    rc_apply = _run_cli(["apply", str(updated_docx_path), str(batch_json_path), "-o", str(out_path)])
    assert rc_apply == 0, f"adeu apply failed with exit code {rc_apply}"
    assert out_path.exists(), "Applied output docx was not written"

    # 6. Extract the text of the generated document and verify that the comment has been preserved
    rc_extract = _run_cli(["extract", str(out_path)])
    assert rc_extract == 0, "CLI extract failed"

    with open(out_path, "rb") as f:
        applied_stream = io.BytesIO(f.read())
    text_applied = extract_text_from_stream(applied_stream)

    # Under the bug, the comment [Com:1] is deleted, so the next assertion fails.
    # When fixed, the comment is preserved, and the assertion passes.
    assert f"[{com_id}]" in text_applied, f"Comment {com_id} was silently deleted when accepting change {chg_id}"


def test_mac_live_flag_help_warning(capsys):
    """
    Test that on non-Windows platforms (like macOS and Linux),
    the help text for both 'extract' and 'apply' commands either hides the '--live' option
    or clearly designates it as Windows-only.
    """
    import sys
    from unittest.mock import patch

    import pytest

    from adeu.cli import main

    if sys.platform == "win32":
        pytest.skip("This check is for non-Windows platforms (macOS/Linux) where --live is unsupported.")

    # 1. Check extract help
    with patch.object(sys, "argv", ["adeu", "extract", "--help"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    out_extract = capsys.readouterr().out

    # 2. Check apply help
    with patch.object(sys, "argv", ["adeu", "apply", "--help"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    out_apply = capsys.readouterr().out

    # Assert that '--live' is either not present or is labeled as Windows-only / Windows only
    # Under the bug, '--live' is present but has no platform warning, so the assertion fails.
    # When fixed, it should either be omitted or have an explicit "Windows-only" warning.
    for cmd_name, out in [("extract", out_extract), ("apply", out_apply)]:
        if "--live" in out:
            # Option is exposed, so it must mention that it is Windows-only
            assert "Windows-only" in out or "Windows only" in out, (
                f"The '--live' option help message in '{cmd_name}' must clearly state "
                "that it is Windows-only on non-Windows platforms."
            )


def test_detailed_report_table_modifications(tmp_path, capsys):
    """
    Test that the detailed report printed to sys.stderr by 'adeu apply'
    displays clear table operation indications ('Inserted row:' and 'Deleted row')
    for InsertTableRow and DeleteTableRow operations instead of misleading 'New text:' lines.
    """
    import json

    docx_path = tmp_path / "doc_tables.docx"
    edits_path = tmp_path / "edits_tables.json"
    out_path = tmp_path / "doc_tables_applied.docx"

    # 1. Create a document with a table
    doc = Document()
    doc.add_heading("Table Test", level=1)
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Row1 Col1"
    table.cell(0, 1).text = "Row1 Col2"
    table.cell(1, 0).text = "Row2 Col1"
    table.cell(1, 1).text = "Row2 Col2"
    table.cell(2, 0).text = "Row3 Col1"
    table.cell(2, 1).text = "Row3 Col2"
    doc.save(docx_path)

    # 2. Define our structural table edits
    edits = [
        {
            "type": "insert_row",
            "target_text": "Row1 Col1 | Row1 Col2",
            "position": "below",
            "cells": ["NewRow Col1", "NewRow Col2"],
        },
        {"type": "delete_row", "target_text": "Row2 Col1"},
    ]
    with open(edits_path, "w", encoding="utf-8") as f:
        json.dump(edits, f)

    # 3. Run adeu apply CLI
    rc = _run_cli(["apply", "-o", str(out_path), str(docx_path), str(edits_path)])
    assert rc == 0

    # 4. Capture stderr output
    captured = capsys.readouterr()
    err_output = captured.err

    # Under the bug, it prints:
    #   New text: 'NewRow Col1 | NewRow Col2'
    # and
    #   New text: ''
    # When fixed, it should print:
    #   Inserted row: 'NewRow Col1 | NewRow Col2'
    # and
    #   Deleted row
    # without displaying "New text:" for these table operations.

    assert "Inserted row: 'NewRow Col1 | NewRow Col2'" in err_output
    assert "Deleted row" in err_output
    assert "New text:" not in err_output


def test_cli_init_success_logs_stream_routing(tmp_path, capsys):
    """
    Test that successful configuration logs of the 'init' command
    are routed to stdout, and stderr remains completely empty.
    """
    from unittest.mock import patch

    mock_config_dir = tmp_path / "Claude"
    mock_config_dir.mkdir()
    mock_config_path = mock_config_dir / "claude_desktop_config.json"

    # --- Case 1: Fresh Installation ---
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("adeu.cli.shutil.which", return_value="/usr/local/bin/uvx"):
            rc = _run_cli(["init", "--scope", "docx"])
            assert rc == 0

    captured = capsys.readouterr()
    stdout_output = captured.out
    stderr_output = captured.err

    # Assert that all successful setup logs are in stdout
    assert "🤖 Adeu Agentic Setup" in stdout_output
    assert "Config will be created" in stdout_output or "Config found" in stdout_output
    assert "Found uvx at: /usr/local/bin/uvx" in stdout_output
    assert "✅ Adeu successfully configured in Claude Desktop." in stdout_output

    # Assert that stderr is completely empty for successful run
    assert stderr_output == ""

    # --- Case 2: Already Configured (No-op) ---
    # Re-running the CLI command with unchanged configuration should trigger the no-op path.
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("adeu.cli.shutil.which", return_value="/usr/local/bin/uvx"):
            rc = _run_cli(["init", "--scope", "docx"])
            assert rc == 0

    captured = capsys.readouterr()
    stdout_output = captured.out
    stderr_output = captured.err

    assert "🤖 Adeu Agentic Setup" in stdout_output
    assert "Config found" in stdout_output
    assert "Found uvx at: /usr/local/bin/uvx" in stdout_output
    assert "✅ Adeu is already configured — config unchanged, no backup needed." in stdout_output

    # Assert that stderr is completely empty for successful run
    assert stderr_output == ""


def test_start_of_run_insertion_alignment_repro(tmp_path, capsys):
    """
    Test that modifying the start of a styled run correctly places the insertion
    at the start of the run (before the target text) instead of at the end of the run.
    """
    import json

    docx_path = tmp_path / "simple.docx"
    edits_path = tmp_path / "edits_modify.json"
    out_path = tmp_path / "simple_edited.docx"

    # 1. Create a pristine document simple.docx with some bold and italic text
    doc = Document()
    p = doc.add_paragraph("This is a simple paragraph with some plain text.")
    p.add_run(" Bold text.").bold = True
    p.add_run(" Italic text.").italic = True
    doc.save(docx_path)

    # 2. Create a JSON edit that prepends text to the bold section
    edits = [{"type": "modify", "target_text": "Bold text.", "new_text": "Super Bold text."}]
    with open(edits_path, "w", encoding="utf-8") as f:
        json.dump(edits, f)

    # 3. Apply the edit to a new document
    rc_apply = _run_cli(["apply", str(docx_path), str(edits_path), "-o", str(out_path)])
    assert rc_apply == 0, f"adeu apply failed with exit code {rc_apply}"
    assert out_path.exists(), "Applied output docx was not written"

    # Clear capsys captured buffers
    capsys.readouterr()

    # 4. Extract the clean text output of the edited document
    rc_extract = _run_cli(["extract", "--clean-view", str(out_path)])
    assert rc_extract == 0, "CLI extract failed"

    captured = capsys.readouterr()
    stdout_output = captured.out

    # Under the bug, the output contains:
    # "This is a simple paragraph with some plain text. Bold text.Super  Italic text."
    # When fixed, it should be:
    # "This is a simple paragraph with some plain text. Super Bold text. Italic text."

    # Assert that the text is correctly prepended, and "Bold text.Super" is NOT present.
    assert "Super Bold text." in stdout_output, (
        f"The insertion was incorrectly placed. Captured output:\n{stdout_output}"
    )
    assert "Bold text.Super" not in stdout_output, (
        "The inserted text was appended to the end of the run instead of prepended."
    )


def test_apply_unicode_character_offset_drift_repro(tmp_path):
    """
    Test that adeu apply successfully applies text-file edits to a document containing
    multi-byte unicode characters (Chinese, Cyrillic, Japanese, Finnish, Emojis)
    without character-to-byte offset drift that causes text corruption and fails
    post-apply verification.
    """
    docx_path = tmp_path / "doc_unicode.docx"
    txt_path = tmp_path / "modified_unicode.txt"
    out_path = tmp_path / "unicode_applied.docx"

    # 1. Create the unicode document
    doc = Document()
    doc.add_heading("Unicode and Internationalization Test", level=0)
    p = doc.add_paragraph("This paragraph contains unicode characters from various languages:")
    p.add_run("\nChinese (Simplified): 🚀 欢迎使用 Adeu 红线引擎！")
    p.add_run("\nCyrillic (Russian): Быстрая бурая лиса прыгает через ленивую собаку.")
    p.add_run("\nJapanese: 素早い茶色の狐が怠惰な犬を飛び越えます。")
    p.add_run("\nFinnish: Vihreä kissa käveli liukkaalla jäällä ja lauloi iloisesti.")
    p.add_run("\nEmojis: 🍕🌮🍣🍺🎈🎉🔥💧🌲🧠")
    doc.save(docx_path)

    # 2. Create the modified text file
    modified_content = f"""> **File Path:** `{docx_path.name}`

# Unicode and Internationalization Test

This paragraph contains unicode characters from various languages:
Chinese (Simplified): 🚀 欢迎使用 Adeu 红线引擎！ 🎉 (Wow!)
Cyrillic (Russian): Быстрая бурая лиса прыгает через ленивую собаку.
Japanese: 素早い茶色の狐が怠惰な犬を飛び越えます。
Finnish: Keltainen kissa käveli liukkaalla jäällä ja lauloi iloisesti.
Emojis: 🍕🌮🍣🍺🎈🎉🔥💧🌲🧠 ✨🌟
"""

    txt_path.write_text(modified_content, encoding="utf-8")

    # 3. Run adeu apply CLI
    rc_apply = _run_cli(["apply", str(docx_path), str(txt_path), "-o", str(out_path)])

    # Under the bug, apply fails (returns exit code 1) because of character/byte offset drift
    # which leads to verification mismatch.
    # When fixed, it should succeed (return 0) and write out the file.
    assert rc_apply == 0, "adeu apply failed due to unicode character-to-byte offset drift"
    assert out_path.exists(), "Applied output docx was not written"
