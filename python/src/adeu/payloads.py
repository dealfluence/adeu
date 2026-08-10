"""
Payload builders for error envelopes and response formatting.
"""

from typing import Any, Dict, List, Optional, Tuple


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


def shrink_batch_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts a standard batch stats dictionary to minimal report format.

    - Omits target_text, new_text, and clean_text for applied edits.
    - Retains verification fields: status, type, pages, heading_path, occurrences_modified, critic_markup.
    - Keeps match_mode only when non-strict (match_mode != "strict").
    - For failed edits: retains full error and target stub capped at <=80 chars.
    - Drops top-level "engine" field, retains "version".
    - Deduplicates skipped_details against edit errors and internal duplicates.
    """
    res = dict(stats)
    if "engine" in res:
        del res["engine"]

    edits = stats.get("edits", [])
    edit_errors = set()
    shrunk_edits: List[Dict[str, Any]] = []

    for edit in edits:
        if not isinstance(edit, dict):
            shrunk_edits.append(edit)
            continue

        err = edit.get("error")
        if err:
            err_str = str(err).strip()
            edit_errors.add(err_str)
            for line in err_str.splitlines():
                if line.strip():
                    edit_errors.add(line.strip())

        shrunk_e: Dict[str, Any] = {}
        status = edit.get("status")
        if status is not None:
            shrunk_e["status"] = status

        if "type" in edit:
            shrunk_e["type"] = edit["type"]

        if status == "failed":
            if "target_text" in edit and edit["target_text"] is not None:
                tgt = str(edit["target_text"])
                shrunk_e["target_text"] = tgt[:80]
            if err is not None:
                shrunk_e["error"] = err
        else:
            if "critic_markup" in edit and edit["critic_markup"] is not None:
                shrunk_e["critic_markup"] = edit["critic_markup"]

        for k in ("pages", "heading_path", "occurrences_modified"):
            if k in edit and edit[k] is not None:
                shrunk_e[k] = edit[k]

        match_mode = edit.get("match_mode")
        if match_mode is not None and match_mode != "strict":
            shrunk_e["match_mode"] = match_mode

        if edit.get("comment"):
            shrunk_e["comment"] = edit["comment"]

        if edit.get("warning"):
            shrunk_e["warning"] = edit["warning"]

        shrunk_edits.append(shrunk_e)

    res["edits"] = shrunk_edits

    raw_skipped = stats.get("skipped_details", [])
    deduped_skipped: List[Any] = []
    seen = set()
    for item in raw_skipped:
        item_str = str(item).strip()
        if item_str in edit_errors or item_str in seen:
            continue
        seen.add(item_str)
        deduped_skipped.append(item)

    res["skipped_details"] = deduped_skipped
    return res
