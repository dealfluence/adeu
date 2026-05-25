# FILE: langchain/langchain_adeu/_shared.py
"""Internal helpers shared across langchain-adeu tools.

This module deliberately keeps zero LangChain-specific logic in the
business layer — the tool classes are thin orchestrators that:
  1. Validate input paths via `validate_docx_path` / `validate_path`
  2. Call directly into the `adeu.*` SDK
  3. Convert Adeu's domain errors into `ToolException` so LangChain's
     agent middleware can present them cleanly to the model.

All path validation happens at the tool boundary so engine code never
sees a missing/wrong-extension file (those errors would bubble up as
opaque `python-docx` exceptions otherwise).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.tools import ToolException

_DOCX_SUFFIX = ".docx"


def validate_path(path_str: str, *, must_exist: bool = True, label: str = "path") -> Path:
    """Validate a filesystem path string and return a resolved `Path`.

    Args:
        path_str: The path as provided by the LLM.
        must_exist: When True (default), raise if the path doesn't exist on disk.
        label: Human-readable label for the path used in error messages
            (e.g. "input file", "baseline document").

    Raises:
        ToolException: If the path is empty, malformed, or (when
            `must_exist=True`) does not exist.

    Returns:
        A resolved `Path` object.
    """
    if not path_str or not path_str.strip():
        raise ToolException(f"The {label} cannot be empty.")

    try:
        p = Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise ToolException(f"The {label} '{path_str}' is not a valid filesystem path: {e}") from e

    if must_exist and not p.exists():
        raise ToolException(f"The {label} does not exist: {p}")

    return p


def validate_docx_path(path_str: str, *, must_exist: bool = True, label: str = "DOCX file") -> Path:
    """Validate a path that must point to a `.docx` file.

    Performs the same checks as `validate_path`, then verifies the suffix.

    Args:
        path_str: The path as provided by the LLM.
        must_exist: When True (default), raise if the file doesn't exist.
        label: Human-readable label used in error messages.

    Raises:
        ToolException: On the same conditions as `validate_path`, plus
            when the suffix is not `.docx`.

    Returns:
        A resolved `Path` to the DOCX file.
    """
    p = validate_path(path_str, must_exist=must_exist, label=label)

    if p.suffix.lower() != _DOCX_SUFFIX:
        raise ToolException(
            f"The {label} must be a .docx file, got '{p.suffix}': {p}. "
            "Adeu only supports modern Word (.docx) format; .doc and other "
            "formats are not supported."
        )

    if must_exist and not p.is_file():
        raise ToolException(f"The {label} exists but is not a regular file: {p}")

    return p


def wrap_tool_errors[F: Callable[..., Any]](func: F) -> F:
    """Decorator that converts Adeu/python-docx exceptions to `ToolException`.

    Why: agents are far more useful when tool failures arrive as readable
    `ToolMessage` content than when they crash the run loop. By raising
    `ToolException` (rather than the original exception type), LangChain's
    default `handle_tool_errors` middleware will format the message and
    feed it back to the model, which can then correct its input and retry.

    `ToolException` and `KeyboardInterrupt`/`SystemExit` are re-raised
    untouched. Everything else is wrapped.

    Use sparingly: only wrap entry-point functions called directly from
    `_run` / `_arun`. Wrapping internal helpers hides stack traces during
    development.
    """

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
            # Catch-all for python-docx, lxml, and other deep-stack failures.
            # We deliberately surface the type name so debugging is possible
            # from the agent's transcript alone.
            raise ToolException(f"{type(e).__name__}: {e}") from e

    return wrapper  # type: ignore[return-value]
