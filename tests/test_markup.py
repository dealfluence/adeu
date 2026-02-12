# FILE: tests/test_markup.py
"""
Tests for the pure text CriticMarkup transformation function.
"""

import re

from adeu.markup import (
    _build_critic_markup,
    _find_match_in_text,
    _make_fuzzy_regex,
    _replace_smart_quotes,
    apply_edits_to_markdown,
)
from adeu.models import DocumentEdit


class TestHelperFunctions:
    """Tests for internal helper functions."""

    def test_replace_smart_quotes(self):
        text = "\"Hello\" and 'World'"
        result = _replace_smart_quotes(text)
        assert result == "\"Hello\" and 'World'"

    def test_make_fuzzy_regex_whitespace(self):
        pattern = _make_fuzzy_regex("hello world")

        assert re.match(pattern, "hello world")
        assert re.match(pattern, "hello  world")
        assert re.match(pattern, "hello   world")

    def test_make_fuzzy_regex_underscores(self):
        pattern = _make_fuzzy_regex("[___]")

        assert re.match(pattern, "[___]")
        assert re.match(pattern, "[_____]")
        assert re.match(pattern, "[__________]")

    def test_find_match_exact(self):
        text = "The quick brown fox"
        start, end = _find_match_in_text(text, "quick")
        assert start == 4
        assert end == 9

    def test_find_match_smart_quotes(self):
        text = '"Hello" said the fox'
        start, end = _find_match_in_text(text, '"Hello"')
        assert start == 0
        assert end == 7

    def test_find_match_fuzzy_whitespace(self):
        text = "hello   world"
        start, end = _find_match_in_text(text, "hello world")
        assert start == 0
        assert end == 13  # Actual matched length

    def test_find_match_not_found(self):
        text = "The quick brown fox"
        start, end = _find_match_in_text(text, "elephant")
        assert start == -1
        assert end == -1

    def test_find_match_empty_target(self):
        text = "Some text"
        start, end = _find_match_in_text(text, "")
        assert start == -1
        assert end == -1


class TestBuildCriticMarkup:
    """Tests for the markup generation helper."""

    def test_deletion(self):
        result = _build_critic_markup(
            target_text="old",
            new_text="",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert result == "{--old--}"

    def test_insertion(self):
        result = _build_critic_markup(
            target_text="",
            new_text="new",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert result == "{++new++}"

    def test_modification(self):
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert result == "{--old--}{++new++}"

    def test_with_comment(self):
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment="Changed this",
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert result == "{--old--}{++new++}{>>Changed this<<}"

    def test_with_index(self):
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment=None,
            edit_index=3,
            include_index=True,
            highlight_only=False,
        )
        assert result == "{--old--}{++new++}{>>[Edit:3]<<}"

    def test_with_comment_and_index(self):
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment="Reason",
            edit_index=5,
            include_index=True,
            highlight_only=False,
        )
        assert result == "{--old--}{++new++}{>>Reason [Edit:5]<<}"

    def test_highlight_only(self):
        result = _build_critic_markup(
            target_text="target",
            new_text="ignored",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=True,
        )
        assert result == "{==target==}"

    def test_highlight_with_comment_and_index(self):
        result = _build_critic_markup(
            target_text="target",
            new_text="ignored",
            comment="Note",
            edit_index=2,
            include_index=True,
            highlight_only=True,
        )
        assert result == "{==target==}{>>Note [Edit:2]<<}"


