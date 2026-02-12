# FILE: src/adeu/markup.py

import re
from typing import List, Optional, Tuple

import structlog

from adeu.models import DocumentEdit

logger = structlog.get_logger(__name__)


def _should_strip_markers(text: str, marker: str) -> bool:
    """
    Determines if outer markers should be stripped from text.
    Only strip if:
    1. Text starts AND ends with the marker (balanced)
    2. The inner content is "prose-like" (not a code identifier or pure symbols)
    3. There are no additional marker pairs inside (e.g., "**A** and **B**" should NOT strip)
    """
    import re

    if not text.startswith(marker) or not text.endswith(marker):
        return False

    if len(text) < len(marker) * 2:
        return False

    inner = text[len(marker) : -len(marker)]

    if not inner:
        return False

    # Check for additional markers inside - if present, don't strip
    # e.g., "**A** and **B**" has "A** and **B" inside, which contains **
    if marker in inner:
        return False

    # Inner content must have actual letter characters (not just digits/underscores/symbols)
    # This prevents stripping things like "___", "__0__"
    if not re.search(r"[a-zA-Z]", inner):
        return False

    # For double-underscore (__), be conservative:
    # Don't strip if inner looks like a code identifier (e.g., "init", "name", "main")
    # Code identifiers: only word chars, no spaces
    if marker == "__":
        # If inner has no spaces and is only word characters, it's likely code like __init__
        if re.fullmatch(r"\w+", inner):
            return False

    # For single underscore (_), only skip if it looks like snake_case (contains inner underscore)
    # _emphasis_ -> strip (prose)
    # _some_var_ -> don't strip (code identifier)
    if marker == "_":
        # If inner contains underscore, likely snake_case code identifier
        if "_" in inner:
            return False
        # If inner is a single word with no letters (like just digits), don't strip
        if re.fullmatch(r"[0-9_]+", inner):
            return False

    return True


def _strip_balanced_markers(text: str) -> tuple[str, str, str]:
    """
    Strips balanced outer formatting markers from text.
    Returns (prefix_markup, clean_text, suffix_markup).

    Only strips if the markers are truly formatting (content has word chars).
    """
    prefix_markup = ""
    suffix_markup = ""
    clean_text = text

    # Check markers in order of length (longer first to avoid ** vs * conflicts)
    markers = ["**", "__", "_", "*"]

    for marker in markers:
        if _should_strip_markers(clean_text, marker):
            prefix_markup += marker
            suffix_markup = marker + suffix_markup
            clean_text = clean_text[len(marker) : -len(marker)]
            # Only strip one level of markers
            break

    return prefix_markup, clean_text, suffix_markup


def _replace_smart_quotes(text: str) -> str:
    """Normalizes smart quotes to ASCII equivalents."""
    return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")


def _strip_markdown_for_matching(text: str) -> Tuple[str, List[int]]:
    """
    Strips markdown formatting markers and builds a position map.
    Returns (stripped_text, position_map) where position_map[i] = original index.
    """
    result = []
    position_map = []
    i = 0

    while i < len(text):
        # Skip ** or __
        if i < len(text) - 1 and text[i : i + 2] in ("**", "__"):
            i += 2
            continue
        # Skip single * or _ that look like markdown (at word boundaries)
        if text[i] in ("*", "_"):
            prev_char = text[i - 1] if i > 0 else " "
            next_char = text[i + 1] if i < len(text) - 1 else " "
            # If at boundary (space or start/end), likely markdown
            if prev_char in (" ", "\n", "\t") or next_char in (" ", "\n", "\t"):
                i += 1
                continue

        position_map.append(i)
        result.append(text[i])
        i += 1

    return "".join(result), position_map


def _find_safe_boundaries(text: str, start: int, end: int) -> Tuple[int, int]:
    """
    Adjusts match boundaries to avoid splitting markdown formatting tokens.
    Ensures that if we consume an opening marker, we also consume the closing one,
    keeping the replacement balanced.
    """
    new_start = start
    new_end = end

    def expand_if_unbalanced(marker: str):
        nonlocal new_start, new_end

        # Get current match content
        current_match = text[new_start:new_end]

        # Check if unbalanced (odd number of markers)
        if current_match.count(marker) % 2 != 0:
            # Look in suffix first (most common case for regex consuming opening tag)
            suffix = text[new_end:]
            if suffix.startswith(marker):
                new_end += len(marker)
                return  # Re-evaluate? For now assuming simple adjacency

            # Look in prefix
            prefix = text[:new_start]
            if prefix.endswith(marker):
                new_start -= len(marker)
                return

    # Iteratively check markers.
    for _ in range(2):
        expand_if_unbalanced("**")
        expand_if_unbalanced("__")
        expand_if_unbalanced("_")
        expand_if_unbalanced("*")

    return new_start, new_end


