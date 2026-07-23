import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from adeu.diff import trim_common_context
from adeu.models import ModifyText
from adeu.utils.safe_regex import RegexTimeoutError, user_finditer

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

    REGEX SAFETY: All optional marker groups use atomic-group syntax (?>...)
    to prevent catastrophic backtracking. The previous non-atomic version
    `(?:\\*\\*|__|\\*|_)?` interleaved with `\\s+` produced exponential
    backtracking on long targets in long haystacks. Atomic groups commit
    on first match and never reconsider, making the regex linear.
    """
    target_text = _replace_smart_quotes(target_text)

    parts = []
    # Tokenize: Underscores, Whitespace, Quotes, AND Punctuation
    token_pattern = re.compile(r"(_+)|(\s+)|(['\"])|([.,;:])")

    # Pattern to allow optional markdown markers between tokens.
    # Atomic group prevents backtracking through marker permutations.
    md_noise = r"(?>\*\*|__|\*|_)*"

    # Pattern for Structural Noise (bullets, indentation, numbering)
    structural_noise = r"(?:\s*(?:[*+\->]|\d+\.)\s+|\s*\n\s*)"

    # START ANCHOR:
    # Allow optional list marker at the very start
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
            # If the whitespace contains a newline, allow structural noise
            if "\n" in g_space:
                parts.append(f"(?:{structural_noise}|\\s+)+")
            else:
                parts.append(r"\s+")
        elif g_quote:
            if g_quote == "'":
                parts.append(r"[\u2018\u2019']")
            else:
                parts.append(r"[\"\u201c\u201d]")
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
    matches = _find_all_matches_in_text(text, target)
    if matches:
        return matches[0]
    return -1, -1


def _find_all_matches_in_text(text: str, target: str, is_regex: bool = False) -> List[Tuple[int, int]]:
    """
    Every non-overlapping match of `target` in `text` as (start, end) pairs,
    using the SAME strategy ladder as the apply engine's
    DocumentMapper.find_all_match_indices: regex (when requested) or
    exact → smart-quote-normalized → fuzzy. Markup previews must resolve
    matching identically to apply, or the preview lies (QA 2026-07-18 M1).
    """
    if not target:
        return []

    if is_regex:
        # Same semantics as the mapper: budgeted user regex; an invalid
        # pattern simply produces no matches (surfaced as "not found").
        try:
            return [m.span() for m in user_finditer(target, text)]
        except re.error:
            return []

    # 1. Exact matches
    spans = [m.span() for m in re.finditer(re.escape(target), text)]
    if spans:
        return [_find_safe_boundaries(text, s, e) for s, e in spans]

    # 2. Smart quote normalization
    norm_text = _replace_smart_quotes(text)
    norm_target = _replace_smart_quotes(target)
    spans = [m.span() for m in re.finditer(re.escape(norm_target), norm_text)]
    if spans:
        return [_find_safe_boundaries(text, s, e) for s, e in spans]

    # 3. Markdown-stripped match, mirroring the mapper's strip-markdown and
    # plain-projection rungs: a plain target must find text whose projection
    # carries **bold**/_italic_ markers (even mid-word), and a marked target
    # must find plain text.
    stripped_text, pos_map = _strip_markdown_for_matching(norm_text)
    stripped_target, _ = _strip_markdown_for_matching(norm_target)
    if stripped_target and (stripped_text != norm_text or stripped_target != norm_target):
        results = []
        for m in re.finditer(re.escape(stripped_target), stripped_text):
            p_start, p_end = m.span()
            raw_start = pos_map[p_start]
            raw_end = pos_map[p_end - 1] + 1
            results.append(_find_safe_boundaries(text, raw_start, raw_end))
        if results:
            return results

    # 4. Fuzzy regex match (handles markdown noise, list markers, etc.).
    # Atomic groups in _make_fuzzy_regex prevent catastrophic backtracking.
    try:
        pattern = _make_fuzzy_regex(target)
        results = []
        for match in re.finditer(pattern, text):
            refined_start, refined_end = _refine_match_boundaries(text, match.start(), match.end())
            results.append(_find_safe_boundaries(text, refined_start, refined_end))
        if results:
            return results
    except re.error:
        pass

    return []


# Maximum number of match examples to include in an ambiguity error message.
# Capped to keep error payloads bounded — high-frequency tokens (like a brand
# name appearing 100+ times) would otherwise produce tens of KB of context
# snippets that consume LLM context budget without adding signal.
AMBIGUITY_EXAMPLES_CAP = 5

# Length of the surrounding-text window shown for each match example.
AMBIGUITY_CONTEXT_CHARS = 50


def format_ambiguity_error(
    edit_index: int,
    target_text: str,
    haystack: str,
    match_positions: list[tuple[int, int]],
) -> str:
    """
    Builds a uniformly-formatted ambiguity error message used by both the disk
    and Live Word edit pipelines.

    Args:
        edit_index: 1-based index of the failing edit, used in the message prefix.
        target_text: the search string the agent provided.
        haystack: the text the search was performed against.
        match_positions: list of (start, end) tuples for ALL matches found.
            The function shows up to AMBIGUITY_EXAMPLES_CAP examples and
            indicates how many additional matches are not shown.

    Returns:
        A multi-line error string suitable for inclusion in the
        BatchValidationError list (disk path) or skipped_details (Live Word path).

    Raises:
        ValueError: if match_positions has fewer than 2 entries (this helper is
            only meaningful for genuine ambiguity).
    """
    total = len(match_positions)
    if total < 2:
        raise ValueError(f"format_ambiguity_error requires at least 2 matches, got {total}")

    shown = match_positions[:AMBIGUITY_EXAMPLES_CAP]
    remaining = total - len(shown)

    lines = [
        f"- Edit {edit_index} Failed: Ambiguous match. Target text appears "
        f"{total} times. First {len(shown)} occurrences:"
    ]

    for i, (start, end) in enumerate(shown, start=1):
        pre_start = max(0, start - AMBIGUITY_CONTEXT_CHARS)
        post_end = min(len(haystack), end + AMBIGUITY_CONTEXT_CHARS)

        pre_context = haystack[pre_start:start].replace("\n", " ")
        post_context = haystack[end:post_end].replace("\n", " ")
        match_text = haystack[start:end].replace("\n", " ")

        # Truncate displayed match itself if pathologically long.
        if len(match_text) > 50:
            match_text = match_text[:25] + "..." + match_text[-20:]

        prefix_marker = "..." if pre_start > 0 else ""
        suffix_marker = "..." if post_end < len(haystack) else ""

        lines.append(f'    {i}. "{prefix_marker}{pre_context}[{match_text}]{post_context}{suffix_marker}"')

    if remaining > 0:
        lines.append(f"    ... and {remaining} more occurrence(s) not shown.")

    # Tell the agent EXACTLY how to re-call. Without this, agents loop forever
    # refining target_text/regex because they never learn that match_mode is the
    # built-in escape hatch for genuine ambiguity. The safe strategy (more
    # context) comes first: blindly switching to "first"/"all" has silently
    # modified unrelated occurrences — dates, section numbers — in real use
    # (QA C1), so those options carry an explicit verification warning.
    lines.append("  To resolve, re-send this edit using ONE of these strategies:")
    lines.append(
        "    1. RECOMMENDED: Provide more surrounding context in your target_text to uniquely "
        'identify a single location (keep the default "match_mode": "strict").'
    )
    lines.append(
        f'    2. Set "match_mode": "all" to modify ALL {total} occurrences — only after verifying '
        "from the occurrence list above that EVERY occurrence should change."
    )
    lines.append(
        '    3. Set "match_mode": "first" to modify only the FIRST occurrence — only after verifying '
        "the first occurrence above is the one you intend to change."
    )

    return "\n".join(lines)


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
    edits: List[ModifyText],
    include_index: bool = False,
    highlight_only: bool = False,
    edit_reports: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Applies edits to Markdown text and returns CriticMarkup-annotated output.

    Edit resolution follows the SAME semantics as `apply` (QA 2026-07-18 M1):
      - `regex: true` targets match as regular expressions
      - `match_mode` is honored: "strict" (default) refuses ambiguous targets,
        "first" marks the first occurrence, "all" marks every occurrence
      - a missing target is a per-edit failure, never a silent skip

    When `edit_reports` is provided, one dict per input edit is appended:
    {"index": 0-based input position, "status": "applied"|"failed",
     "error": str|None, "occurrences": int}.
    """
    if not edits:
        return markdown_text

    def _report(idx: int, status: str, error: Optional[str] = None, occurrences: int = 0):
        if edit_reports is not None:
            edit_reports.append({"index": idx, "status": status, "error": error, "occurrences": occurrences})

    # Step 1: Find match positions for each edit
    matched_edits: List[Tuple[int, int, str, ModifyText, int]] = []
    failed_indices: set = set()

    for idx, edit in enumerate(edits):
        target = edit.target_text or ""
        match_mode = getattr(edit, "match_mode", "strict") or "strict"
        is_regex = bool(getattr(edit, "regex", False))

        if not target:
            msg = (
                f"- Edit {idx + 1} Failed: target_text is empty. Pure insertions are expressed as a "
                "replacement: put the text immediately around the insertion point in target_text and "
                "repeat it (plus the new text) in new_text."
            )
            logger.warning(msg)
            failed_indices.add(idx)
            _report(idx, "failed", msg)
            continue

        try:
            spans = _find_all_matches_in_text(markdown_text, target, is_regex=is_regex)
        except RegexTimeoutError as e:
            msg = f"- Edit {idx + 1} Failed: {e}"
            logger.warning(msg)
            failed_indices.add(idx)
            _report(idx, "failed", msg)
            continue

        if not spans:
            msg = f'- Edit {idx + 1} Failed: Target text not found in document:\n  "{target[:80]}"'
            logger.warning(msg)
            failed_indices.add(idx)
            _report(idx, "failed", msg)
            continue

        if len(spans) > 1 and match_mode == "strict":
            msg = format_ambiguity_error(
                edit_index=idx + 1,
                target_text=target,
                haystack=markdown_text,
                match_positions=spans,
            )
            logger.warning(msg)
            failed_indices.add(idx)
            _report(idx, "failed", msg)
            continue

        selected = spans[:1] if match_mode in ("strict", "first") else spans
        for start, end in selected:
            matched_edits.append((start, end, markdown_text[start:end], edit, idx))
        _report(idx, "applied", None, len(selected))

    # Step 2: Check for overlapping edits
    matched_edits_filtered: List[Tuple[int, int, str, ModifyText, int]] = []
    occupied_ranges: List[Tuple[int, int]] = []

    matched_edits.sort(key=lambda x: x[4])

    for start, end, actual_text, edit, orig_idx in matched_edits:
        overlaps = False
        for occ_start, occ_end in occupied_ranges:
            if start < occ_end and end > occ_start:
                overlaps = True
                msg = f"- Edit {orig_idx + 1} Failed: overlaps with a previously matched edit."
                logger.warning(msg)
                if edit_reports is not None:
                    for r in edit_reports:
                        if r["index"] == orig_idx:
                            r["status"] = "failed"
                            r["error"] = msg
                            r["occurrences"] = 0
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

        # Apply context trimming to isolate the actual change from the anchor
        prefix_len, suffix_len = trim_common_context(actual_text, new)

        # Extract the unmodified prefix and suffix
        unmodified_prefix = actual_text[:prefix_len] if prefix_len > 0 else ""
        unmodified_suffix = actual_text[len(actual_text) - suffix_len :] if suffix_len > 0 else ""

        # Isolate the actual target and new text to be marked up
        t_end = len(actual_text) - suffix_len
        n_end = len(new) - suffix_len
        isolated_target = actual_text[prefix_len:t_end]
        isolated_new = new[prefix_len:n_end]

        markup = _build_critic_markup(
            target_text=isolated_target,
            new_text=isolated_new,
            comment=edit.comment,
            # 1-based, matching apply's "Edit N" reports and batch
            # validation errors (QA 2026-07-17 F10).
            edit_index=orig_idx + 1,
            include_index=include_index,
            highlight_only=highlight_only,
        )

        # Recombine the unmodified anchors with the newly generated markup block
        full_replacement = unmodified_prefix + markup + unmodified_suffix
        result = result[:start] + full_replacement + result[end:]

    return result


