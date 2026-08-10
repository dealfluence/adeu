"""
Payload builders for error envelopes and response formatting.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from adeu.utils.text import clamp_text

# Ceiling for one applied edit in a minimal report, in the approx-token unit
# used by the report budget tests (len(json) // 4). Errors and engine warnings
# ride free: an advisory the caller must act on is never truncated, exactly as
# a failed edit keeps its full error.
MINIMAL_EDIT_TOKEN_BUDGET = 40

# A failed edit echoes just enough of the caller's target_text to identify
# which edit failed; the error message carries the diagnosis.
FAILED_TARGET_STUB_CAP = 80

# The four CriticMarkup bubble forms. Every delimiter is exactly 3 characters
# ("{--"/"--}", "{++"/"++}", "{=="/"==}", "{>>"/"<<}"), which is what lets a
# bubble body be clamped in place without disturbing its delimiters.
_CRITIC_BUBBLE_RE = re.compile(r"\{--.*?--\}|\{\+\+.*?\+\+\}|\{==.*?==\}|\{>>.*?<<\}", re.DOTALL)
_CRITIC_DELIM_LEN = 3

# Fields exempt from the per-edit budget: free prose the agent must read in
# full to recover from, or act on.
_UNBUDGETED_FIELDS = ("error", "warning")

# Smallest bubble body worth emitting — below this the preview stops being
# evidence of anything.
_MIN_BUBBLE_BODY = 8


def failure_envelope(
    code: str,
    failed: List[Tuple[int, str]],
    message: str,
    errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Builds a uniform machine-readable failure envelope.

    Args:
        code: Stable error code string (e.g. "invalid_changes_file", "batch_validation_failed").
        failed: List of (0-based_batch_index, reason_string) tuples.
        message: Human-readable error message.
        errors: Optional list of raw prose error strings for backward compatibility.

    Returns:
        Dict with keys "error", "failed", and "message" (and optionally "errors").
    """
    clean_message = " ".join(line.strip() for line in message.splitlines() if line.strip())
    res: Dict[str, Any] = {
        "error": code,
        "failed": [{"index": i, "reason": r} for i, r in failed],
        "message": clean_message,
    }
    if errors is not None:
        res["errors"] = errors
    return res


def _changed_span(markup: str) -> str:
    """
    The CriticMarkup bubbles of a preview with the surrounding document context
    dropped. Context is the cheapest thing to give up: it repeats text the
    caller can read from the document, whereas the bubbles ARE the evidence
    that the edit landed as asked.
    """
    bubbles = list(_CRITIC_BUBBLE_RE.finditer(markup))
    if not bubbles:
        return markup
    return markup[bubbles[0].start() : bubbles[-1].end()]


def _clamp_bubble(bubble: str, body_cap: int) -> str:
    """Shortens a bubble's body, leaving its opening and closing delimiter intact."""
    body = bubble[_CRITIC_DELIM_LEN:-_CRITIC_DELIM_LEN]
    return bubble[:_CRITIC_DELIM_LEN] + clamp_text(body, body_cap) + bubble[-_CRITIC_DELIM_LEN:]


