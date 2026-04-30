import docx


def test_diff_engine_splits_markdown_tokens():
    """
    Reproduces Bug 8: Diff engine breaks mid-markdown token.
    """
    d1 = docx.Document()
    d1.add_paragraph("ons)**  **(In millions)**          Dear")
    d1.save("diff_markdown_1.docx")

    d2 = docx.Document()
    d2.add_paragraph("ons)**  **(In millions)**\n\n\n\n\n\n\n\n\nDear shareholders:")
    d2.save("diff_markdown_2.docx")

    import subprocess

    diff_output = subprocess.check_output(
        ["uv", "run", "adeu", "diff", "diff_markdown_1.docx", "diff_markdown_2.docx"]
    ).decode("utf-8")

    # The bug produces `- *(In millions)**` which is invalid markdown.
    assert "- *(In millions)**" not in diff_output, "BUG: Diff engine split a markdown token!"
