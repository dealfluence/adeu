from pathlib import Path

import pytest

from adeu.cli import main
from adeu.mcp_components._response_builders import build_search_response
from tests.utils import approx_tokens


def _make_haystack(count: int = 50, paragraph_length: int = 15) -> str:
    lines = []
    for i in range(1, count + 1):
        pad_before = f"Line {i:02d} prefix text " + ("x" * paragraph_length) + " "
        pad_after = " " + ("y" * paragraph_length) + f" suffix text for line {i:02d}."
        lines.append(f"# Heading {i}\n{pad_before}Supplier contract target {i}{pad_after}")
    return "\n\n".join(lines)


def test_default_cap_is_twenty_and_reports_total():
    text = _make_haystack(50)
    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    assert "50" in md
    assert "20" in md
    assert md.count("### Match ") == 20


def test_max_matches_respected():
    text = _make_haystack(50)
    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=5,
    )
    md = res.structured_content["markdown"]
    assert md.count("### Match ") == 5


def test_match_offset_pages_without_overlap():
    text = _make_haystack(50)
    page1 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=0,
    ).structured_content["markdown"]

    page2 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=20,
    ).structured_content["markdown"]

    assert "### Match 1 (" in page1
    assert "### Match 20 (" in page1
    assert "### Match 21 (" not in page1

    assert "### Match 21 (" in page2
    assert "### Match 40 (" in page2
    assert "### Match 1 (" not in page2


def test_offset_past_end_is_not_an_error():
    text = _make_haystack(10)
    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=100,
    )
    md = res.structured_content["markdown"]
    assert "no matches in this window" in md.lower() or "no matches" in md.lower()


def test_snippet_clamped_to_120_chars_each_side():
    # Long paragraph: 300 chars before, target, 300 chars after
    long_prefix = "A" * 300
    long_suffix = "B" * 300
    text = f"{long_prefix} Supplier target {long_suffix}"

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        full_paragraph=False,
    )
    md = res.structured_content["markdown"]
    # Should not contain 300 A's in a row
    assert "A" * 200 not in md
    assert "B" * 200 not in md
    assert "..." in md


def test_full_paragraph_opt_out():
    long_prefix = "A" * 300
    long_suffix = "B" * 300
    text = f"{long_prefix} Supplier target {long_suffix}"

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        full_paragraph=True,
    )
    md = res.structured_content["markdown"]
    assert "A" * 200 in md
    assert "B" * 200 in md


def test_clamped_snippet_never_leaves_markup_unterminated():
    # CriticMarkup span placed around 150 chars from match
    prefix = "X" * 100 + " {>>[Chg:1 delete] long comment bubble text<<} " + "Y" * 50
    suffix = "Z" * 50 + " {--deleted text span--} " + "W" * 100
    text = f"{prefix} Supplier target {suffix}"

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        full_paragraph=False,
    )
    md = res.structured_content["markdown"]
    # Check that CriticMarkup openers and closers in snippet are balanced
    for opener, closer in (("{>>", "<<}"), ("{--", "--}"), ("{++", "++}"), ("{==", "==}")):
        assert md.count(opener) == md.count(closer)


def test_continue_note_names_next_offset():
    text = _make_haystack(50)
    cli_res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=0,
        is_cli=True,
    )
    cli_md = cli_res.structured_content["markdown"]
    assert "--match-offset 20" in cli_md

    mcp_res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=0,
        is_cli=False,
    )
    mcp_md = mcp_res.structured_content["markdown"]
    assert "match_offset=20" in mcp_md


def test_search_token_budget():
    text = _make_haystack(50)
    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=20,
        match_offset=0,
    )
    content = str(res.content)
    assert approx_tokens(content) <= 20 * 60


def test_cli_search_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    import sys

    import docx

    doc_path = tmp_path / "test.docx"
    doc = docx.Document()
    for i in range(10):
        doc.add_paragraph(f"Supplier clause entry number {i}")
    doc.save(doc_path)

    monkeypatch.setattr(
        sys, "argv", ["adeu", "extract", str(doc_path), "--search-query", "Supplier", "--max-matches", "3"]
    )
    main()
    captured = capsys.readouterr()
    stdout = captured.out
    assert stdout.count("### Match ") == 3