class TestApplyEditsToMarkdown:
    """Tests for the main transformation function."""

    def test_empty_edits_returns_original(self):
        text = "Original text"
        result = apply_edits_to_markdown(text, [])
        assert result == text

    def test_basic_modification(self):
        text = "This is a Contract Agreement for services."
        edits = [
            DocumentEdit(
                target_text="Contract Agreement",
                new_text="Service Agreement",
            )
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--Contract Agreement--}" in result
        assert "{++Service Agreement++}" in result
        assert "This is a " in result
        assert " for services." in result

    def test_basic_deletion(self):
        text = "Remove this word please."
        edits = [DocumentEdit(target_text="this ", new_text="")]
        result = apply_edits_to_markdown(text, edits)
        assert result == "Remove {--this --}word please."

    def test_modification_with_comment(self):
        text = "The quick brown fox."
        edits = [
            DocumentEdit(
                target_text="quick",
                new_text="slow",
                comment="Speed change",
            )
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--quick--}{++slow++}{>>Speed change<<}" in result

    def test_modification_with_index(self):
        text = "Hello world."
        edits = [DocumentEdit(target_text="world", new_text="universe")]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        assert "{--world--}{++universe++}{>>[Edit:0]<<}" in result

    def test_highlight_only_mode(self):
        text = "Highlight this section please."
        edits = [DocumentEdit(target_text="this section", new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)
        assert "{==this section==}" in result
        assert "{--" not in result
        assert "{++" not in result

    def test_highlight_with_comment_and_index(self):
        text = "Mark this text."
        edits = [
            DocumentEdit(
                target_text="this text",
                new_text="ignored",
                comment="Review needed",
            )
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True, highlight_only=True)
        assert "{==this text==}{>>Review needed [Edit:0]<<}" in result

    def test_multiple_edits_non_overlapping(self):
        text = "First word and second word."
        edits = [
            DocumentEdit(target_text="First", new_text="1st"),
            DocumentEdit(target_text="second", new_text="2nd"),
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--First--}{++1st++}" in result
        assert "{--second--}{++2nd++}" in result

    def test_multiple_edits_preserve_order(self):
        text = "A B C"
        edits = [
            DocumentEdit(target_text="A", new_text="X"),
            DocumentEdit(target_text="B", new_text="Y"),
            DocumentEdit(target_text="C", new_text="Z"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        # Verify indices match original list order
        assert "[Edit:0]" in result
        assert "[Edit:1]" in result
        assert "[Edit:2]" in result
        # Verify positional order in output
        idx_x = result.find("{++X++}")
        idx_y = result.find("{++Y++}")
        idx_z = result.find("{++Z++}")
        assert idx_x < idx_y < idx_z

    def test_overlapping_edits_first_wins(self):
        text = "The quick brown fox"
        edits = [
            DocumentEdit(target_text="quick brown", new_text="slow red"),  # First in list
            DocumentEdit(target_text="brown fox", new_text="green dog"),  # Overlaps, should be skipped
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--quick brown--}{++slow red++}" in result
        assert "green dog" not in result
        # "fox" should remain unchanged
        assert " fox" in result

    def test_target_not_found_skipped(self):
        text = "Hello world."
        edits = [
            DocumentEdit(target_text="nonexistent", new_text="replacement"),
            DocumentEdit(target_text="world", new_text="universe"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)
        # First edit skipped, second applied with its original index
        assert "nonexistent" not in result
        assert "{--world--}{++universe++}{>>[Edit:1]<<}" in result

    def test_first_occurrence_only(self):
        text = "word word word"
        edits = [DocumentEdit(target_text="word", new_text="WORD")]
        result = apply_edits_to_markdown(text, edits)
        # Only first occurrence should be changed
        assert result.count("{--word--}") == 1
        assert result.count("{++WORD++}") == 1
        # Verify structure: changed + space + unchanged + space + unchanged
        assert result == "{--word--}{++WORD++} word word"

    def test_highlight_only_skips_missing_target(self):
        text = "Some text here."
        edits = [
            DocumentEdit(target_text="missing", new_text="anything"),
            DocumentEdit(target_text="text", new_text="ignored"),
        ]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)
        assert "{==text==}" in result
        assert "missing" not in result

    def test_pure_insertion_skipped_in_text_mode(self):
        """Pure insertions (empty target) are skipped in text transformation mode."""
        text = "Hello world."
        edits = [
            DocumentEdit(target_text="", new_text="NEW "),  # No anchor
            DocumentEdit(target_text="world", new_text="universe"),
        ]
        result = apply_edits_to_markdown(text, edits)
        # Pure insertion skipped
        assert "NEW" not in result
        # Regular edit applied
        assert "{--world--}{++universe++}" in result

    def test_fuzzy_matching_whitespace(self):
        text = "hello   world"
        edits = [DocumentEdit(target_text="hello world", new_text="hi earth")]
        result = apply_edits_to_markdown(text, edits)
        # Should match despite whitespace difference
        assert "{--hello   world--}" in result
        assert "{++hi earth++}" in result

    def test_fuzzy_matching_underscores(self):
        text = "Sign here: [__________]"
        edits = [DocumentEdit(target_text="[___]", new_text="John Doe")]
        result = apply_edits_to_markdown(text, edits)
        assert "{--[__________]--}" in result
        assert "{++John Doe++}" in result

    def test_smart_quote_matching(self):
        text = '"Hello" said the fox.'
        edits = [DocumentEdit(target_text='"Hello"', new_text='"Hi"')]
        result = apply_edits_to_markdown(text, edits)
        assert '{--"Hello"--}' in result
        assert '{++"Hi"++}' in result

    def test_multiline_text(self):
        text = "Line 1\n\nLine 2\n\nLine 3"
        edits = [DocumentEdit(target_text="Line 2", new_text="Modified Line")]
        result = apply_edits_to_markdown(text, edits)
        assert "Line 1\n\n{--Line 2--}{++Modified Line++}\n\nLine 3" == result

    def test_edit_at_start(self):
        text = "Start of text."
        edits = [DocumentEdit(target_text="Start", new_text="Beginning")]
        result = apply_edits_to_markdown(text, edits)
        assert result.startswith("{--Start--}{++Beginning++}")

    def test_edit_at_end(self):
        text = "End of text."
        edits = [DocumentEdit(target_text="text.", new_text="document.")]
        result = apply_edits_to_markdown(text, edits)
        assert result.endswith("{--text.--}{++document.++}")

    def test_complex_legal_scenario(self):
        """Simulates a real contract editing scenario."""
        text = """# Service Agreement

The Tenant shall pay rent monthly.

## Termination

Either party may terminate with 30 days notice."""

        edits = [
            DocumentEdit(
                target_text="Tenant",
                new_text="Lessee",
                comment="Standardizing terminology",
            ),
            DocumentEdit(
                target_text="30 days",
                new_text="60 days",
                comment="Extended notice period",
            ),
        ]

        result = apply_edits_to_markdown(text, edits, include_index=True)

        assert "{--Tenant--}{++Lessee++}{>>Standardizing terminology [Edit:0]<<}" in result
        assert "{--30 days--}{++60 days++}{>>Extended notice period [Edit:1]<<}" in result
        # Structure preserved
        assert "# Service Agreement" in result
        assert "## Termination" in result


class TestEdgeCases:
    """Edge cases and regression tests."""

    def test_empty_text_returns_empty(self):
        result = apply_edits_to_markdown("", [DocumentEdit(target_text="x", new_text="y")])
        assert result == ""

    def test_special_regex_chars_in_target(self):
        """Ensure regex special chars don't break matching."""
        text = "Price is $100.00 (USD)."
        edits = [DocumentEdit(target_text="$100.00", new_text="$200.00")]
        result = apply_edits_to_markdown(text, edits)
        assert "{--$100.00--}{++$200.00++}" in result

    def test_critic_markup_chars_in_text(self):
        """Text containing CriticMarkup-like chars should still work."""
        text = "Use {curly} and [square] brackets."
        edits = [DocumentEdit(target_text="{curly}", new_text="{braces}")]
        result = apply_edits_to_markdown(text, edits)
        assert "{--{curly}--}{++{braces}++}" in result

    def test_unicode_text(self):
        text = "Héllo wörld 你好"
        edits = [DocumentEdit(target_text="wörld", new_text="world")]
        result = apply_edits_to_markdown(text, edits)
        assert "{--wörld--}{++world++}" in result

    def test_very_long_text_performance(self):
        """Ensure reasonable performance on large documents."""
        text = "word " * 10000
        edits = [DocumentEdit(target_text="word", new_text="WORD")]
        # Should complete without timeout
        result = apply_edits_to_markdown(text, edits)
        assert "{--word--}{++WORD++}" in result

    def test_adjacent_edits(self):
        """Edits that are adjacent but not overlapping."""
        text = "ABCD"
        edits = [
            DocumentEdit(target_text="AB", new_text="XY"),
            DocumentEdit(target_text="CD", new_text="ZW"),
        ]
        result = apply_edits_to_markdown(text, edits)
        assert "{--AB--}{++XY++}" in result
        assert "{--CD--}{++ZW++}" in result


class TestMarkdownFormattingNoise:
    """Tests for matching plain text against Markdown-formatted source text."""

    def test_ignore_bold_markers(self):
        text = "**Período de Prueba:** Los primeros 90 días"
        # User provides plain text quote without **
        target = "Período de Prueba: Los"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        # Should match the bolded text and wrap it, keeping markers inside
        assert "{==**Período de Prueba:** Los==}" in result

    def test_ignore_italic_markers(self):
        text = "This is _very_ important."
        target = "is very important"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        assert "{==is _very_ important==}" in result

    def test_mixed_formatting_noise(self):
        text = "**Section 1** _Introduction_"
        target = "Section 1 Introduction"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        assert "{==**Section 1** _Introduction_==}" in result

    def test_formatting_inside_match(self):
        text = "The **Vendor** shall pay."
        target = "The Vendor shall"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        assert "{==The **Vendor** shall==}" in result

    def test_formatting_at_boundaries(self):
        # Case where match starts immediately after formatting
        text = "**Note:** Prices are net."
        target = "Prices are net"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        # Match should NOT include the bold markers or "Note:"
        assert "**Note:** {==Prices are net==}." in result

    def test_complex_policy_scenario(self):
        # Specific regression test for the user's provided example
        text = "## 1. PROPÓSITO\n\n**Período de Evaluación:** Fase inicial..."
        target = "Período de Evaluación: Fase inicial"

        edits = [DocumentEdit(target_text=target, new_text="ignored")]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        assert "{==**Período de Evaluación:** Fase inicial==}" in result
