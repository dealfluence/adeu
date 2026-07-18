# FILE: src/adeu/utils/safe_regex.py
"""
Time-budgeted execution of USER/LLM-supplied regular expressions.

`regex: true` on a ModifyText edit and `search_regex` on read_docx hand an
LLM-controlled pattern to a backtracking engine. Python's `re` has no timeout,
so a pathological pattern like `(a+)+$` against a run of 28+ repeated
characters hangs the process indefinitely (QA 2026-07-17 F5 — ReDoS).

This module routes user patterns through the third-party `regex` engine,
which checks a wall-clock deadline while matching and raises `TimeoutError`
when exceeded — a clean interruption with no leaked spinning thread. Errors
are translated back to `re.error` so existing invalid-pattern handling keeps
working unchanged.

Only USER-SUPPLIED patterns belong here. The engine's own generated patterns
(fuzzy matchers etc.) are built to be linear-time and stay on `re`.
"""

import re as _re
from typing import Iterator, Optional

import regex as _regex

# Wall-clock budget per pattern execution. Legitimate patterns scan even
# book-length documents in milliseconds; only catastrophic backtracking gets
# anywhere near this.
USER_PATTERN_TIMEOUT_SECONDS = 2.0


class RegexTimeoutError(ValueError):
    """A user-supplied pattern exceeded the matching time budget."""

    def __init__(self, pattern: str):
        self.pattern = pattern
        super().__init__(
            f"Regular expression exceeded the {USER_PATTERN_TIMEOUT_SECONDS:g}s matching time budget "
            "(catastrophic backtracking). Simplify the pattern — nested quantifiers like (a+)+ "
            "are the usual cause — or use a literal target instead."
        )


def _translate_error(pattern: str, exc: Exception) -> Exception:
    if isinstance(exc, TimeoutError):
        return RegexTimeoutError(pattern)
    if isinstance(exc, _regex.error) and not isinstance(exc, _re.error):
        # Keep the contract every existing caller relies on: invalid patterns
        # raise re.error.
        return _re.error(str(exc))
    return exc


def user_finditer(pattern: str, text: str, flags: int = 0) -> Iterator["_regex.Match"]:
    """finditer with a wall-clock budget. Materializes matches so the deadline
    covers the entire scan, not just the first match."""
    try:
        return iter(list(_regex.finditer(pattern, text, flags=flags, timeout=USER_PATTERN_TIMEOUT_SECONDS)))
    except Exception as exc:  # noqa: BLE001 - translated and re-raised
        raise _translate_error(pattern, exc) from None


def user_search(pattern: str, text: str, flags: int = 0) -> Optional["_regex.Match"]:
    """search with a wall-clock budget."""
    try:
        return _regex.search(pattern, text, flags=flags, timeout=USER_PATTERN_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - translated and re-raised
        raise _translate_error(pattern, exc) from None