def _shrink_critic_markup(markup: str, cap: int) -> str:
    """
    Bounds a CriticMarkup preview to roughly `cap` characters without ever
    cutting a bubble open: surrounding context goes first, then each bubble's
    body is clamped in place. Every {--…--}/{++…++}/{==…==}/{>>…<<} therefore
    stays balanced — a bare delimiter fragment corrupts the markup for every
    consumer, including this package's own preview regexes (AI_CONTEXT.md).
    """
    span = _changed_span(markup)
    if len(span) <= cap:
        return span
    bubbles = _CRITIC_BUBBLE_RE.findall(span)
    if not bubbles:
        # No markup to protect: a plain-text preview is safe to cut.
        return clamp_text(span, cap)
    body_cap = max(_MIN_BUBBLE_BODY, cap // len(bubbles) - 2 * _CRITIC_DELIM_LEN)
    return _CRITIC_BUBBLE_RE.sub(lambda m: _clamp_bubble(m.group(0), body_cap), span)


def _within_budget(edit: Dict[str, Any]) -> bool:
    """
    Whether an edit report fits MINIMAL_EDIT_TOKEN_BUDGET, measured the way the
    report budget is specified: approx-tokens (len(json) // 4) over the
    serialized edit, ignoring the fields exempt from the budget.
    """
    budgeted = {k: v for k, v in edit.items() if k not in _UNBUDGETED_FIELDS}
    return len(json.dumps(budgeted)) // 4 <= MINIMAL_EDIT_TOKEN_BUDGET


def _fit_to_budget(edit: Dict[str, Any]) -> None:
    """
    Spends the per-edit budget in priority order, in place.

    The engine's verification evidence outranks the locator: `pages` already
    says where the edit landed, so the preview's context and then the heading
    path are surrendered before a CriticMarkup bubble is touched. Only when
    even the bare bubbles overrun the budget are their bodies clamped — and
    then in place, so the markup stays valid.
    """
    if _within_budget(edit):
        return

    markup = edit.get("critic_markup")
    if markup:
        edit["critic_markup"] = _changed_span(markup)
        if _within_budget(edit):
            return

    path = edit.get("heading_path")
    if path and " > " in path:
        # Deepest heading only: the ancestors are the least specific part.
        edit["heading_path"] = path.rsplit(" > ", 1)[-1]
        if _within_budget(edit):
            return
    if "heading_path" in edit:
        del edit["heading_path"]
        if _within_budget(edit):
            return

    if markup:
        # Measure the real JSON (escaping included) rather than predicting it.
        preview_cap = len(markup)
        while preview_cap > _MIN_BUBBLE_BODY and not _within_budget(edit):
            preview_cap = preview_cap * 4 // 5
            edit["critic_markup"] = _shrink_critic_markup(markup, preview_cap)


def _minimal_edit(edit: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuilds one edit report with the caller's echoes dropped."""
    status = edit.get("status")
    minimal: Dict[str, Any] = {}
    if status is not None:
        minimal["status"] = status
    if "type" in edit:
        minimal["type"] = edit["type"]

    if status == "failed":
        if edit.get("target_text") is not None:
            minimal["target_text"] = clamp_text(str(edit["target_text"]), FAILED_TARGET_STUB_CAP)
    elif edit.get("critic_markup"):
        minimal["critic_markup"] = edit["critic_markup"]

    if edit.get("pages"):
        minimal["pages"] = edit["pages"]
    heading_path = str(edit.get("heading_path") or "").strip()
    if heading_path:
        minimal["heading_path"] = heading_path
    if edit.get("occurrences_modified") is not None:
        minimal["occurrences_modified"] = edit["occurrences_modified"]
    match_mode = edit.get("match_mode")
    if match_mode is not None and match_mode != "strict":
        minimal["match_mode"] = match_mode
    if edit.get("warning"):
        minimal["warning"] = edit["warning"]
    if edit.get("error"):
        minimal["error"] = edit["error"]

    if status != "failed":
        _fit_to_budget(minimal)
    return minimal


def _error_lines(error: Any) -> List[str]:
    """
    Every form in which a batch may repeat an edit's error: the whole message,
    or one of its lines.
    """
    text = str(error).strip()
    return [text] + [line.strip() for line in text.splitlines() if line.strip()]


def _dedupe_skipped(details: Any, edit_errors: Set[str]) -> List[Any]:
    """
    Batch-level skipped details repeat the per-edit errors verbatim; a minimal
    report states each reason once.
    """
    deduped: List[Any] = []
    seen: Set[str] = set()
    for item in details:
        key = str(item).strip()
        if key in edit_errors or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def shrink_batch_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reshapes standard batch stats into the minimal report.

    Two classes of field share an edit report: echoes of caller input
    (`target_text`, `new_text`, `clean_text`, `comment`) and engine-produced
    verification evidence (`critic_markup`, `pages`, `heading_path`,
    `occurrences_modified`). Minimal mode drops the echoes — the caller wrote
    that text in the same turn and gains nothing by being sold it back — and
    keeps the evidence, bounded to MINIMAL_EDIT_TOKEN_BUDGET approx-tokens per
    applied edit. `clean_text` goes as a duplicate of `critic_markup`, which
    already shows the same span with the change marked up.

    A failed edit keeps its full error plus a target stub of at most
    FAILED_TARGET_STUB_CAP chars, so the agent can tell which edit failed and
    why. Batch level: `engine` goes (a constant per binary), `version` stays,
    and skipped details are deduplicated against the per-edit errors. Keys
    absent from `stats` are never invented.
    """
    res = dict(stats)
    res.pop("engine", None)

    edit_errors: Set[str] = set()
    if "edits" in stats:
        shrunk_edits: List[Any] = []
        for edit in stats["edits"]:
            if not isinstance(edit, dict):
                shrunk_edits.append(edit)
                continue
            if edit.get("error"):
                edit_errors.update(_error_lines(edit["error"]))
            shrunk_edits.append(_minimal_edit(edit))
        res["edits"] = shrunk_edits

    if "skipped_details" in stats:
        res["skipped_details"] = _dedupe_skipped(stats["skipped_details"], edit_errors)
    return res
