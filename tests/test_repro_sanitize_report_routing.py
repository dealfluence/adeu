from adeu.sanitize.report import SanitizeReport


def test_report_routes_comment_lines_to_structural():
    """
    Reproduces a bug where the SanitizeReport heuristic incorrectly routes
    comment detail lines (like `[Open] "..."`) into the STRUCTURAL section
    because the string doesn't contain the word "comment".
    """
    report = SanitizeReport("test.docx")

    # This is what transforms.remove_all_comments() yields
    lines = ["Comments removed: 1 (0 resolved, 1 open)", '  [Open] "Updated term." (Counterparty)']

    report.add_transform_lines(lines)

    # The first line correctly goes to removed_comment_lines
    assert "Comments removed: 1 (0 resolved, 1 open)" in report.removed_comment_lines

    # The detail line should be in removed_comment_lines, not structural_lines
    assert '  [Open] "Updated term." (Counterparty)' in report.removed_comment_lines, (
        "BUG: The comment line was not routed to removed_comment_lines."
    )
    assert '  [Open] "Updated term." (Counterparty)' not in report.structural_lines, (
        "BUG: The comment line incorrectly landed in structural_lines."
    )
