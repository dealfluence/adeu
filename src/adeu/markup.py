# FILE: src/adeu/markup.py

import re
from typing import List, Optional, Tuple

import structlog

from adeu.models import DocumentEdit

logger = structlog.get_logger(__name__)


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
    # Order matters slightly if markers are nested, but repeating helps stability.
    # We do a few passes to handle nesting like **_Text_**.

    for _ in range(2):
        expand_if_unbalanced("**")
        expand_if_unbalanced("__")
        expand_if_unbalanced("_")
        expand_if_unbalanced("*")

    return new_start, new_end


def _make_fuzzy_regex(target_text: str) -> str:
    """
    Constructs a regex pattern from target text that permits:
    - Variable whitespace (\\s+)
    - Variable underscores (_+)
    - Smart quote variation
    - Intervening markdown formatting (**, _, etc.)
    """
    target_text = _replace_smart_quotes(target_text)

    parts = []
    token_pattern = re.compile(r"(_+)|(\s+)|(['\"])")

    # Pattern to allow optional markdown markers between tokens
    md_noise = r"(?:\*\*|__|\*|_)*"

    # Allow noise at start
    parts.append(md_noise)

    last_idx = 0
    for match in token_pattern.finditer(target_text):
        literal = target_text[last_idx : match.start()]
        if literal:
            parts.append(re.escape(literal))
            parts.append(md_noise)

        g_underscore, g_space, g_quote = match.groups()

        if g_underscore:
            parts.append(r"_+")
        elif g_space:
            parts.append(r"\s+")
        elif g_quote:
            if g_quote == "'":
                parts.append(r"[''']")
            else:
                parts.append(r"[\"" "]")

        parts.append(md_noise)
        last_idx = match.end()

    remaining = target_text[last_idx:]
    if remaining:
        parts.append(re.escape(remaining))

    # Allow noise at end for cases where target is "Word" but text is "Word**"
    # But ONLY if it's noise. This is risky?
    # Actually, removing trailing md_noise here and letting _find_safe_boundaries
    # handle the balance is safer.
    # We removed trailing noise in previous attempt, let's keep it removed
    # so we don't aggressively consume markers unless necessary.

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
            return _find_safe_boundaries(text, match.start(), match.end())
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

    prefix_markup = ""
    suffix_markup = ""
    clean_target = target_text

    # Logic to strip balanced outer markers (e.g. **Term**) and place them outside
    # This keeps the CriticMarkup cleaner: **{==Term==}** instead of {==**Term**==}

    # Check for balanced **
    if clean_target.startswith("**") and clean_target.endswith("**") and len(clean_target) >= 4:
        prefix_markup += "**"
        suffix_markup = "**" + suffix_markup
        clean_target = clean_target[2:-2]
    # Check for balanced __
    elif clean_target.startswith("__") and clean_target.endswith("__") and len(clean_target) >= 4:
        prefix_markup += "__"
        suffix_markup = "__" + suffix_markup
        clean_target = clean_target[2:-2]
    # Check for balanced _
    elif clean_target.startswith("_") and clean_target.endswith("_") and len(clean_target) >= 2:
        prefix_markup += "_"
        suffix_markup = "_" + suffix_markup
        clean_target = clean_target[1:-1]
    # Check for balanced *
    elif clean_target.startswith("*") and clean_target.endswith("*") and len(clean_target) >= 2:
        prefix_markup += "*"
        suffix_markup = "*" + suffix_markup
        clean_target = clean_target[1:-1]

    parts.append(prefix_markup)

    if highlight_only:
        parts.append(f"{{=={clean_target}==}}")
    else:
        has_target = bool(clean_target)
        has_new = bool(new_text)

        if has_target and not has_new:
            parts.append(f"{{--{clean_target}--}}")
        elif not has_target and has_new:
            parts.append(f"{{++{new_text}++}}")
        elif has_target and has_new:
            parts.append(f"{{--{clean_target}--}}{{++{new_text}++}}")

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