def _refine_match_boundaries(text: str, start: int, end: int) -> Tuple[int, int]:
    """
    Refines fuzzy match boundaries to avoid greedy consumption of unbalanced markers.
    Example: "**Header.**Body" -> Regex matches "**Body".
    This function trims the leading "**" because "Body" is balanced (0 markers)
    while "**Body" is unbalanced (1 marker).
    """
    # Markers to check. Order matters (check compound markers first).
    markers = ["**", "__", "*", "_"]

    current_text = text[start:end]
    best_start, best_end = start, end

    # 1. Check Leading Noise
    for marker in markers:
        if current_text.startswith(marker):
            # Calculate balance scores
            # "Unbalanced-ness" = count % 2. 0 is perfect. 1 is bad.
            current_score = current_text.count(marker) % 2

            trimmed_text = current_text[len(marker) :]
            trimmed_score = trimmed_text.count(marker) % 2

            # If we are currently unbalanced (1) and trimming makes it balanced (0), do it.
            if current_score == 1 and trimmed_score == 0:
                best_start += len(marker)
                current_text = trimmed_text  # Update for next iteration

    # 2. Check Trailing Noise (Logic is symmetric)
    for marker in markers:
        if current_text.endswith(marker):
            current_score = current_text.count(marker) % 2
            trimmed_text = current_text[: -len(marker)]
            trimmed_score = trimmed_text.count(marker) % 2

            if current_score == 1 and trimmed_score == 0:
                best_end -= len(marker)
                current_text = trimmed_text

    return best_start, best_end


def _make_fuzzy_regex(target_text: str) -> str:
    """
    Constructs a regex pattern from target text that permits:
    - Variable whitespace (\\s+)
    - Variable underscores (_+)
    - Smart quote variation
    - Intervening markdown formatting (**, _, etc.)
    - Punctuation boundaries
    - Structural noise (bullets, numbering) across newlines
    """
    target_text = _replace_smart_quotes(target_text)

    parts = []
    # Tokenize: Underscores, Whitespace, Quotes, AND Punctuation
    token_pattern = re.compile(r"(_+)|(\s+)|(['\"])|([.,;:])")

    # Pattern to allow optional markdown markers between tokens
    md_noise = r"(?:\*\*|__|\*|_)*"

    # Pattern for Structural Noise (bullets, indentation, numbering)
    # Matches sequences of: Whitespace, *, +, -, >, digits+dot
    # STRICTER: Should not match simple space chars that act as delimiters.
    # Only match if it looks like a list item marker or newline sequence.
    structural_noise = r"(?:\s*(?:[*+\->]|\d+\.)\s+|\s*\n\s*)"

    # START ANCHOR:
    # Allow optional list marker at the very start
    # Must contain a bullet/number to avoid greedy space matching
    start_list_marker = r"(?:[ \t]*(?:[*+\->]|\d+\.)\s+)?"
    parts.append(start_list_marker)
    parts.append(md_noise)

    last_idx = 0
    for match in token_pattern.finditer(target_text):
        literal = target_text[last_idx : match.start()]
        if literal:
            parts.append(re.escape(literal))
            parts.append(md_noise)

        g_underscore, g_space, g_quote, g_punct = match.groups()

        if g_underscore:
            parts.append(r"_+")
        elif g_space:
            # If the whitespace contains a newline, allow structural noise (bullets, indents)
            if "\n" in g_space:
                parts.append(f"(?:{structural_noise}|\\s+)+")
            else:
                parts.append(r"\s+")
        elif g_quote:
            if g_quote == "'":
                parts.append(r"[''']")
            else:
                parts.append(r"[\"" "]")
        elif g_punct:
            parts.append(re.escape(g_punct))

        parts.append(md_noise)
        last_idx = match.end()

    remaining = target_text[last_idx:]
    if remaining:
        parts.append(re.escape(remaining))

    return "".join(parts)


