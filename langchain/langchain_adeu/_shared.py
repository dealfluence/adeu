from __future__ import annotations

import functools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adeu import path_policy
from langchain_core.tools import ToolException

_DOCX_SUFFIX = ".docx"

LANGCHAIN_ID_DISCOVERY_HINT = (
    "Call `adeu_read_docx` with `mode='changes'` on the document again to list the "
    "current change (Chg:) and comment (Com:) ids — ids shift between document states."
)


def validate_path(path_str: str, *, must_exist: bool = True, label: str = "path") -> Path:
    if not path_str or not path_str.strip():
        raise ToolException(f"The {label} cannot be empty. Supply a file path.")

    try:
        p = path_policy.resolved_input_path(os.path.expanduser(path_str))
    except path_policy.PathPolicyError as e:
        raise ToolException(str(e)) from e

    if must_exist and not p.exists():
        raise ToolException(f"The {label} does not exist: {p}. Supply an existing file.")

    return p


def validate_docx_path(path_str: str, *, must_exist: bool = True, label: str = "DOCX file") -> Path:
    if not must_exist:
        return validate_output_path(path_str)

    p = validate_path(path_str, label=label)

    if p.suffix.lower() != _DOCX_SUFFIX:
        raise ToolException(
            f"The {label} must be a .docx file, got '{p.suffix}': {p}. "
            "Adeu only supports modern Word (.docx) format; .doc and other "
            "formats are not supported."
        )

    if not p.is_file():
        raise ToolException(f"The {label} exists but is not a regular file: {p}. Supply a .docx file.")

    return p


def validate_output_path(target: str | Path, *sources: Path) -> Path:
    """Apply the engine's tool policy to the requested or derived output."""
    try:
        return path_policy.checked_output_path(os.path.expanduser(target), *sources)
    except path_policy.PathPolicyError as e:
        raise ToolException(str(e)) from e


def wrap_tool_errors[F: Callable[..., Any]](func: F) -> F:
    """Decorator converting Adeu/python-docx exceptions to ToolException."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ToolException:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except FileNotFoundError as e:
            raise ToolException(f"File not found: {e}") from e
        except (ValueError, OSError) as e:
            raise ToolException(str(e)) from e
        except Exception as e:
            raise ToolException(f"{type(e).__name__}: {e}") from e

    return wrapper  # type: ignore[return-value]
