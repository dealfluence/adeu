import re
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
    assert "(50 total, 20 shown)" in md
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
    assert "no matches in this window (match_offset=100, total matches=10)" in md.lower()


def test_multi_hit_paragraph_snippet_gap_elision():
    prefix = "A" * 300
    middle = "M" * 1000
    suffix = "Z" * 300
    text = f"{prefix} Supplier clause one {middle} Supplier clause two {suffix}"

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    assert " ... " in md
    assert "M" * 500 not in md
    assert "**Supplier** clause one" in md
    assert "**Supplier** clause two" in md


def test_per_hit_match_limiting():
    text = "Supplier one. Supplier two. Supplier three. Supplier four. Supplier five."

    # Window 1: max_matches=2, offset=0 -> hits 1, 2
    res1 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=2,
        match_offset=0,
    )
    md1 = res1.structured_content["markdown"]
    assert "(5 total, 2 shown)" in md1
    assert "match_offset=2" in md1
    assert "**Supplier** one" in md1
    assert "**Supplier** two" in md1
    assert "**Supplier** three" not in md1

    # Window 2: max_matches=2, offset=2 -> hits 3, 4
    res2 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=2,
        match_offset=2,
    )
    md2 = res2.structured_content["markdown"]
    assert "(5 total, 2 shown)" in md2
    assert "match_offset=4" in md2
    assert "**Supplier** three" in md2
    assert "**Supplier** four" in md2
    assert "**Supplier** one" not in md2
    assert "**Supplier** five" not in md2

    # Window 3: max_matches=2, offset=4 -> hit 5
    res3 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=2,
        match_offset=4,
    )
    md3 = res3.structured_content["markdown"]
    assert "(5 total, 1 shown)" in md3
    assert "**Supplier** five" in md3
    assert "**Supplier** four" not in md3

    # Past end: offset=10
    res4 = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
        max_matches=2,
        match_offset=10,
    )
    md4 = res4.structured_content["markdown"]
    assert "(match_offset=10, total matches=5)" in md4


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


_MARKUP_PAIRS = (("{>>", "<<}"), ("{--", "--}"), ("{++", "++}"), ("{==", "==}"))


def _assert_markup_terminated_in_order(md: str) -> None:
    """
    Walks CriticMarkup delimiters in document order: a closer may never appear
    before its opener, and nothing may still be open at the end. Counting
    delimiters per pair is NOT enough — one stray closer on the left plus one
    stray opener on the right balances arithmetically while reading
    `l1--}` … `{--del`.
    """
    token_re = re.compile("|".join(re.escape(t) for pair in _MARKUP_PAIRS for t in pair))
    closer_of = dict(_MARKUP_PAIRS)
    opener_of = {closer: opener for opener, closer in _MARKUP_PAIRS}
    depth = dict.fromkeys(closer_of, 0)
    for tok in token_re.finditer(md):
        token = tok.group(0)
        if token in closer_of:
            depth[token] += 1
        else:
            opener = opener_of[token]
            assert depth[opener] > 0, (
                f"closer `{token}` at {tok.start()} has no open `{opener}`: {md[tok.start() - 20 : tok.end() + 20]!r}"
            )
            depth[opener] -= 1
    assert not any(depth.values()), f"unterminated CriticMarkup: {depth}"


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
    _assert_markup_terminated_in_order(res.structured_content["markdown"])


def test_clamped_snippet_markup_balanced_in_order_not_by_count():
    # Both window edges land INSIDE a deletion span: the left edge cuts
    # `{--del1--}` after its opener, the right edge cuts `{--del2--}` after
    # its opener. Delimiter counts match (one `--}`, one `{--`) while the text
    # is nonsense, so only an ordered scan catches it.
    text = "P" * 100 + "{--del1--}" + "Q" * 115 + "Supplier" + "R" * 114 + "{--del2--}" + "S" * 100

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    _assert_markup_terminated_in_order(md)
    assert "{--del1--}" in md
    assert "{--del2--}" in md
    assert "l1--}" not in md.replace("{--del1--}", "")
    assert "{--de" not in md.replace("{--del1--}", "").replace("{--del2--}", "")


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


def test_search_token_budget_with_long_paragraphs():
    # 50 paragraphs of 4000+ chars each: a ±120 window per hit is ~240 chars
    # of context, and 20 of those plus per-entry chrome overshoots the budget
    # unless the renderer accounts for the WHOLE response.
    text = "\n\n".join("a" * 2000 + f" Supplier target {i} " + "b" * 2000 for i in range(50))

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    assert approx_tokens(str(res.content)) <= 20 * 60
    assert md.count("### Match ") == 20
    assert "**Supplier**" in md


def test_search_token_budget_with_many_hits_in_one_paragraph():
    # One paragraph, 25 hits separated by 300 chars: every hit window is its own
    # elided segment inside a SINGLE entry, so the budget must be enforced
    # across segments too, not per entry.
    text = ("z" * 300).join(["Supplier"] * 25)

    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    assert approx_tokens(md) <= 20 * 60
    assert "**Supplier**" in md


def test_small_max_matches_still_shows_every_requested_match():
    # Budget scales with max_matches, so a narrow page must trim snippets
    # rather than silently drop entries the caller explicitly asked for.
    text = "\n\n".join("a" * 2000 + f" Supplier target {i} " + "b" * 2000 for i in range(50))

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
    assert approx_tokens(str(res.content)) <= 5 * 60


def test_match_entry_blocks_keep_blank_line_separators():
    text = _make_haystack(3)
    res = build_search_response(
        text,
        search_query="Supplier",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = res.structured_content["markdown"]
    assert "---\n\n### Match 1 (p1)\n\n**Path:** `Heading 1`\n\n> " in md
    assert "\n\n*Occurrences:*" in md


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