def _find_match_in_text(text: str, target: str) -> Tuple[int, int]:
    """
    Finds target in text using progressive matching strategies.
    Returns (start_idx, end_idx) or (-1, -1) if not found.
    """
    if not target:
        return -1, -1

    # 1. Exact match
    idx = text.find(target)
    if idx != -1:
        return _find_safe_boundaries(text, idx, idx + len(target))

    # 2. Smart quote normalization
    norm_text = _replace_smart_quotes(text)
    norm_target = _replace_smart_quotes(target)
    idx = norm_text.find(norm_target)
    if idx != -1:
        return _find_safe_boundaries(text, idx, idx + len(norm_target))

    # 3. Fuzzy regex match (handles markdown noise)
    try:
        pattern = _make_fuzzy_regex(target)
        match = re.search(pattern, text)
        if match:
            # Refine boundaries BEFORE expanding safe boundaries.
            # This prevents the greedy regex match from stealing markers from neighbors.
            raw_start, raw_end = match.start(), match.end()
            refined_start, refined_end = _refine_match_boundaries(text, raw_start, raw_end)

            return _find_safe_boundaries(text, refined_start, refined_end)
    except re.error:
        pass

    return -1, -1


def _build_critic_markup(
    target_text: str,
    new_text: str,
    comment: Optional[str],
    edit_index: int,
    include_index: bool,
    highlight_only: bool,
) -> str:
    """
    Generates CriticMarkup string for a single edit.
    """
    parts = []

    # Strip balanced markers from target
    prefix_markup, clean_target, suffix_markup = _strip_balanced_markers(target_text)

    # If we stripped markers from target, try to strip the SAME markers from new_text
    clean_new = new_text
    if prefix_markup and new_text:
        # Check if new_text has the same outer markers
        if new_text.startswith(prefix_markup) and new_text.endswith(suffix_markup):
            inner_len = len(prefix_markup)
            clean_new = new_text[inner_len:-inner_len] if len(new_text) > inner_len * 2 else new_text

    parts.append(prefix_markup)

    if highlight_only:
        parts.append(f"{{=={clean_target}==}}")
    else:
        has_target = bool(clean_target)
        has_new = bool(clean_new)

        if has_target and not has_new:
            parts.append(f"{{--{clean_target}--}}")
        elif not has_target and has_new:
            parts.append(f"{{++{clean_new}++}}")
        elif has_target and has_new:
            parts.append(f"{{--{clean_target}--}}{{++{clean_new}++}}")

    parts.append(suffix_markup)

    # Build metadata block
    meta_parts = []
    if comment:
        meta_parts.append(comment)
    if include_index:
        meta_parts.append(f"[Edit:{edit_index}]")

    if meta_parts:
        meta_content = " ".join(meta_parts)
        parts.append(f"{{>>{meta_content}<<}}")

    return "".join(parts)


def apply_edits_to_markdown(
    markdown_text: str,
    edits: List[DocumentEdit],
    include_index: bool = False,
    highlight_only: bool = False,
) -> str:
    """
    Applies edits to Markdown text and returns CriticMarkup-annotated output.
    """
    if not edits:
        return markdown_text

    # Step 1: Find match positions for each edit
    matched_edits: List[Tuple[int, int, str, DocumentEdit, int]] = []

    for idx, edit in enumerate(edits):
        target = edit.target_text or ""

        if not target:
            if highlight_only:
                logger.debug(f"Skipping edit {idx}: no target_text in highlight_only mode")
                continue
            else:
                logger.warning(f"Skipping edit {idx}: pure insertion without target_text not supported in text mode")
                continue

        start, end = _find_match_in_text(markdown_text, target)

        if start == -1:
            logger.warning(f"Skipping edit {idx}: target_text not found: '{target[:50]}...'")
            continue

        actual_matched_text = markdown_text[start:end]
        matched_edits.append((start, end, actual_matched_text, edit, idx))

    # Step 2: Check for overlapping edits
    matched_edits_filtered: List[Tuple[int, int, str, DocumentEdit, int]] = []
    occupied_ranges: List[Tuple[int, int]] = []

    matched_edits.sort(key=lambda x: x[4])

    for start, end, actual_text, edit, orig_idx in matched_edits:
        overlaps = False
        for occ_start, occ_end in occupied_ranges:
            if start < occ_end and end > occ_start:
                overlaps = True
                logger.warning(f"Skipping edit {orig_idx}: overlaps with previously matched edit")
                break

        if not overlaps:
            matched_edits_filtered.append((start, end, actual_text, edit, orig_idx))
            occupied_ranges.append((start, end))

    # Step 3: Sort by position descending
    matched_edits_filtered.sort(key=lambda x: x[0], reverse=True)

    # Step 4: Apply edits
    result = markdown_text

    for start, end, actual_text, edit, orig_idx in matched_edits_filtered:
        new = edit.new_text or ""

        markup = _build_critic_markup(
            target_text=actual_text,
            new_text=new,
            comment=edit.comment,
            edit_index=orig_idx,
            include_index=include_index,
            highlight_only=highlight_only,
        )

        result = result[:start] + markup + result[end:]

    return result
