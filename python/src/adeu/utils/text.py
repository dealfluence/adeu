# FILE: src/adeu/utils/text.py
"""Small text helpers shared by the engine and CLI output paths."""

from typing import List

from diff_match_patch import diff_match_patch

# Default cap for echoing caller-supplied strings (target_text/new_text) back
# in batch reports and error messages.
REPORT_ECHO_CAP = 500

# Tighter cap for the inline redline preview snippets ({--...--}{++...++}),
# which additionally carry surrounding document context.
PREVIEW_TEXT_CAP = 200


def truncate_middle(text: str, cap: int) -> str:
    """
    Bounds `text` to roughly `cap` visible characters, keeping the head and
    tail and stating how much was omitted. Returns short strings unchanged.
    """
    if text is None or len(text) <= cap:
        return text
    head = max(1, cap * 2 // 3)
    tail = max(1, cap - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}… [{omitted:,} chars omitted] …{text[-tail:]}"


def clamp_text(text: str, cap: int) -> str:
    """
    Hard-caps `text` to at most `cap` characters, marking the elision with an
    ASCII "...". Use this instead of `truncate_middle` wherever the cap is a
    real ceiling: `truncate_middle` keeps head AND tail plus a ~25-char
    "[N chars omitted]" note (with non-ASCII ellipses that a JSON escape
    triples), so its result routinely runs longer than `cap` — fine for a
    500-char echo budget, fatal for the minimal report's per-edit token
    budget.
    """
    if len(text) <= cap:
        return text
    return text[: max(1, cap - 3)] + "..."


def batch_details_header(details) -> str:
    """
    Section header for a batch report's detail lines. Purely informational
    notes ("- Note: … the action itself succeeded") must not be filed under
    "Skipped Details" — that header claims work was skipped when it wasn't
    (QA round 3, finding 3.4).
    """
    if details and all(str(d).lstrip().startswith("- Note:") for d in details):
        return "Notes:"
    return "Skipped Details:"


# CriticMarkup delimiters that must never appear verbatim inside a {>>…<<}
# meta bubble: a comment body containing e.g. "{--del--}" would nest raw
# markup inside the annotation, and its "<<}"/"--}" terminates the outer
# bubble early for every CriticMarkup consumer — including this package's
# own preview/tidy regexes (QA round 3, findings 3.7/3.8).
_CRITIC_TOKENS = ("{++", "++}", "{--", "--}", "{==", "==}", "{>>", "<<}")


def escape_critic_tokens(text: str) -> str:
    """
    Defangs CriticMarkup delimiters in projection-embedded free text (comment
    bodies) by spacing the brace/marker apart: "{>>x<<}" renders as
    "{ >>x<< }". The content stays readable while no delimiter sequence
    survives for a parser to misinterpret.
    """
    if not text or "{" not in text and "}" not in text:
        return text
    for token in _CRITIC_TOKENS:
        if token in text:
            if token.startswith("{"):
                text = text.replace(token, "{ " + token[1:])
            else:
                text = text.replace(token, token[:-1] + " }")
    return text


# ---------------------------------------------------------------------------
# Typographic normalization (matcher/writer symmetry)
# ---------------------------------------------------------------------------

# The EXACT set DocumentMapper._replace_smart_quotes forgives when matching a
# target against the projection. The writer must forgive the same set and no
# more, or the two halves of one edit disagree about what the caller meant
# (BUG_comment_threading_anchoring_and_typography.md B4). Each entry maps one
# character to one character, so normalization is length-preserving — the
# alignment in restore_document_typography relies on that.
SMART_QUOTE_MAP = {
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
}

_SMART_QUOTE_TRANSLATION = str.maketrans(SMART_QUOTE_MAP)


def normalize_smart_quotes(text: str) -> str:
    """Folds curly quotes/apostrophes onto their ASCII equivalents."""
    if not text:
        return text
    return text.translate(_SMART_QUOTE_TRANSLATION)


def has_smart_quotes(text: str) -> bool:
    """True when `text` carries at least one curly quote/apostrophe."""
    return bool(text) and any(ch in text for ch in SMART_QUOTE_MAP)


def restore_document_typography(doc_text: str, new_text: str) -> str:
    """
    Rewrites `new_text` so every position the caller did NOT intentionally
    change keeps the DOCUMENT's own characters.

    The matcher is smart-quote-insensitive: an LLM that writes
    `parties' Master` matches a document reading `parties’ Master`, which is
    the forgiving behaviour we want. The writer then word-diffs the document's
    real slice against the caller's literal `new_text`, so each such
    difference used to land as a genuine tracked change on a provision nobody
    touched — four of eight change chunks in the reported run were pure
    punctuation rewrites (B4).

    Both strings are normalized (length-preserving, see SMART_QUOTE_MAP) and
    aligned character-by-character; runs the alignment calls EQUAL adopt
    `doc_text`'s characters, runs that genuinely differ keep the caller's.
    When the two differ ONLY by normalized punctuation the result is
    `doc_text` verbatim, i.e. zero tracked changes.
    """
    if not doc_text or not new_text:
        return new_text
    if not has_smart_quotes(doc_text):
        return new_text

    norm_doc = normalize_smart_quotes(doc_text)
    norm_new = normalize_smart_quotes(new_text)
    if norm_doc == norm_new:
        # Differ ONLY by forgiven punctuation: the correct number of tracked
        # changes is zero, so hand back the document verbatim.
        return doc_text

    dmp = diff_match_patch()
    diffs = dmp.diff_main(norm_doc, norm_new)

    out: List[str] = []
    doc_pos = 0
    new_pos = 0
    for op, chunk in diffs:
        if op == 0:  # EQUAL -> the caller changed nothing here
            out.append(doc_text[doc_pos : doc_pos + len(chunk)])
            doc_pos += len(chunk)
            new_pos += len(chunk)
        elif op == -1:  # DELETE -> present in the document, dropped by the caller
            doc_pos += len(chunk)
        else:  # INSERT -> the caller's own text
            out.append(new_text[new_pos : new_pos + len(chunk)])
            new_pos += len(chunk)
    return "".join(out)
