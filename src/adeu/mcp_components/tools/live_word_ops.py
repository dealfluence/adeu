import re
from typing import Any, List, Optional, Tuple, TypedDict

import structlog

logger = structlog.get_logger(__name__)


def strip_critic_markup(text: str) -> str:
    """Removes CriticMarkup tags so raw text can be found via Word's native Find."""
    if not text:
        return ""
    text = re.sub(r"\{--.*?--\}", "", text)
    text = re.sub(r"\{>>.*?<<\}", "", text)
    text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", text)
    text = re.sub(r"\{==(.*?)==\}", r"\1", text)
    return text


def strip_markdown_formatting(text: str) -> str:
    """Strips Markdown bold/italic/header markers so target_text can match plain COM text."""
    if not text:
        return ""
    # Strip bold: **text** or __text__
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"__", "", text)
    # Strip italic: single * or _ not part of a word
    text = re.sub(r"(?<!\w)\*(?!\*)", "", text)
    text = re.sub(r"(?<!\w)_(?!_)", "", text)
    # Strip header markers at line start
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    return text


def parse_markdown_for_com(text: str) -> Tuple[str, List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Parses bold and italic markdown, returning plain text and index ranges."""
    bold_ranges = []
    italic_ranges = []

    while True:
        m = re.search(r"\*\*(.*?)\*\*", text)
        if not m:
            break
        start = m.start()
        inner = m.group(1)
        text = text[:start] + inner + text[m.end() :]
        bold_ranges.append((start, start + len(inner)))

    while True:
        m = re.search(r"_(.*?)_", text)
        if not m:
            break
        start = m.start()
        inner = m.group(1)
        text = text[:start] + inner + text[m.end() :]
        italic_ranges.append((start, start + len(inner)))

    return text, bold_ranges, italic_ranges


_WD_HEADING_STYLE_IDS = {
    1: -2,  # wdStyleHeading1
    2: -3,  # wdStyleHeading2
    3: -4,  # wdStyleHeading3
    4: -5,  # wdStyleHeading4
    5: -6,  # wdStyleHeading5
    6: -7,  # wdStyleHeading6
    7: -8,  # wdStyleHeading7
    8: -9,  # wdStyleHeading8
    9: -10,  # wdStyleHeading9
}


def _parse_markdown_heading_prefix(line: str) -> Tuple[str, Optional[int]]:
    """
    Detects a leading markdown heading marker on a single line.
    Returns (clean_text, heading_level) where heading_level is an int 1..9
    or None if no heading prefix. Strips up to 9 leading '#' chars followed
    by a space, matching the disk engine's _parse_markdown_style.
    """
    if not line.startswith("#"):
        return line, None
    level = 0
    rest = line
    while rest.startswith("#") and level < 9:
        level += 1
        rest = rest[1:]
    if rest.startswith(" "):
        return rest[1:], level
    # '#' with no space: not a heading, treat as literal
    return line, None


def _is_structured_new_text(new_text: str) -> bool:
    """
    Returns True if `new_text` contains any markdown structure that the
    simple inline-replacement path cannot render: paragraph breaks or
    heading markers. Bold/italic alone is NOT structural.
    """
    if not new_text:
        return False
    # Paragraph boundary via blank line or explicit newline pair
    if "\n" in new_text or "\r" in new_text:
        return True
    # Heading marker at the very start
    stripped = new_text.lstrip()
    if stripped.startswith("#"):
        # confirm it's actually a heading marker ('# ...'), not literal
        _, level = _parse_markdown_heading_prefix(stripped)
        if level is not None:
            return True
    return False


def _split_new_text_into_lines(new_text: str) -> List[str]:
    """
    Splits new_text into lines for paragraph-wise insertion. Matches
    the disk engine's behaviour of splitting on any run of newline chars.
    Trailing empty elements are dropped so we don't emit a trailing
    empty paragraph.
    """
    lines = re.split(r"[\r\n]+", new_text)
    # Disk engine also pops trailing empty lines in track_insert
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _apply_line_formatting(
    doc: Any,
    base_start: int,
    plain_text: str,
    b_ranges: List[Tuple[int, int]],
    i_ranges: List[Tuple[int, int]],
    was_tracking: bool,
) -> None:
    """
    Applies bold/italic ranges to a just-inserted span. Mirrors the
    TrackRevisions-toggle pattern used in the original
    _apply_com_replacement: formatting is applied with tracking OFF so
    we don't pollute the review pane with format revisions, then the
    caller's tracking state is restored.
    """
    doc.TrackRevisions = False
    try:
        for b_start, b_end in b_ranges:
            fmt_rng = doc.Range(base_start + b_start, base_start + b_end)
            fmt_rng.Font.Bold = True
        for i_start, i_end in i_ranges:
            fmt_rng = doc.Range(base_start + i_start, base_start + i_end)
            fmt_rng.Font.Italic = True
    except Exception as e:
        logger.warning(f"Failed to apply formatting: {e}")
    finally:
        doc.TrackRevisions = was_tracking


_WD_STYLE_NORMAL = -1


def _apply_paragraph_style(doc: Any, position: int, heading_level: Optional[int]) -> None:
    """
    Applies a paragraph style to the paragraph containing `position`.

    heading_level=None means "plain body paragraph" (apply Normal).
    heading_level=1..9 means Heading N.

    We always apply an explicit style so that subsequent paragraphs
    inserted after a Heading do NOT silently inherit the Heading
    style (Word's default split-paragraph behaviour).
    """
    if heading_level is None:
        style_id = _WD_STYLE_NORMAL
    else:
        looked_up = _WD_HEADING_STYLE_IDS.get(heading_level)
        if looked_up is None:
            return
        style_id = looked_up
    try:
        p = doc.Range(position, position).Paragraphs(1)
        p.Style = style_id
    except Exception as e:
        logger.warning(f"Failed to apply paragraph style (level={heading_level}) at {position}: {e}")


class ParsedLineInfo(TypedDict):
    idx: int
    plain: str
    level: Optional[int]
    b_ranges: List[Tuple[int, int]]
    i_ranges: List[Tuple[int, int]]


def _apply_structured_com_replacement(
    doc: Any, app: Any, target_rng: Any, new_text: str, comment_text: Optional[str]
) -> None:
    """
    Reverse Sandwich Algorithm for Structured Replacements.
    Decouples the insertion from the deletion and applies the deletion LAST.
    This prevents Word from natively reordering tracked deletions past new paragraph
    breaks, and allows Comments.Add to span the entire block before the deletion
    forces truncation.
    """
    was_tracking = doc.TrackRevisions
    base_start = target_rng.Start
    original_len = target_rng.End - target_rng.Start

    logger.info(
        "Starting Reverse Sandwich Structured Replacement",
        base_start=base_start,
        original_len=original_len,
        was_tracking=was_tracking,
    )

    # 1. Rescue comments currently anchored to target_rng
    rescued_comments = []
    try:
        for i in range(1, target_rng.Comments.Count + 1):
            c = target_rng.Comments(i)
            rescued_comments.append({"author": c.Author, "text": c.Range.Text})
        if rescued_comments:
            logger.debug(f"Rescued {len(rescued_comments)} comments before payload execution.")
    except Exception as e:
        logger.warning(f"Failed to rescue comments: {e}")

    # 2. Pre-parse new_text into structured lines
    lines = _split_new_text_into_lines(new_text)
    if not lines:
        logger.debug("No lines found in new_text, falling back to empty string replacement.")
        target_rng.Delete()
        return

    parsed_lines: List[ParsedLineInfo] = []
    full_plain_parts = []

    for idx, line in enumerate(lines):
        clean, level = _parse_markdown_heading_prefix(line)
        plain, b_ranges, i_ranges = parse_markdown_for_com(clean)

        parsed_lines.append(
            {
                "idx": idx + 1,
                "plain": plain,
                "level": level,
                "b_ranges": b_ranges,
                "i_ranges": i_ranges,
            }
        )
        full_plain_parts.append(plain)

    line_1 = full_plain_parts[0]

    # 3. Insert Line 1 BEFORE the original text
    logger.debug(f"Inserting Line 1 before deletion at index {base_start}")
    doc.Range(base_start, base_start).Text = line_1

    # 4. Insert remaining lines AFTER the original text
    after_orig = base_start + len(line_1) + original_len
    if len(full_plain_parts) > 1:
        rest_text = "\r" + "\r".join(full_plain_parts[1:])
        logger.debug(f"Inserting remaining lines after deletion at index {after_orig}")
        doc.Range(after_orig, after_orig).Text = rest_text
        actual_end = after_orig + len(rest_text)
    else:
        actual_end = after_orig

    # 5. Attach comments strictly to Line 1.
    # Word's Comments.Add natively truncates or refuses to span tracked paragraph breaks.
    # Anchoring to the first inserted line guarantees the comment survives and is highly visible.
    line_1_rng = doc.Range(base_start, base_start + len(line_1))
    logger.info(
        "Attaching comments strictly to Line 1.",
        range_start=line_1_rng.Start,
        range_end=line_1_rng.End,
    )

    current_user = app.UserName
    for c_data in rescued_comments:
        try:
            app.UserName = c_data["author"]
            doc.Comments.Add(line_1_rng, c_data["text"])
            logger.debug(f"Rescued comment re-attached: '{c_data['text'][:20]}...'")
        except Exception as e:
            logger.warning(f"Failed to re-attach rescued comment: {e}")
    app.UserName = current_user

    if comment_text:
        try:
            doc.Comments.Add(line_1_rng, comment_text)
            logger.debug("New comment attached successfully.")
        except Exception as e:
            logger.error(f"Failed to attach edit comment: {e}")

    # 6. Execute the explicit Deletion LAST
    # The \r is already anchored after it, so Word will not natively reorder them.
    logger.debug("Executing deletion of original text to finalize Sandwich.")
    orig_rng_to_delete = doc.Range(base_start + len(line_1), after_orig)
    orig_rng_to_delete.Delete()

    # 7. Post-Replacement Formatting & Styles (Tracking OFF)
    logger.debug("Toggling TrackRevisions OFF for styling phase.")
    doc.TrackRevisions = False
    try:
        current_abs_offset = base_start

        for i, p_info in enumerate(parsed_lines):
            if i == 1:
                # Moving from Line 1 to Line 2: Skip deleted text and the \r
                current_abs_offset += original_len + 1
            elif i > 1:
                # Moving between subsequent lines: Skip the \r
                current_abs_offset += 1

            plain_len = len(p_info["plain"])

            logger.debug(
                f"Applying styles/formats for Line {p_info['idx']}",
                abs_start=current_abs_offset,
                plain_len=plain_len,
                heading_level=p_info["level"],
            )

            assert current_abs_offset >= base_start, "Invariant violated: Absolute start drifted before base_start!"
            assert current_abs_offset + plain_len <= actual_end, "Invariant violated: Line offset exceeded bounds!"

            _apply_paragraph_style(doc, current_abs_offset, p_info["level"])
            _apply_line_formatting(
                doc,
                current_abs_offset,
                p_info["plain"],
                p_info["b_ranges"],
                p_info["i_ranges"],
                was_tracking=False,  # Already disabled in this block
            )

            current_abs_offset += plain_len

    except Exception as e:
        logger.error(f"Error during styling/formatting phase: {e}")
    finally:
        logger.debug("Restoring TrackRevisions state.")
        doc.TrackRevisions = was_tracking

    logger.info("Reverse Sandwich Structured Replacement finished successfully.")


def apply_com_replacement(doc: Any, app: Any, target_rng: Any, new_text: str, comment_text: Optional[str]) -> None:
    """
    Routes to simple or structured replacement based on new_text content.
    """
    if _is_structured_new_text(new_text):
        _apply_structured_com_replacement(doc, app, target_rng, new_text, comment_text)
        return

    # ---- Simple path (original behaviour) ----
    rescued_comments = []
    try:
        for i in range(1, target_rng.Comments.Count + 1):
            c = target_rng.Comments(i)
            rescued_comments.append({"author": c.Author, "text": c.Range.Text})
    except Exception as e:
        logger.warning(f"Failed to rescue comments: {e}")

    plain_text, b_ranges, i_ranges = parse_markdown_for_com(new_text.replace("\n", "\r"))
    target_rng.Text = plain_text

    was_tracking = doc.TrackRevisions
    doc.TrackRevisions = False
    try:
        base_start = target_rng.Start
        for b_start, b_end in b_ranges:
            fmt_rng = doc.Range(base_start + b_start, base_start + b_end)
            fmt_rng.Font.Bold = True
        for i_start, i_end in i_ranges:
            fmt_rng = doc.Range(base_start + i_start, base_start + i_end)
            fmt_rng.Font.Italic = True
    except Exception as e:
        logger.warning(f"Failed to apply formatting: {e}")
    finally:
        doc.TrackRevisions = was_tracking

    current_user = app.UserName
    for c_data in rescued_comments:
        try:
            app.UserName = c_data["author"]
            doc.Comments.Add(target_rng, c_data["text"])
        except Exception:
            pass
    app.UserName = current_user

    if comment_text:
        doc.Comments.Add(target_rng, comment_text)
