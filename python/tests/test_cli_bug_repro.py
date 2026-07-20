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
