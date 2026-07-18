# FILE: tests/test_markup.py
"""
Tests for the pure text CriticMarkup transformation function.
"""

import re

import pytest

from adeu.markup import (
    _build_critic_markup,
    _find_match_in_text,
    _make_fuzzy_regex,
    _replace_smart_quotes,
    apply_edits_to_markdown,
)
from adeu.models import ModifyText


class TestHelperFunctions:
    """Tests for internal helper functions."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("\"Hello\" and 'World'", "\"Hello\" and 'World'"),
            ("Smart “quotes” and ‘apostrophes’", "Smart \"quotes\" and 'apostrophes'"),
        ],
    )
    def test_replace_smart_quotes(self, text, expected):
        result = _replace_smart_quotes(text)
        assert result == expected

    @pytest.mark.parametrize(
        "input_str, matches",
        [
            ("hello world", ["hello world", "hello  world", "hello   world"]),
            ("[___]", ["[___]", "[_____]", "[__________]"]),
        ],
    )
    def test_make_fuzzy_regex(self, input_str, matches):
        pattern = _make_fuzzy_regex(input_str)
        for m in matches:
            assert re.match(pattern, m)

    @pytest.mark.parametrize(
        "text, target, expected_start, expected_end",
        [
            ("The quick brown fox", "quick", 4, 9),
            ('"Hello" said the fox', '"Hello"', 0, 7),
            ("hello   world", "hello world", 0, 13),
            ("The quick brown fox", "elephant", -1, -1),
            ("Some text", "", -1, -1),
        ],
    )
    def test_find_match_in_text(self, text, target, expected_start, expected_end):
        start, end = _find_match_in_text(text, target)
        assert start == expected_start
        assert end == expected_end


@pytest.mark.parametrize(
    "params, expected",
    [
        # Basic scenarios
        ({"target_text": "old", "new_text": ""}, "{--old--}"),
        ({"target_text": "", "new_text": "new"}, "{++new++}"),
        ({"target_text": "old", "new_text": "new"}, "{--old--}{++new++}"),
        ({"target_text": "old", "new_text": "new", "comment": "Changed this"}, "{--old--}{++new++}{>>Changed this<<}"),
        (
            {"target_text": "old", "new_text": "new", "edit_index": 3, "include_index": True},
            "{--old--}{++new++}{>>[Edit:3]<<}",
        ),
        (
            {"target_text": "old", "new_text": "new", "comment": "Reason", "edit_index": 5, "include_index": True},
            "{--old--}{++new++}{>>Reason [Edit:5]<<}",
        ),
        ({"target_text": "target", "new_text": "ignored", "highlight_only": True}, "{==target==}"),
        (
            {
                "target_text": "target",
                "new_text": "ignored",
                "comment": "Note",
                "edit_index": 2,
                "include_index": True,
                "highlight_only": True,
            },
            "{==target==}{>>Note [Edit:2]<<}",
        ),
        # Advanced / Formatting
        ({"target_text": "**Important**", "new_text": "**Critical**"}, "**{--Important--}{++Critical++}**"),
        ({"target_text": "_emphasis_", "new_text": "_strong emphasis_"}, "_{--emphasis--}{++strong emphasis++}_"),
        ({"target_text": "**_nested_**", "new_text": "**_deeply nested_**"}, "**{--_nested_--}{++_deeply nested_++}**"),
        (
            {"target_text": "**unbalanced", "new_text": "**still unbalanced"},
            "{--**unbalanced--}{++**still unbalanced++}",
        ),
        ({"target_text": "__0__", "new_text": "__1__"}, "{--__0__--}{++__1__++}"),
        ({"target_text": "Sign: [___]", "new_text": "Sign: John Doe"}, "{--Sign: [___]--}{++Sign: John Doe++}"),
        ({"target_text": "**Term**", "highlight_only": True}, "**{==Term==}**"),
        ({"target_text": "_definition_", "highlight_only": True}, "_{==definition==}_"),
        ({"target_text": "", "new_text": ""}, ""),
        ({"target_text": "   ", "new_text": "text"}, "{--   --}{++text++}"),
        ({"target_text": "Line1\nLine2", "new_text": "SingleLine"}, "{--Line1\nLine2--}{++SingleLine++}"),
        (
            {"target_text": "Use {curly} braces", "new_text": "Use [square] brackets"},
            "{--Use {curly} braces--}{++Use [square] brackets++}",
        ),
        ({"target_text": "A--B", "new_text": "A-B"}, "{--A--B--}{++A-B++}"),
        ({"target_text": "C++", "new_text": "Python"}, "{--C++--}{++Python++}"),
        (
            {"target_text": "old", "new_text": "new", "comment": "Check {this} & <that>"},
            "{--old--}{++new++}{>>Check {this} & <that><<}",
        ),
        (
            {"target_text": "日本語", "new_text": "中文", "comment": "Changed: 한국어"},
            "{--日本語--}{++中文++}{>>Changed: 한국어<<}",
        ),
        ({"target_text": "Hello 👋", "new_text": "Goodbye 👋"}, "{--Hello 👋--}{++Goodbye 👋++}"),
        (
            {"target_text": "<div>content</div>", "new_text": "<span>content</span>"},
            "{--<div>content</div>--}{++<span>content</span>++}",
        ),
        (
            {"target_text": "**A** and **B**", "new_text": "**X** and **Y**"},
            "{--**A** and **B**--}{++**X** and **Y**++}",
        ),
        ({"target_text": "2*3*4", "new_text": "2*4*6"}, "{--2*3*4--}{++2*4*6++}"),
        (
            {
                "target_text": "Section 3.2(a)(i)",
                "new_text": "Section 4.1(b)(ii)",
                "comment": "Updated reference",
                "edit_index": 5,
                "include_index": True,
            },
            "{--Section 3.2(a)(i)--}{++Section 4.1(b)(ii)++}{>>Updated reference [Edit:5]<<}",
        ),
        # Edge Cases
        ({"target_text": "****", "new_text": "text"}, "{--****--}{++text++}"),
        ({"target_text": "**bold_", "new_text": "fixed"}, "{--**bold_--}{++fixed++}"),
        ({"target_text": "___", "new_text": "---"}, "{--___--}{++---++}"),
        ({"target_text": "_*text*_", "new_text": "_*other*_"}, "_{--*text*--}{++*other*++}_"),
        (
            {"target_text": "C:\\Users\\file.txt", "new_text": "C:\\Documents\\file.txt"},
            "{--C:\\Users\\file.txt--}{++C:\\Documents\\file.txt++}",
        ),
        (
            {"target_text": "Price: $100.00 (USD)", "new_text": "Price: $200.00 (EUR)"},
            "{--Price: $100.00 (USD)--}{++Price: $200.00 (EUR)++}",
        ),
        ({"target_text": "Col1\tCol2", "new_text": "Column1\tColumn2"}, "{--Col1\tCol2--}{++Column1\tColumn2++}"),
        ({"target_text": "Line1\r\nLine2", "new_text": "Line1\nLine2"}, "{--Line1\r\nLine2--}{++Line1\nLine2++}"),
        ({"target_text": "null", "new_text": "None"}, "{--null--}{++None++}"),
        ({"target_text": "a", "new_text": "b", "edit_index": 0, "include_index": True}, "{--a--}{++b++}{>>[Edit:0]<<}"),
        (
            {"target_text": "a", "new_text": "b", "edit_index": 99999, "include_index": True},
            "{--a--}{++b++}{>>[Edit:99999]<<}",
        ),
        ({"target_text": "old", "new_text": "new", "comment": "   "}, "{--old--}{++new++}{>>   <<}"),
        ({"target_text": "old", "new_text": "new", "comment": ""}, "{--old--}{++new++}"),
        ({"target_text": "", "new_text": "ignored", "highlight_only": True}, ""),
    ],
)
def test_build_critic_markup_parametrized(params, expected):
    """Unified test for _build_critic_markup covering basic, advanced, and edge cases."""
    full_params = {
        "target_text": "",
        "new_text": "",
        "comment": None,
        "edit_index": 0,
        "include_index": False,
        "highlight_only": False,
    }
    full_params.update(params)
    result = _build_critic_markup(**full_params)
    if full_params["highlight_only"] and not full_params["target_text"]:
        assert result in ("", "{====}")
    else:
        assert result == expected


class TestApplyEditsToMarkdown:
    """Tests for the main transformation function using parametrization where appropriate."""

    @pytest.mark.parametrize(
        "text, target, new, expected",
        [
            (
                "Notice of Termination",
                "Notice of Termination",
                "Notice of Immediate Termination",
                "Notice of {++Immediate ++}Termination",
            ),
            ("Hello World", "Hello World", "Hello Universe", "Hello {--World--}{++Universe++}"),
            ("Old Item", "Old Item", "New Item", "{--Old--}{++New++} Item"),
            ("Original text", "none", "none", "Original text"),  # No edits applied
            ("Remove this word please.", "this ", "", "Remove {--this --}word please."),
            ("The quick brown fox.", "quick", "slow", "{--quick--}{++slow++}"),
            ("Hello world.", "world", "universe", "{--world--}{++universe++}"),
            ("Start of text.", "Start", "Beginning", "{--Start--}{++Beginning++}"),
            ("End of text.", "text.", "document.", "{--text.--}{++document.++}"),
            ("", "x", "y", ""),  # Empty text
            ("Price is $100.00 (USD).", "$100.00", "$200.00", "{--$100.00--}{++$200.00++}"),
            ("Use {curly} and [square] brackets.", "{curly}", "{braces}", "{--{curly}--}{++{braces}++}"),
            ("Héllo wörld 你好", "wörld", "world", "{--wörld--}{++world++}"),
        ],
    )
    def test_apply_edits_basic_and_edge(self, text, target, new, expected):
        if target == "none":
            result = apply_edits_to_markdown(text, [])
        else:
            result = apply_edits_to_markdown(text, [ModifyText(target_text=target, new_text=new)])
        assert expected in result

    def test_modification_with_comment(self):
        text = "The quick brown fox."
        edits = [ModifyText(target_text="quick", new_text="slow", comment="Speed change")]
        result = apply_edits_to_markdown(text, edits)
        assert "{--quick--}{++slow++}{>>Speed change<<}" in result

    def test_modification_with_index(self):
        text = "Hello world."
        edits = [ModifyText(target_text="world", new_text="universe")]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        # 1-based, matching apply's "Edit N" reports (QA 2026-07-17 F10).
        assert "{--world--}{++universe++}{>>[Edit:1]<<}" in result

    def test_highlight_only_mode(self):
        text = "Highlight this section please."
        edits = [ModifyText(target_text="this section", new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)
        assert "{==this section==}" in result
        assert "{--" not in result
        assert "{++" not in result

    def test_highlight_with_comment_and_index(self):
        text = "Mark this text."
        edits = [ModifyText(target_text="this text", new_text="ignored", comment="Review needed")]
        result = apply_edits_to_markdown(text, edits, include_index=True, highlight_only=True)
        assert "{==this text==}{>>Review needed [Edit:1]<<}" in result

    def test_multiple_edits_non_overlapping(self):
        text = "First word and second word."
        edits = [
            ModifyText(target_text="First", new_text="1st"),
            ModifyText(target_text="second", new_text="2nd"),
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--First--}{++1st++}" in result
        assert "{--second--}{++2nd++}" in result

    def test_multiple_edits_preserve_order(self):
        text = "A B C"
        edits = [
            ModifyText(target_text="A", new_text="X"),
            ModifyText(target_text="B", new_text="Y"),
            ModifyText(target_text="C", new_text="Z"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        assert "[Edit:1]" in result and "[Edit:2]" in result and "[Edit:3]" in result
        assert result.find("{++X++}") < result.find("{++Y++}") < result.find("{++Z++}")

    def test_overlapping_edits_first_wins(self):
        text = "The quick brown fox"
        edits = [
            ModifyText(target_text="quick brown", new_text="slow red"),
            ModifyText(target_text="brown fox", new_text="green dog"),
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--quick brown--}{++slow red++}" in result
        assert "green dog" not in result

    def test_target_not_found_skipped(self):
        text = "Hello world."
        edits = [
            ModifyText(target_text="nonexistent", new_text="replacement"),
            ModifyText(target_text="world", new_text="universe"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        # The skipped edit still owns batch position 1, so this is Edit 2.
        assert "{--world--}{++universe++}{>>[Edit:2]<<}" in result

    def test_first_occurrence_only(self):
        # Strict (the default) refuses ambiguity, exactly like apply
        # (QA 2026-07-18 M1); "first" is the explicit opt-in.
        text = "word word word"
        strict_edits = [ModifyText(target_text="word", new_text="WORD")]
        reports = []
        result = apply_edits_to_markdown(text, strict_edits, edit_reports=reports)
        assert result == text
        assert reports[0]["status"] == "failed"
        assert "Ambiguous" in reports[0]["error"]

        edits = [ModifyText(target_text="word", new_text="WORD", match_mode="first")]
        result = apply_edits_to_markdown(text, edits)
        assert result.count("{--word--}") == 1
        assert result == "{--word--}{++WORD++} word word"

    def test_pure_insertion_skipped_in_text_mode(self):
        text = "Hello world."
        edits = [ModifyText(target_text="", new_text="NEW "), ModifyText(target_text="world", new_text="universe")]
        result = apply_edits_to_markdown(text, edits)
        assert "NEW" not in result
        assert "{--world--}{++universe++}" in result

    @pytest.mark.parametrize(
        "text, target, expected_match",
        [
            ("hello   world", "hello world", "{--hello   world--}"),
            ("Sign here: [__________]", "[___]", "{--[__________]--}"),
            ('"Hello" said the fox.', '"Hello"', '{--"Hello"--}'),
        ],
    )
    def test_fuzzy_and_smart_quotes(self, text, target, expected_match):
        result = apply_edits_to_markdown(text, [ModifyText(target_text=target, new_text="replacement")])
        assert expected_match in result

    def test_complex_legal_scenario(self):
        text = (
            "# Service Agreement\nThe Tenant shall pay rent monthly.\n"
            + "## Termination\nEither party may terminate with 30 days notice."
        )
        edits = [
            ModifyText(target_text="Tenant", new_text="Lessee", comment="Standardizing terminology"),
            ModifyText(target_text="30 days", new_text="60 days", comment="Extended notice period"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        assert "{--Tenant--}{++Lessee++}{>>Standardizing terminology [Edit:1]<<}" in result
        assert "{--30--}{++60++}{>>Extended notice period [Edit:2]<<} days" in result

    @pytest.mark.parametrize(
        "text, target, new, expected",
        [
            (
                "The **quick brown fox** jumped.",
                "quick brown fox",
                "slow red dog",
                "The **{--quick brown fox--}{++slow red dog++}** jumped.",
            ),
            ("This is _emphasized_ text.", "emphasized", "highlighted", "_{--emphasized--}{++highlighted++}_"),
            ("This is **_very important_** indeed.", "very important", "extremely critical", "extremely critical"),
            ("Variable __init__ is special.", "__init__", "__setup__", "__{--init--}{++setup++}__"),
            ("The **Vendor** shall pay.", "The Vendor shall", "ignored", "{==The **Vendor** shall==}"),
            ("**Note:** Prices are net.", "Prices are net", "ignored", "**Note:** {==Prices are net==}."),
        ],
    )
    def test_formatting_noise_and_preservation(self, text, target, new, expected):
        highlight = new == "ignored"
        result = apply_edits_to_markdown(text, [ModifyText(target_text=target, new_text=new)], highlight_only=highlight)
        assert expected in result

    def test_multiple_edits_same_formatting(self):
        text = "**Bold word1 and word2 here**"
        edits = [ModifyText(target_text="word1", new_text="WORD1"), ModifyText(target_text="word2", new_text="WORD2")]
        result = apply_edits_to_markdown(text, edits)
        assert "WORD1" in result and "WORD2" in result

    @pytest.mark.parametrize(
        "text, target, expected_substring",
        [
            ("Payment of $1,000.00 (USD) due.", "$1,000.00", "{--$1,000.00--}"),
            ("Interest rate of 5.5% per annum.", "5.5%", "{--5.5%--}"),
            ("Contact: john.doe@example.com", "john.doe@example.com", "{--john.doe@example.com--}"),
            (
                "Visit https://old-site.com/page for info.",
                "https://old-site.com/page",
                "{--https://old-site.com/page--}",
            ),
        ],
    )
    def test_special_content_types(self, text, target, expected_substring):
        result = apply_edits_to_markdown(text, [ModifyText(target_text=target, new_text="replacement")])
        assert expected_substring in result

    def test_same_word_multiple_occurrences(self):
        text = "The fee shall be paid. The fee is non-refundable. The fee covers all services."
        # Ambiguous strict target fails like apply (QA 2026-07-18 M1)…
        result = apply_edits_to_markdown(text, [ModifyText(target_text="fee", new_text="payment")])
        assert result == text
        # …match_mode="first" marks only the first occurrence…
        result = apply_edits_to_markdown(text, [ModifyText(target_text="fee", new_text="payment", match_mode="first")])
        assert result.count("{--fee--}") == 1
        assert result.startswith("The {--fee--}{++payment++}")
        # …and match_mode="all" marks every occurrence.
        result = apply_edits_to_markdown(text, [ModifyText(target_text="fee", new_text="payment", match_mode="all")])
        assert result.count("{--fee--}{++payment++}") == 3

    def test_very_long_text_performance(self):
        text = "word " * 10000 + "TARGET"
        result = apply_edits_to_markdown(text, [ModifyText(target_text="TARGET", new_text="FOUND")])
        assert "{--TARGET--}" in result
