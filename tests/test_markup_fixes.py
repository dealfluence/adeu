# FILE: tests/test_markup_fixes.py

from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentEdit


class TestMarkdownRobustness:
    def test_target_ignores_surrounding_bold(self):
        """
        Scenario: Document has **Bold**, user targets 'Bold'.
        The matching should find it, and the markup should wrap the inner text,
        preserving the outer bold markers.
        """
        text = "This is **Bold** text."
        edits = [DocumentEdit(target_text="Bold", new_text="Italic")]

        result = apply_edits_to_markdown(text, edits)

        # Expectation: The markers ** are preserved outside the modification
        # because the match finds 'Bold' inside.
        assert "This is **{--Bold--}{++Italic++}** text." == result

    def test_target_fuzzy_matches_across_formatting(self):
        """
        Scenario: Document has 'Fee.** A one-time', user targets 'Fee. A one-time'.
        The fuzzy regex should skip the '**' noise, and the result should handle
        the inclusion of the markers cleanly.
        """
        text = "Setup Fee.** A one-time fee."
        # User provides text without the bold markers that exist in doc
        target = "Fee. A one-time"

        edits = [DocumentEdit(target_text=target, new_text="")]

        result = apply_edits_to_markdown(text, edits)

        # The fuzzy matcher consumes "Fee.** A one-time".
        # _build_critic_markup attempts to strip leading/trailing markers if they are balanced.
        # Here, the match is "Fee.** A one-time". Markers are in the middle.
        # They should be included in the deletion block to ensure they are removed.
        assert "Setup {--Fee.** A one-time--} fee." == result

    def test_highlight_strips_outer_markers(self):
        """
        Scenario: Highlight-only mode on **Term**.
        We want **{==Term==}**, not {==**Term**==} which breaks some renderers,
        or worse **{==**Term**==}**.
        """
        text = "Define **Term** here."
        edits = [DocumentEdit(target_text="**Term**", new_text="ignored")]

        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        # The fix logic in _build_critic_markup strips outer markers
        assert "Define **{==Term==}** here." == result

    def test_highlight_fuzzy_bold_mismatch(self):
        """
        Scenario: Doc has **Term**, user targets 'Term'.
        """
        text = "Define **Term** here."
        edits = [DocumentEdit(target_text="Term", new_text="ignored")]

        result = apply_edits_to_markdown(text, edits, highlight_only=True)

        # Should match inside the bold tags
        assert "Define **{==Term==}** here." == result

    def test_overlapping_formatting_noise(self):
        """
        Scenario: 'Prefix **Suffix' matching 'Prefix Suffix'.
        This tests that the regex noise `(?:\\*\\*|__|\\*|_)*` works between words.
        """
        text = "Prefix **Suffix**"
        # User input ignores the bold start
        target = "Prefix Suffix"

        edits = [DocumentEdit(target_text=target, new_text="New")]

        result = apply_edits_to_markdown(text, edits)

        # Match should cover "Prefix **Suffix".
        # The trailing "**" is left alone because the target ended at Suffix.
        # However, to ensure balanced markdown, valid behavior is to consume the trailing **
        # so the result is `{--Prefix **Suffix**--}{++New++}` (clean replacement)
        # rather than leaving a dangling `**`.
        # Result: {--Prefix **Suffix**--}{++New++}
        assert "{--Prefix **Suffix**--}{++New++}" == result

    def test_unbalanced_marker_expansion_start(self):
        """
        Scenario: Match starts inside a bold block.
        Doc: "**Bold Text**"
        Target: "Bold"

        If the safe_boundaries logic expands aggressiveley (swallowing the whole block),
        this test documents that behavior. If it allows inner matching, it verifies that.
        """
        text = "**Bold Text**"
        edits = [DocumentEdit(target_text="Bold", new_text="Link")]

        result = apply_edits_to_markdown(text, edits)

        # We are inside. "Bold" match is balanced (0 markers).
        # We should NOT expand.
        # Expectation: **{--Bold--}{++Link++} Text**
        assert "**{--Bold--}{++Link++} Text**" == result

    def test_multi_line_header_match(self):
        """
        Scenario: Matching headers across newlines.
        """
        text = "# Header 1\n\n# Header 2"
        target = "# Header 1\n\n# Header 2"
        edits = [DocumentEdit(target_text=target, new_text="# H1\n\n# H2")]

        result = apply_edits_to_markdown(text, edits)

        assert "{--# Header 1\n\n# Header 2--}{++# H1\n\n# H2++}" == result

    def test_partial_underscore_fuzzy(self):
        """
        Scenario: '[___]' in text, user targets '[_]'.
        """
        text = "Sign: [_______]"
        edits = [DocumentEdit(target_text="[_]", new_text="Signed")]

        result = apply_edits_to_markdown(text, edits)

        # Fuzzy regex handles variable underscores
        assert "Sign: {--[_______]--}{++Signed++}" == result