# CriticMarkup wrappers stripped when computing a line's CLEAN (accepted)
# reading: deletions and comments vanish, insertions and highlights unwrap.
_CRITIC_DELETION_RE = re.compile(r"\{--(.*?)--\}", re.DOTALL)
_CRITIC_COMMENT_RE = re.compile(r"\{>>(.*?)<<\}", re.DOTALL)
_CRITIC_INSERTION_RE = re.compile(r"\{\+\+(.*?)\+\+\}", re.DOTALL)
_CRITIC_HIGHLIGHT_RE = re.compile(r"\{==(.*?)==\}", re.DOTALL)


def _clean_reading_of_line(line: str) -> str:
    """A line's accepted text: what the document reads after every rendered
    CriticMarkup change on it is accepted."""
    line = _CRITIC_DELETION_RE.sub("", line)
    line = _CRITIC_COMMENT_RE.sub("", line)
    line = _CRITIC_INSERTION_RE.sub(r"\1", line)
    line = _CRITIC_HIGHLIGHT_RE.sub(r"\1", line)
    return line


def apply_structural_ops_to_markdown(
    markdown_text: str,
    ops: List[Any],
    edit_reports: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Renders structured table-row operations (insert_row / delete_row) into a
    CriticMarkup preview: a deleted row's line is wrapped in {--…--}, an
    inserted row appears as a {++cells | joined | like | this++} line beside
    its anchor — structural changes belong in the rendered preview, not in
    a stderr footnote.

    Anchors resolve against each line's CLEAN reading, mirroring the apply
    engine: an insert_row anchored on the final text of a row an earlier
    edit in the same batch modified finds the {--old--}{++new++} line.
    Match semantics are strict — exactly one line must match, and both the
    not-found and ambiguous cases report as failed.
    """
    lines = markdown_text.split("\n")

    def _record(op: Any, status: str, error: Optional[str] = None) -> None:
        if edit_reports is not None:
            new_text = " | ".join(op.cells) if getattr(op, "cells", None) is not None else ""
            edit_reports.append(
                {
                    "status": status,
                    "target_text": op.target_text,
                    "new_text": new_text,
                    "error": error,
                }
            )

    for op in ops:
        anchor = (op.target_text or "").strip()
        matches = [i for i, line in enumerate(lines) if anchor and anchor in _clean_reading_of_line(line)]
        if len(matches) == 0:
            _record(
                op,
                "failed",
                f"- Row operation failed: no table row line contains '{anchor}'. Structural "
                "operations anchor on the row's full or partial text.",
            )
            continue
        if len(matches) > 1:
            _record(
                op,
                "failed",
                f"- Row operation failed: {len(matches)} lines contain '{anchor}' — the anchor is "
                "ambiguous. Make the anchor row unique.",
            )
            continue

        i = matches[0]
        if op.type == "delete_row":
            lines[i] = "{--" + lines[i] + "--}"
        else:
            new_line = "{++" + " | ".join(op.cells) + "++}"
            if getattr(op, "position", "below") == "above":
                lines.insert(i, new_line)
            else:
                lines.insert(i + 1, new_line)
        _record(op, "applied")

    return "\n".join(lines)
