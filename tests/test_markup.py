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


class TestBuildCriticMarkupAdvanced:
    """Advanced tests for CriticMarkup generation with markdown formatting."""

    def test_target_with_bold_markers_modification(self):
        """Target contains **bold** markers, should preserve them correctly."""
        result = _build_critic_markup(
            target_text="**Important**",
            new_text="**Critical**",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Bold markers should be outside the critic markup
        assert result == "**{--Important--}{++Critical++}**"

    def test_target_with_italic_markers_modification(self):
        """Target contains _italic_ markers."""
        result = _build_critic_markup(
            target_text="_emphasis_",
            new_text="_strong emphasis_",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert result == "_{--emphasis--}{++strong emphasis++}_"

    def test_target_with_nested_bold_italic(self):
        """Target contains **_nested_** formatting."""
        result = _build_critic_markup(
            target_text="**_nested_**",
            new_text="**_deeply nested_**",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Outer markers extracted, inner content modified
        assert "**" in result
        assert "{--" in result
        assert "{++" in result

    def test_unbalanced_markers_not_stripped(self):
        """Unbalanced markers like **text should NOT be stripped."""
        result = _build_critic_markup(
            target_text="**unbalanced",
            new_text="**still unbalanced",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should include the ** in the deletion/insertion
        assert "{--**unbalanced--}" in result
        assert "{++**still unbalanced++}" in result

    def test_underscore_content_not_treated_as_italic(self):
        """Underscores like __0__ are content, not formatting."""
        result = _build_critic_markup(
            target_text="__0__",
            new_text="__1__",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should NOT strip underscores as formatting
        assert "{--__0__--}" in result
        assert "{++__1__++}" in result

    def test_placeholder_brackets_preserved(self):
        """Legal placeholders [___] should be preserved as-is."""
        result = _build_critic_markup(
            target_text="Sign: [___]",
            new_text="Sign: John Doe",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Sign: [___]--}" in result
        assert "{++Sign: John Doe++}" in result

    def test_highlight_with_bold_markers(self):
        """Highlight mode with bold target."""
        result = _build_critic_markup(
            target_text="**Term**",
            new_text="ignored",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=True,
        )
        # Bold outside, highlight inside
        assert result == "**{==Term==}**"

    def test_highlight_with_italic_markers(self):
        """Highlight mode with italic target."""
        result = _build_critic_markup(
            target_text="_definition_",
            new_text="ignored",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=True,
        )
        assert result == "_{==definition==}_"

    def test_empty_target_and_new_produces_nothing(self):
        """Both empty should produce minimal output."""
        result = _build_critic_markup(
            target_text="",
            new_text="",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # No deletion or insertion markers for empty
        assert "{--" not in result
        assert "{++" not in result

    def test_whitespace_only_target(self):
        """Target is only whitespace."""
        result = _build_critic_markup(
            target_text="   ",
            new_text="text",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--   --}" in result
        assert "{++text++}" in result

    def test_newlines_in_target(self):
        """Target contains newlines."""
        result = _build_critic_markup(
            target_text="Line1\nLine2",
            new_text="SingleLine",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Line1\nLine2--}" in result
        assert "{++SingleLine++}" in result

    def test_special_critic_markup_chars_in_content(self):
        """Content contains characters used in CriticMarkup syntax."""
        result = _build_critic_markup(
            target_text="Use {curly} braces",
            new_text="Use [square] brackets",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Use {curly} braces--}" in result
        assert "{++Use [square] brackets++}" in result

    def test_double_dash_in_content(self):
        """Content contains -- which is CriticMarkup deletion marker."""
        result = _build_critic_markup(
            target_text="A--B",
            new_text="A-B",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # The -- inside should not break parsing
        assert "{--A--B--}" in result
        assert "{++A-B++}" in result

    def test_double_plus_in_content(self):
        """Content contains ++ which is CriticMarkup insertion marker."""
        result = _build_critic_markup(
            target_text="C++",
            new_text="Python",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--C++--}" in result
        assert "{++Python++}" in result

    def test_comment_with_special_chars(self):
        """Comment contains special characters."""
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment="Check {this} & <that>",
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{>>Check {this} & <that><<}" in result

    def test_very_long_target(self):
        """Very long target text should work."""
        long_text = "word " * 100
        result = _build_critic_markup(
            target_text=long_text,
            new_text="short",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert f"{{--{long_text}--}}" in result
        assert "{++short++}" in result

    def test_unicode_in_all_fields(self):
        """Unicode characters in target, new, and comment."""
        result = _build_critic_markup(
            target_text="日本語",
            new_text="中文",
            comment="Changed: 한국어",
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--日本語--}" in result
        assert "{++中文++}" in result
        assert "{>>Changed: 한국어<<}" in result

    def test_emoji_content(self):
        """Emoji in content."""
        result = _build_critic_markup(
            target_text="Hello 👋",
            new_text="Goodbye 👋",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Hello 👋--}" in result
        assert "{++Goodbye 👋++}" in result

    def test_html_like_content(self):
        """Content that looks like HTML tags."""
        result = _build_critic_markup(
            target_text="<div>content</div>",
            new_text="<span>content</span>",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--<div>content</div>--}" in result
        assert "{++<span>content</span>++}" in result

    def test_multiple_bold_sections(self):
        """Target with multiple separate bold sections."""
        result = _build_critic_markup(
            target_text="**A** and **B**",
            new_text="**X** and **Y**",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Cannot extract outer markers when there are multiple
        assert "{--**A** and **B**--}" in result
        assert "{++**X** and **Y**++}" in result

    def test_asterisks_as_content_not_formatting(self):
        """Asterisks used as content (math: 2*3*4)."""
        result = _build_critic_markup(
            target_text="2*3*4",
            new_text="2*4*6",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should not try to strip as italic
        assert "{--2*3*4--}" in result
        assert "{++2*4*6++}" in result

    def test_legal_clause_reference(self):
        """Legal document clause reference format."""
        result = _build_critic_markup(
            target_text="Section 3.2(a)(i)",
            new_text="Section 4.1(b)(ii)",
            comment="Updated reference",
            edit_index=5,
            include_index=True,
            highlight_only=False,
        )
        assert "{--Section 3.2(a)(i)--}" in result
        assert "{++Section 4.1(b)(ii)++}" in result
        assert "{>>Updated reference [Edit:5]<<}" in result


class TestBuildCriticMarkupEdgeCases:
    """Edge cases and potential failure modes."""

    def test_only_markers_no_content(self):
        """Target is just formatting markers with no content."""
        result = _build_critic_markup(
            target_text="****",  # Empty bold
            new_text="text",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should handle gracefully
        assert "{++" in result or "{--" in result

    def test_mismatched_markers(self):
        """Mismatched opening/closing markers."""
        result = _build_critic_markup(
            target_text="**bold_",  # Mismatched
            new_text="fixed",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--**bold_--}" in result
        assert "{++fixed++}" in result

    def test_triple_underscore(self):
        """Triple underscore edge case."""
        result = _build_critic_markup(
            target_text="___",
            new_text="---",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--___--}" in result
        assert "{++---++}" in result

    def test_alternating_markers(self):
        """Alternating bold/italic like _*text*_."""
        result = _build_critic_markup(
            target_text="_*text*_",
            new_text="_*other*_",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should handle without crashing
        assert "{--" in result
        assert "{++" in result

    def test_backslash_content(self):
        """Backslashes in content (common in paths)."""
        result = _build_critic_markup(
            target_text="C:\\Users\\file.txt",
            new_text="C:\\Documents\\file.txt",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--C:\\Users\\file.txt--}" in result
        assert "{++C:\\Documents\\file.txt++}" in result

    def test_regex_special_chars(self):
        """Characters that are special in regex."""
        result = _build_critic_markup(
            target_text="Price: $100.00 (USD)",
            new_text="Price: $200.00 (EUR)",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Price: $100.00 (USD)--}" in result
        assert "{++Price: $200.00 (EUR)++}" in result

    def test_tab_characters(self):
        """Tab characters in content."""
        result = _build_critic_markup(
            target_text="Col1\tCol2",
            new_text="Column1\tColumn2",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Col1\tCol2--}" in result
        assert "{++Column1\tColumn2++}" in result

    def test_carriage_return(self):
        """Windows-style line endings."""
        result = _build_critic_markup(
            target_text="Line1\r\nLine2",
            new_text="Line1\nLine2",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--Line1\r\nLine2--}" in result
        assert "{++Line1\nLine2++}" in result

    def test_null_like_strings(self):
        """Strings that might be confused with null/None."""
        result = _build_critic_markup(
            target_text="null",
            new_text="None",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        assert "{--null--}" in result
        assert "{++None++}" in result

    def test_index_zero(self):
        """Edit index 0 should work correctly."""
        result = _build_critic_markup(
            target_text="a",
            new_text="b",
            comment=None,
            edit_index=0,
            include_index=True,
            highlight_only=False,
        )
        assert "[Edit:0]" in result

    def test_large_index(self):
        """Large edit index."""
        result = _build_critic_markup(
            target_text="a",
            new_text="b",
            comment=None,
            edit_index=99999,
            include_index=True,
            highlight_only=False,
        )
        assert "[Edit:99999]" in result

    def test_comment_only_spaces(self):
        """Comment that is only spaces."""
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment="   ",
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Should include the spaces in comment block
        assert "{>>   <<}" in result

    def test_empty_comment_string(self):
        """Empty string comment (different from None)."""
        result = _build_critic_markup(
            target_text="old",
            new_text="new",
            comment="",
            edit_index=0,
            include_index=False,
            highlight_only=False,
        )
        # Empty comment should not create comment block
        assert "{>>" not in result

    def test_highlight_empty_target(self):
        """Highlight mode with empty target should handle gracefully."""
        result = _build_critic_markup(
            target_text="",
            new_text="ignored",
            comment=None,
            edit_index=0,
            include_index=False,
            highlight_only=True,
        )
        # Should produce empty highlight or nothing
        assert result == "{====}" or result == ""


class TestApplyEditsToMarkdownRobust:
    """Robust tests for the full apply_edits_to_markdown pipeline."""

    def test_bold_in_document_plain_in_target_multiword(self):
        """Multi-word bold phrase matched with plain target."""
        text = "The **quick brown fox** jumped."
        edits = [DocumentEdit(target_text="quick brown fox", new_text="slow red dog")]
        result = apply_edits_to_markdown(text, edits)

        # When target doesn't include **, the ** markers stay in document at their positions
        # and the inner text is modified. This is correct behavior.
        assert result == "The **{--quick brown fox--}{++slow red dog++}** jumped."

    def test_italic_target_plain_input(self):
        """Document has _italic_, user targets 'italic' without markers."""
        text = "This is _emphasized_ text."
        edits = [DocumentEdit(target_text="emphasized", new_text="highlighted")]
        result = apply_edits_to_markdown(text, edits)

        assert "_{--emphasized--}{++highlighted++}_" in result

    def test_nested_formatting_target(self):
        """Document has **_nested_** formatting."""
        text = "This is **_very important_** indeed."
        edits = [DocumentEdit(target_text="very important", new_text="extremely critical")]
        result = apply_edits_to_markdown(text, edits)

        assert "extremely critical" in result
        assert "{--" in result

    def test_multiple_bold_sections_target_one(self):
        """Multiple bold sections, target only one."""
        text = "**Section A** and **Section B** are different."
        edits = [DocumentEdit(target_text="Section A", new_text="Part 1")]
        result = apply_edits_to_markdown(text, edits)

        # Should only modify Section A
        assert "Part 1" in result
        assert "**Section B**" in result  # Unchanged

    def test_underscore_placeholder_variable_length(self):
        """[___] should match [__________]."""
        text = "Signature: [__________]"
        edits = [DocumentEdit(target_text="[___]", new_text="John Smith")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--[__________]--}" in result
        assert "{++John Smith++}" in result

    def test_literal_underscores_exact_match(self):
        """Literal __text__ should match exactly, not as bold."""
        text = "Variable __init__ is special."
        edits = [DocumentEdit(target_text="__init__", new_text="__setup__")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--__init__--}" in result
        assert "{++__setup__++}" in result

    def test_code_like_content(self):
        """Content that looks like code with underscores."""
        text = "Call my_function_name() here."
        edits = [DocumentEdit(target_text="my_function_name", new_text="new_func")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--my_function_name--}" in result
        assert "{++new_func++}" in result

    def test_smart_quotes_in_document(self):
        """Document has smart quotes, target has straight quotes."""
        text = 'He said "Hello" to her.'
        edits = [DocumentEdit(target_text='"Hello"', new_text='"Hi"')]
        result = apply_edits_to_markdown(text, edits)

        assert '{--"Hello"--}' in result
        assert '{++"Hi"++}' in result

    def test_smart_apostrophe(self):
        """Smart apostrophe matching."""
        text = "It's a nice day."
        edits = [DocumentEdit(target_text="It's", new_text="It is")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--It's--}" in result
        assert "{++It is++}" in result

    def test_extra_whitespace_in_document(self):
        """Document has extra whitespace."""
        text = "Hello    world"
        edits = [DocumentEdit(target_text="Hello world", new_text="Hi earth")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--Hello    world--}" in result
        assert "{++Hi earth++}" in result

    def test_header_markdown_preserved(self):
        """Markdown headers should be preserved."""
        text = "# Main Title\n\nSome content here."
        edits = [DocumentEdit(target_text="Some content", new_text="Different content")]
        result = apply_edits_to_markdown(text, edits)

        assert "# Main Title" in result
        assert "{--Some content--}" in result

    def test_multiple_edits_same_formatting(self):
        """Multiple edits in same formatted region."""
        text = "**Bold word1 and word2 here**"
        edits = [
            DocumentEdit(target_text="word1", new_text="WORD1"),
            DocumentEdit(target_text="word2", new_text="WORD2"),
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "WORD1" in result
        assert "WORD2" in result

    def test_edit_spans_formatting_boundary(self):
        """Edit target spans from plain to bold."""
        text = "Plain text **bold text** more plain"
        edits = [DocumentEdit(target_text="text **bold", new_text="TEXT BOLD")]
        result = apply_edits_to_markdown(text, edits)

        # Should handle crossing boundary
        assert "TEXT BOLD" in result or "{++" in result

    def test_legal_section_reference(self):
        """Legal document section references."""
        text = "As per Section 3.2(a)(i), the Party shall..."
        edits = [
            DocumentEdit(
                target_text="Section 3.2(a)(i)",
                new_text="Section 4.1(b)(ii)",
                comment="Updated cross-reference",
            )
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "{--Section 3.2(a)(i)--}" in result
        assert "{++Section 4.1(b)(ii)++}" in result
        assert "Updated cross-reference" in result

    def test_defined_term_with_quotes(self):
        """Legal defined term in quotes."""
        text = 'The "Effective Date" shall be January 1.'
        edits = [DocumentEdit(target_text='"Effective Date"', new_text='"Commencement Date"')]
        result = apply_edits_to_markdown(text, edits)

        assert "Commencement Date" in result

    def test_currency_amounts(self):
        """Currency with special characters."""
        text = "Payment of $1,000.00 (USD) due."
        edits = [DocumentEdit(target_text="$1,000.00", new_text="$2,500.00")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--$1,000.00--}" in result
        assert "{++$2,500.00++}" in result

    def test_percentage_values(self):
        """Percentage values."""
        text = "Interest rate of 5.5% per annum."
        edits = [DocumentEdit(target_text="5.5%", new_text="6.25%")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--5.5%--}" in result
        assert "{++6.25%++}" in result

    def test_date_formats(self):
        """Various date formats."""
        text = "Dated: December 31, 2024"
        edits = [DocumentEdit(target_text="December 31, 2024", new_text="January 15, 2025")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--December 31, 2024--}" in result
        assert "{++January 15, 2025++}" in result

    def test_email_addresses(self):
        """Email address modification."""
        text = "Contact: john.doe@example.com"
        edits = [DocumentEdit(target_text="john.doe@example.com", new_text="jane.smith@company.org")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--john.doe@example.com--}" in result
        assert "{++jane.smith@company.org++}" in result

    def test_url_modification(self):
        """URL modification."""
        text = "Visit https://old-site.com/page for info."
        edits = [
            DocumentEdit(
                target_text="https://old-site.com/page",
                new_text="https://new-site.com/updated",
            )
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "{--https://old-site.com/page--}" in result
        assert "{++https://new-site.com/updated++}" in result

    def test_parenthetical_content(self):
        """Content in parentheses."""
        text = "The Company (hereinafter referred to as 'Vendor')"
        edits = [DocumentEdit(target_text="'Vendor'", new_text="'Supplier'")]
        result = apply_edits_to_markdown(text, edits)

        assert "Supplier" in result

    def test_numbered_list_item(self):
        """Numbered list items."""
        text = "1. First item\n2. Second item\n3. Third item"
        edits = [DocumentEdit(target_text="Second item", new_text="Modified item")]
        result = apply_edits_to_markdown(text, edits)

        assert "1. First item" in result
        assert "{--Second item--}" in result
        assert "3. Third item" in result

    def test_bullet_list_item(self):
        """Bullet list items."""
        text = "- Item A\n- Item B\n- Item C"
        edits = [DocumentEdit(target_text="Item B", new_text="Changed B")]
        result = apply_edits_to_markdown(text, edits)

        assert "- Item A" in result
        assert "{--Item B--}" in result
        assert "- Item C" in result

    def test_table_like_content(self):
        """Pipe-separated table content."""
        text = "| Header1 | Header2 |\n| Cell1 | Cell2 |"
        edits = [DocumentEdit(target_text="Cell1", new_text="NewCell1")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--Cell1--}" in result
        assert "{++NewCell1++}" in result
        assert "Header1" in result

    def test_blockquote_content(self):
        """Blockquote modification."""
        text = "> This is a quoted statement."
        edits = [DocumentEdit(target_text="quoted statement", new_text="referenced clause")]
        result = apply_edits_to_markdown(text, edits)

        assert "> " in result
        assert "{--quoted statement--}" in result


class TestApplyEditsToMarkdownComplexScenarios:
    """Complex real-world scenarios."""

    def test_contract_clause_modification(self):
        """Full contract clause modification."""
        text = (
            "## 3. PAYMENT TERMS\n\n"
            "The **Buyer** shall pay the **Seller** the sum of $10,000.00 "
            "(ten thousand dollars) within 30 days of invoice receipt."
        )

        edits = [
            DocumentEdit(target_text="Buyer", new_text="Purchaser"),
            DocumentEdit(target_text="Seller", new_text="Vendor"),
            DocumentEdit(target_text="$10,000.00", new_text="$15,000.00"),
            DocumentEdit(target_text="30 days", new_text="45 days"),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)

        assert "## 3. PAYMENT TERMS" in result
        assert "Purchaser" in result
        assert "Vendor" in result
        assert "$15,000.00" in result
        assert "45 days" in result
        assert "[Edit:0]" in result
        assert "[Edit:3]" in result

    def test_policy_document_with_definitions(self):
        """Policy document with defined terms."""
        text = """**"Employee"** means any individual employed by the Company.

**"Confidential Information"** includes all proprietary data."""

        edits = [
            DocumentEdit(
                target_text='"Employee"',
                new_text='"Staff Member"',
                comment="Broader terminology",
            ),
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "Staff Member" in result
        assert "Broader terminology" in result

    def test_technical_specification_update(self):
        """Technical spec with version numbers and measurements."""
        text = "System requires CPU >= 2.4GHz, RAM >= 8GB, Storage >= 256GB SSD."
        edits = [
            DocumentEdit(target_text="2.4GHz", new_text="3.0GHz"),
            DocumentEdit(target_text="8GB", new_text="16GB"),
            DocumentEdit(target_text="256GB", new_text="512GB"),
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "3.0GHz" in result
        assert "16GB" in result
        assert "512GB" in result

    def test_mixed_language_content(self):
        """Content with mixed languages."""
        text = "The term 'Force Majeure' (不可抗力) applies here."
        edits = [DocumentEdit(target_text="Force Majeure", new_text="Act of God")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--Force Majeure--}" in result
        assert "{++Act of God++}" in result
        assert "不可抗力" in result

    def test_highlight_mode_contract_review(self):
        """Highlight mode for contract review - mark areas for attention."""
        text = """The Tenant shall maintain insurance coverage of not less than $1,000,000.

The Landlord reserves the right to enter the premises with 24 hours notice."""

        edits = [
            DocumentEdit(
                target_text="$1,000,000",
                new_text="ignored",
                comment="Verify coverage amount",
            ),
            DocumentEdit(
                target_text="24 hours",
                new_text="ignored",
                comment="Consider extending notice period",
            ),
        ]
        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        assert "{==$1,000,000==}" in result
        assert "{==24 hours==}" in result
        assert "Verify coverage amount" in result
        assert "Consider extending notice period" in result
        # Should NOT have deletion/insertion markers
        assert "{--" not in result
        assert "{++" not in result

    def test_redline_with_all_features(self):
        """Full redline with indices and comments."""
        text = "Original clause with **important** terms."
        edits = [
            DocumentEdit(
                target_text="Original clause",
                new_text="Modified provision",
                comment="Clarified language",
            ),
            DocumentEdit(
                target_text="important",
                new_text="critical",
                comment="Strengthened terminology",
            ),
        ]
        result = apply_edits_to_markdown(text, edits, include_index=True)

        assert "{--Original clause--}" in result
        assert "{++Modified provision++}" in result
        assert "[Edit:0]" in result
        assert "Clarified language" in result
        assert "critical" in result
        assert "[Edit:1]" in result
        assert "Strengthened terminology" in result

    def test_deletion_entire_paragraph(self):
        """Delete entire paragraph."""
        text = """First paragraph stays.

This entire paragraph should be removed.

Third paragraph stays."""

        edits = [
            DocumentEdit(
                target_text="This entire paragraph should be removed.",
                new_text="",
                comment="Removed redundant clause",
            )
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "First paragraph stays." in result
        assert "{--This entire paragraph should be removed.--}" in result
        assert "Third paragraph stays." in result
        assert "Removed redundant clause" in result

    def test_insertion_new_clause(self):
        """Insert new clause (modification that adds content)."""
        text = "Section 1 content.\n\nSection 3 content."
        edits = [
            DocumentEdit(
                target_text="Section 1 content.",
                new_text="Section 1 content.\n\nSection 2: New inserted content.",
                comment="Added missing section",
            )
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "Section 2: New inserted content" in result
        assert "Added missing section" in result

    def test_complex_nested_structure(self):
        """Complex nested lists and formatting."""
        text = """## Article 5: Obligations

5.1 The **Contractor** shall:
   (a) perform services _diligently_;
   (b) maintain **confidentiality**;
   (c) comply with all _applicable laws_."""

        edits = [
            DocumentEdit(target_text="Contractor", new_text="Service Provider"),
            DocumentEdit(target_text="diligently", new_text="professionally"),
            DocumentEdit(target_text="confidentiality", new_text="strict confidentiality"),
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "Service Provider" in result
        assert "professionally" in result
        assert "strict confidentiality" in result
        assert "## Article 5" in result

    def test_overlapping_edits_handled(self):
        """Overlapping edits - first one wins."""
        text = "The quick brown fox jumps over."
        edits = [
            DocumentEdit(target_text="quick brown", new_text="slow red"),
            DocumentEdit(target_text="brown fox", new_text="gray wolf"),  # Overlaps!
        ]
        result = apply_edits_to_markdown(text, edits)

        # First edit should apply, second should be skipped
        assert "slow red" in result
        assert "gray wolf" not in result  # Skipped due to overlap

    def test_adjacent_non_overlapping_edits(self):
        """Adjacent but non-overlapping edits."""
        text = "WordA WordB WordC"
        edits = [
            DocumentEdit(target_text="WordA", new_text="TermA"),
            DocumentEdit(target_text="WordB", new_text="TermB"),
            DocumentEdit(target_text="WordC", new_text="TermC"),
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "TermA" in result
        assert "TermB" in result
        assert "TermC" in result

    def test_same_word_multiple_occurrences(self):
        """Same word appears multiple times - only first occurrence changed."""
        text = "The fee shall be paid. The fee is non-refundable. The fee covers all services."
        edits = [DocumentEdit(target_text="fee", new_text="payment")]
        result = apply_edits_to_markdown(text, edits)

        # Only first "fee" should be changed
        assert result.count("{--fee--}") == 1
        # Note: result.count("fee") == 3 because "{--fee--}" contains "fee"
        # Check that we have exactly 2 unchanged "fee" by verifying the structure
        assert result.count("{++payment++}") == 1
        # Verify structure: first fee is wrapped, others unchanged
        assert (
            result
            == "The {--fee--}{++payment++} shall be paid. The fee is non-refundable. The fee covers all services."
        )

    def test_case_sensitivity(self):
        """Matching should be case-sensitive by default."""
        text = "The Company and the company are different."
        edits = [DocumentEdit(target_text="Company", new_text="Corporation")]
        result = apply_edits_to_markdown(text, edits)

        # Should only match "Company" not "company"
        assert "{--Company--}" in result
        assert "{++Corporation++}" in result
        assert "the company" in result  # lowercase unchanged


class TestApplyEditsToMarkdownErrorHandling:
    """Error handling and edge cases."""

    def test_empty_document(self):
        """Empty document."""
        text = ""
        edits = [DocumentEdit(target_text="anything", new_text="something")]
        result = apply_edits_to_markdown(text, edits)

        assert result == ""

    def test_whitespace_only_document(self):
        """Document with only whitespace."""
        text = "   \n\n   \t   "
        edits = [DocumentEdit(target_text="text", new_text="other")]
        result = apply_edits_to_markdown(text, edits)

        assert result == text  # Unchanged

    def test_target_not_found(self):
        """Target not in document."""
        text = "Some actual content here."
        edits = [DocumentEdit(target_text="nonexistent phrase", new_text="replacement")]
        result = apply_edits_to_markdown(text, edits)

        assert result == text  # Unchanged
        assert "{--" not in result

    def test_empty_edits_list(self):
        """No edits to apply."""
        text = "Document content stays the same."
        result = apply_edits_to_markdown(text, [])

        assert result == text

    def test_none_values_in_edit(self):
        """Edit with None comment (common case)."""
        text = "Original text."
        edits = [DocumentEdit(target_text="Original", new_text="Modified", comment=None)]
        result = apply_edits_to_markdown(text, edits)

        assert "{--Original--}" in result
        assert "{++Modified++}" in result
        assert "{>>" not in result  # No comment block

    def test_very_long_document(self):
        """Performance with long document."""
        text = "Word " * 10000 + "TARGET " + "Word " * 10000
        edits = [DocumentEdit(target_text="TARGET", new_text="FOUND")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--TARGET--}" in result
        assert "{++FOUND++}" in result

    def test_many_edits(self):
        """Many edits at once."""
        text = " ".join([f"word{i}" for i in range(100)])
        edits = [
            DocumentEdit(target_text=f"word{i}", new_text=f"WORD{i}")
            for i in range(0, 100, 10)  # Every 10th word
        ]
        result = apply_edits_to_markdown(text, edits)

        assert "WORD0" in result
        assert "WORD50" in result
        assert "WORD90" in result

    def test_special_regex_chars_in_all_positions(self):
        """Regex special chars everywhere."""
        text = "Match (this) [and] {that} $100 ^start end$ 50% a+b a*b a?b"
        edits = [DocumentEdit(target_text="(this)", new_text="(THAT)")]
        result = apply_edits_to_markdown(text, edits)

        assert "{--(this)--}" in result
        assert "{++(THAT)++}" in result
