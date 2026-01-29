# FILE: src/adeu/markup.py
"""
Pure text transformation utilities for applying edits to Markdown
and generating CriticMarkup output.
"""

import re
from typing import List, Optional, Tuple

import structlog

from adeu.models import DocumentEdit

logger = structlog.get_logger(__name__)


def _replace_smart_quotes(text: str) -> str:
    """Normalizes smart quotes to ASCII equivalents."""
    return text.replace(""", '"').replace(""", '"').replace("'", "'").replace("'", "'")


def _make_fuzzy_regex(target_text: str) -> str:
    """
    Constructs a regex pattern that permits:
    - Variable whitespace (\\s+)
    - Variable underscores (_+)
    - Smart quote variation
    """
    target_text = _replace_smart_quotes(target_text)

    parts = []
    token_pattern = re.compile(r"(_+)|(\s+)|(['\"])")

    last_idx = 0
    for match in token_pattern.finditer(target_text):
        literal = target_text[last_idx : match.start()]
        if literal:
            parts.append(re.escape(literal))

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
        return idx, idx + len(target)

    # 2. Smart quote normalization
    norm_text = _replace_smart_quotes(text)
    norm_target = _replace_smart_quotes(target)
    idx = norm_text.find(norm_target)
    if idx != -1:
        return idx, idx + len(target)

    # 3. Fuzzy regex match
    try:
        pattern = _make_fuzzy_regex(target)
        match = re.search(pattern, text)
        if match:
            return match.start(), match.end()
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

    if highlight_only:
        # Highlight mode: just mark the target
        parts.append(f"{{=={target_text}==}}")
    else:
        # Full edit mode
        has_target = bool(target_text)
        has_new = bool(new_text)

        if has_target and not has_new:
            # Deletion
            parts.append(f"{{--{target_text}--}}")
        elif not has_target and has_new:
            # Pure insertion
            parts.append(f"{{++{new_text}++}}")
        elif has_target and has_new:
            # Modification
            parts.append(f"{{--{target_text}--}}{{++{new_text}++}}")
        # else: both empty, nothing to output

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

    Args:
        markdown_text: The source Markdown document.
        edits: List of edits with target_text, new_text, and optional comment.
        include_index: If True, include the edit's 0-based index in the output markup.
        highlight_only: If True, only highlight target_text with {==...==} notation
                        without applying insertions/deletions.

    Returns:
        Transformed Markdown string with CriticMarkup annotations.
    """
    if not edits:
        return markdown_text

    # Step 1: Find match positions for each edit
    # Store: (start_idx, end_idx, actual_matched_text, edit, original_index)
    matched_edits: List[Tuple[int, int, str, DocumentEdit, int]] = []

    for idx, edit in enumerate(edits):
        target = edit.target_text or ""

        if not target:
            if highlight_only:
                # In highlight mode, skip edits with no target
                logger.debug(f"Skipping edit {idx}: no target_text in highlight_only mode")
                continue
            else:
                # Pure insertion - needs anchor context
                logger.warning(f"Skipping edit {idx}: pure insertion without target_text not supported in text mode")
                continue

        start, end = _find_match_in_text(markdown_text, target)

        if start == -1:
            logger.warning(f"Skipping edit {idx}: target_text not found: '{target[:50]}...'")
            continue

        # Capture the actual text that was matched (may differ from target due to fuzzy matching)
        actual_matched_text = markdown_text[start:end]
        matched_edits.append((start, end, actual_matched_text, edit, idx))

    # Step 2: Check for overlapping edits, first-in-list wins
    matched_edits_filtered: List[Tuple[int, int, str, DocumentEdit, int]] = []
    occupied_ranges: List[Tuple[int, int]] = []

    # Sort by original index to process in list order for overlap resolution
    matched_edits.sort(key=lambda x: x[4])

    for start, end, actual_text, edit, orig_idx in matched_edits:
        overlaps = False
        for occ_start, occ_end in occupied_ranges:
            # Check overlap: ranges overlap if start < occ_end and end > occ_start
            if start < occ_end and end > occ_start:
                overlaps = True
                logger.warning(f"Skipping edit {orig_idx}: overlaps with previously matched edit")
                break

        if not overlaps:
            matched_edits_filtered.append((start, end, actual_text, edit, orig_idx))
            occupied_ranges.append((start, end))

    # Step 3: Sort by position descending (apply from end to start)
    matched_edits_filtered.sort(key=lambda x: x[0], reverse=True)

    # Step 4: Apply edits
    result = markdown_text

    for start, end, actual_text, edit, orig_idx in matched_edits_filtered:
        new = edit.new_text or ""

        markup = _build_critic_markup(
            target_text=actual_text,  # Use actual matched text, not user input
            new_text=new,
            comment=edit.comment,
            edit_index=orig_idx,
            include_index=include_index,
            highlight_only=highlight_only,
        )

        # Replace the target range with the markup
        result = result[:start] + markup + result[end:]

    return result
