import sys
from pathlib import Path

import docx

from adeu.ingest import _extract_text_from_doc
from adeu.outline import extract_outline
from adeu.pagination import paginate


def create_manual_break_doc(path: Path):
    doc = docx.Document()
    doc.add_heading("Pagination Test Document", level=1)
    for page_num in range(1, 6):
        doc.add_heading(f"Heading on Page {page_num}", level=2)
        doc.add_paragraph(f"This is paragraph content belonging strictly to page {page_num}. " * 5)
        if page_num < 5:
            doc.add_page_break()
    doc.save(str(path))


def test_manual_page_breaks_pagination(tmp_path):
    doc_path = tmp_path / "manual_breaks.docx"
    create_manual_break_doc(doc_path)

    doc = docx.Document(str(doc_path))
    projected_body = _extract_text_from_doc(doc, include_appendix=False)

    # Run paginate
    pag_res = paginate(projected_body)

    # The document must have 5 pages
    assert pag_res.total_pages == 5, f"Expected 5 pages, got {pag_res.total_pages}"

    # Assert each page has the correct heading
    for page_num in range(1, 6):
        page_content = pag_res.pages[page_num - 1].page_content
        assert f"Heading on Page {page_num}" in page_content
        assert f"This is paragraph content belonging strictly to page {page_num}." in page_content
        # Ensure other page content is NOT leaked to this page
        for other_num in range(1, 6):
            if other_num != page_num:
                assert f"Heading on Page {other_num}" not in page_content


def test_manual_page_breaks_outline(tmp_path):
    doc_path = tmp_path / "manual_breaks.docx"
    create_manual_break_doc(doc_path)

    doc = docx.Document(str(doc_path))
    projected_body = _extract_text_from_doc(doc, include_appendix=False)

    pag_res = paginate(projected_body)

    # Get outline
    # extract_outline expects (doc, projected_body, body_pages, body_page_offsets, paragraph_offsets)
    body_pages = [p.page_content for p in pag_res.pages]
    body_page_offsets = pag_res.body_page_offsets

    nodes = extract_outline(doc, projected_body, body_pages, body_page_offsets)

    # We should have headings on pages 1 to 5
    headings = [node for node in nodes if node.level == 2]
    assert len(headings) == 5
    for i, node in enumerate(headings):
        expected_page = i + 1
        assert node.text == f"Heading on Page {expected_page}"
        assert node.page == expected_page, f"Expected {node.text} to be on page {expected_page}, got page {node.page}"


def run_cli(args, capsys):
    """Invoke the CLI in-process; returns (exit_code, stdout, stderr)."""
    from unittest.mock import patch

    from adeu.cli import main

    code = 0
    with patch.object(sys, "argv", ["adeu"] + [str(a) for a in args]):
        try:
            main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_apply_large_document_major_deletions(tmp_path, capsys):
    # 1. Create the original document with 100 sections
    doc_path = tmp_path / "large_bug.docx"
    doc = docx.Document()
    for i in range(1, 101):
        doc.add_heading(f"Section {i}", level=2)
        doc.add_paragraph(f"This is the body paragraph for section {i}. " * 15)
        if i % 10 == 0:
            doc.add_paragraph("Some special marker for search in section " + str(i))
    doc.save(str(doc_path))

    # 2. Create the truncated modified text file
    txt_path = tmp_path / "large_truncated_bug.txt"
    content = f"> **File Path:** {doc_path.name}\n\n# Large Document Test\n\nOnly Section 1 is here."
    txt_path.write_text(content, encoding="utf-8")

    # 3. Execute apply command
    out_path = tmp_path / "large_applied_bug.docx"
    code, stdout, stderr = run_cli(
        ["apply", str(doc_path), str(txt_path), "-o", str(out_path), "--allow-major-deletions"], capsys
    )

    # The regression test asserts that the bug is fixed and it completes successfully
    assert code == 0, f"apply failed with code {code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    assert out_path.exists(), "Output file was not generated."


def test_accept_all_json_response_enrichment(tmp_path, capsys):
    import json

    import docx

    # 1. Create a simple base document
    doc_path = tmp_path / "base.docx"
    doc = docx.Document()
    doc.add_paragraph("This is a test document.")
    doc.save(str(doc_path))

    # 2. Define a modify edit with a comment
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

    # 3. Apply the edit to create tracked changes + comment
    code, stdout, stderr = run_cli(
        ["apply", str(doc_path), str(changes_file), "-o", str(redlined_path), "--json"],
        capsys,
    )
    assert code == 0, f"apply failed with code {code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    assert redlined_path.exists()

    # 4. Run accept-all in JSON mode
    accepted_path = tmp_path / "accepted.docx"
    code, stdout, stderr = run_cli(
        ["accept-all", str(redlined_path), "-o", str(accepted_path), "--json"],
        capsys,
    )
    assert code == 0, f"accept-all failed with code {code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"

    # 5. Parse JSON output and assert that keys are present and counts are correct
    result = json.loads(stdout.strip())
    assert result.get("status") == "ok"
    assert "accepted_insertions" in result, "accepted_insertions missing from JSON output"
    assert "accepted_deletions" in result, "accepted_deletions missing from JSON output"
    assert "removed_comments" in result, "removed_comments missing from JSON output"

    assert result["accepted_insertions"] == 1
    assert result["accepted_deletions"] == 1
    assert result["removed_comments"] == 1


def test_sanitize_baseline_printf_leak(tmp_path):
    """
    Asserts that sanitize_docx raises a SanitizeError with an error message
    that does not leak printf-style string formatting specifiers (i.e. percent signs are properly escaped as %%).
    This prevents Go, Python or any other printf-style logging/formatting host from breaking
    with "not enough arguments" or showing "!o(MISSING)" / "!d(MISSING)".
    """
    import docx
    import pytest

    from adeu.sanitize.core import SanitizeError, sanitize_docx

    # 1. Create two highly different documents to trigger similarity check failure
    working = tmp_path / "normal.docx"
    doc_norm = docx.Document()
    doc_norm.add_paragraph("Agreement between Alpha and Beta.")
    doc_norm.save(str(working))

    baseline = tmp_path / "unicode.docx"
    doc_uni = docx.Document()
    doc_uni.add_paragraph("Different text in Chinese 统一码.")
    doc_uni.save(str(baseline))

    out = tmp_path / "out.docx"

    # 2. Call sanitize_docx and verify the error message is safe from printf leaks
    with pytest.raises(SanitizeError) as exc_info:
        sanitize_docx(str(working), str(out), baseline_path=str(baseline))

    err_msg = str(exc_info.value)

    # The error message should contain the similarity warning/block reason
    assert "share only" in err_msg
    assert "differs" in err_msg

    # Verify that the percent signs are properly escaped as '%%' by applying Python '%' formatting on it.
    # If any plain '%' is followed by space and characters like 'o' or 'd', it will raise TypeError.
    # Once fixed (escaped to '%%'), formatting with % () will succeed and produce a clean message with single '%' signs.
    try:
        formatted = err_msg % ()
    except TypeError as e:
        pytest.fail(
            f"Printf-style leak detected in error message! Formatting the message failed with: {e}\n"
            f"Error message: {err_msg}"
        )

    # After safe formatting, it should have single percent signs and NO formatting leaks
    assert "share only 41% of" in formatted
    assert "differs" in formatted
    assert "%!" not in formatted  # Ensure no Go-style formatting errors
