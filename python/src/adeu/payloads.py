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
