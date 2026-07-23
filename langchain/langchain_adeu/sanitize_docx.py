# FILE: langchain/langchain_adeu/sanitize_docx.py
"""Sanitize a .docx file — strip metadata, comments, and tracked changes.

Wraps `adeu.sanitize.sanitize_docx`. Three modes are exposed via the
combination of `keep_markup` and `baseline_path`:

  - Full sanitize (default): strip everything dangerous, optionally
    auto-accept all pending tracked changes.
  - Keep-markup mode (`keep_markup=True`): preserve open tracked changes
    and unresolved comments; strip resolved comments, all metadata, and
    optionally normalize author identity.
  - Baseline mode (`baseline_path=...`): recompute the agent's edits as a
    clean delta against a baseline document. Use this when track changes
    was off during editing, or to collapse multiple rounds of redlining
    into a single clean revision against the original.

A structured `SanitizeResult` is surfaced via the artifact so downstream
LangGraph nodes can branch on counts and warnings without parsing the
human-readable report text.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from adeu.sanitize import sanitize_docx as _sanitize_docx
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuSanitizeDocxInput(BaseModel):
    """Input schema for `AdeuSanitizeDocx`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="Why am I sanitizing this document? State this reason before any other parameter.",
    )
    file_path: str = Field(
        description="Absolute path to the .docx file to sanitize.",
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path for the sanitized output. When omitted, defaults "
            "to '<stem>_sanitized.docx' in the same directory as the input. "
            "Must differ from input_path — overwriting the source is rejected."
        ),
    )
    keep_markup: bool = Field(
        default=False,
        description=(
            "When True, preserve existing tracked changes and unresolved "
            "comments; strip resolved comments, all metadata, and optionally "
            "normalize author identity. Use this when sending a redline to "
            "counterparty. Ignored if baseline_path is provided."
        ),
    )
    baseline_path: str | None = Field(
        default=None,
        description=(
            "Path to the original/baseline document. When provided, the tool "
            "recomputes your changes as a clean delta against this baseline "
            "and produces a tracked-changes redline. Use this when track "
            "changes was off during editing, or to collapse multiple rounds "
            "of redlining into a single clean revision."
        ),
    )
    author: str | None = Field(
        default=None,
        description=(
            "When set, replace all author names on tracked changes and "
            "comments with this value. Only meaningful with keep_markup=True "
            "or baseline_path; ignored in full sanitize mode (which removes "
            "all author attribution anyway)."
        ),
    )
    accept_all: bool = Field(
        default=False,
        description=(
            "Full sanitize mode only: accept all unresolved tracked changes "
            "before stripping. REQUIRED when the document contains "
            "unresolved changes — otherwise the tool will refuse with a "
            "SanitizeError explaining what's blocking. The report will list "
            "every change that was auto-accepted, including a high-visibility "
            "warning if multiple distinct authors are detected."
        ),
    )
    allow_low_similarity_baseline: bool = Field(
        default=False,
        description=(
            "Baseline mode only: proceed even when the baseline shares less "
            "than half of its content with the working document. Without "
            "this, such a mismatch is blocked to prevent accidental overwrites "
            "from selecting the wrong baseline file."
        ),
    )


_DESCRIPTION = (
    "Sanitize a Microsoft Word (.docx) file by stripping dangerous metadata "
    "(rsids, author names, template paths, DMS metadata, hidden text, "
    "orphaned content) and producing an audit report of everything removed. "
    "Use this before sending documents to external parties.\n\n"
    "Three modes via parameter combination:\n"
    "- Full sanitize (default): strip everything. Use accept_all=True if "
    "the document has unresolved tracked changes — otherwise the tool will "
    "block and tell you what's pending.\n"
    "- keep_markup=True: preserve open tracked changes and unresolved "
    "comments; strip resolved comments and all metadata. Use when sending a "
    "redline to counterparty. Pair with author='...' to normalize identity.\n"
    "- baseline_path='...': recompute your edits as a clean delta against "
    "the baseline document. Use when track changes was off during editing.\n\n"
    "The full SanitizeResult (counts, warnings, report) is available as the "
    "tool's artifact for structured downstream consumption."
)


class AdeuSanitizeDocx(BaseTool):
    """LangChain tool: sanitize a .docx for safe external distribution."""

    name: str = "adeu_sanitize_docx"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuSanitizeDocxInput  # type: ignore[assignment]
    response_format: Literal["content_and_artifact"] = "content_and_artifact"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        file_path: str,
        output_path: str | None = None,
        keep_markup: bool = False,
        baseline_path: str | None = None,
        author: str | None = None,
        accept_all: bool = False,
        allow_low_similarity_baseline: bool = False,
    ) -> tuple[str, dict[str, Any]]:

        source = validate_docx_path(file_path, label="DOCX file")
        target = _resolve_output_path(source, output_path)

        baseline_resolved: Path | None = None
        if baseline_path is not None:
            baseline_resolved = validate_docx_path(baseline_path, label="baseline document")

        target.parent.mkdir(parents=True, exist_ok=True)
        result = _sanitize_docx(
            input_path=str(source),
            output_path=str(target),
            keep_markup=keep_markup,
            baseline_path=str(baseline_resolved) if baseline_resolved else None,
            author=author,
            accept_all=accept_all,
            allow_low_similarity_baseline=allow_low_similarity_baseline,
        )

        artifact: dict[str, Any] = {
            "input_path": str(source),
            **asdict(result),
        }
        if baseline_resolved is not None:
            artifact["baseline_path"] = str(baseline_resolved)

        return result.report_text, artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        output_path: str | None = None,
        keep_markup: bool = False,
        baseline_path: str | None = None,
        author: str | None = None,
        accept_all: bool = False,
        allow_low_similarity_baseline: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            file_path,
            output_path,
            keep_markup,
            baseline_path,
            author,
            accept_all,
            allow_low_similarity_baseline,
        )


def _resolve_output_path(source: Path, requested: str | None) -> Path:
    """Decide where the sanitized file should be written.

    Mirrors the convention from `AdeuAcceptAllChanges`: when no output
    path is requested, default to '<stem>_sanitized.docx' next to the
    source; never silently overwrite the input.
    """
    if requested is None:
        return source.with_name(f"{source.stem}_sanitized{source.suffix}")

    target = validate_docx_path(requested, must_exist=False, label="output path")

    if target == source:
        raise ToolException(
            f"Output path must differ from input path; refusing to overwrite "
            f"the source file at {source}. Pick a different output_path or "
            "omit output_path to use the default '<stem>_sanitized.docx'."
        )
    return target


__all__ = ["AdeuSanitizeDocx", "AdeuSanitizeDocxInput"]
