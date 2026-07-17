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
