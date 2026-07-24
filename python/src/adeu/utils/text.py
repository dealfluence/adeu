# FILE: src/adeu/utils/text.py
"""Small text helpers shared by the engine and CLI output paths."""

# Default cap for echoing caller-supplied strings (target_text/new_text) back
# in batch reports and error messages. Reports feed straight into LLM context
# windows via MCP, so an oversized edit value must never be reflected in full
# (QA C2: a 2MB new_text was echoed twice, unbounded, in the apply report).
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
