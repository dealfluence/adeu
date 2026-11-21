from __future__ import annotations

import functools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adeu import path_policy
from langchain_core.tools import BaseTool, ToolException
from pydantic import PrivateAttr

_DOCX_SUFFIX = ".docx"

LANGCHAIN_ID_DISCOVERY_HINT = (
    "Call `adeu_read_docx` with `mode='changes'` on the document again to list the "
    "current change (Chg:) and comment (Com:) ids — ids shift between document states."
)


class AdeuFileTool(BaseTool):
    """Capture the launcher's directory policy when a tool is constructed."""

    _folders: path_policy.AllowedDirectories = PrivateAttr()

    def __init__(self, **kwargs: Any) -> None:
        folders = path_policy.get_allowed_directories()
        super().__init__(**kwargs)
        self._folders = folders

    def _input_document(
        self,
        path: str,
        *,
        label: str = "DOCX file",
        other_inputs: tuple[str | None, ...] = (),
        output_path: str | None = None,
        default_output: Callable[[Path], Path] | None = None,
        diagnostic_output: Callable[[Path], Path] | None = None,
    ) -> Path:
        source = validate_path(path, must_exist=False, label=label, folders=self._folders)
        if self._folders.enabled:
            # Authorize the whole request before reporting existence or inspecting an existing output.
            for other in other_inputs:
                if other is not None:
                    self._folders.check(os.path.expanduser(other))
            candidate: str | Path | None = os.path.expanduser(output_path) if output_path is not None else None
            if candidate is None and default_output is not None:
                candidate = default_output(source)
            if candidate is not None:
                target = self._folders.check(candidate)
                if diagnostic_output is not None:
                    self._folders.check(diagnostic_output(target))
        return _require_docx_input(source, label)


def validate_path(
    path_str: str,
    *,
    must_exist: bool = True,
    label: str = "path",
    folders: path_policy.AllowedDirectories | None = None,
) -> Path:
    if not path_str or not path_str.strip():
        raise ToolException(f"The {label} cannot be empty. Supply a file path.")

    try:
        p = path_policy.resolved_input_path(os.path.expanduser(path_str), folders=folders)
    except path_policy.PathPolicyError as e:
        raise ToolException(str(e)) from e

    if must_exist and not p.exists():
        raise ToolException(f"The {label} does not exist: {p}. Supply an existing file.")

    return p


def validate_docx_path(
    path_str: str,
    *,
    must_exist: bool = True,
    label: str = "DOCX file",
    folders: path_policy.AllowedDirectories | None = None,
) -> Path:
    if not must_exist:
        return validate_output_path(path_str, folders=folders)

    return _require_docx_input(validate_path(path_str, must_exist=False, label=label, folders=folders), label)


def _require_docx_input(p: Path, label: str) -> Path:
    if not p.exists():
        raise ToolException(f"The {label} does not exist: {p}. Supply an existing file.")

    if p.suffix.lower() != _DOCX_SUFFIX:
        raise ToolException(
            f"The {label} must be a .docx file, got '{p.suffix}': {p}. "
            "Adeu only supports modern Word (.docx) format; .doc and other "
            "formats are not supported."
        )

    if not p.is_file():
        raise ToolException(f"The {label} exists but is not a regular file: {p}. Supply a .docx file.")

    return p


def validate_output_path(
    target: str | Path, *sources: Path, folders: path_policy.AllowedDirectories | None = None
) -> Path:
    """Apply the engine's tool policy to the requested or derived output."""
    try:
        return path_policy.checked_output_path(os.path.expanduser(target), *sources, folders=folders)
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
