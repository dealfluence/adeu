# FILE: langchain/langchain_adeu/apply_changes.py
"""Apply a batch of edits and review actions to a .docx file.

Wraps `adeu.RedlineEngine.process_batch`. The agent supplies a flat list
of `DocumentChange` objects (modify / accept / reject / reply / insert_row
/ delete_row) and gets back a new .docx with the changes applied as
native Word Track Changes plus comments.

Three behaviors distinguish this tool from the others:

1.  The `changes` field is typed as `list[dict]` rather than
    `list[DocumentChange]`. Pydantic discriminated-union schemas don't
    round-trip cleanly through every chat model's tool-calling JSON
    Schema. We accept a permissive list and validate internally using
    `TypeAdapter(list[DocumentChange])` — same validation rigor, broader
    compatibility.

2.  `BatchValidationError` is caught explicitly and returned as content
    (with a `success=False` artifact). This is the only error type in
    the package the agent can fix and retry without external help, so
    we surface it as recoverable feedback rather than as a tool failure.

3.  Even on success the engine may skip individual edits — partial
    success is normal, not an error. The artifact carries per-edit stats
    so downstream LangGraph nodes can decide whether to retry skipped
    items.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from adeu.models import BatchChanges
from adeu.redline.engine import BatchValidationError, RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuApplyChangesInput(BaseModel):
    """Input schema for `AdeuApplyChanges`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="Why am I applying these changes to the document? State this reason before any other parameter.",
    )
    file_path: str = Field(
        description="Absolute path to the source .docx file to edit.",
    )
    author_name: str = Field(
        description=(
            "Name to appear as the author on all tracked changes and "
            "comments created by this batch (e.g., 'AI Reviewer'). Must be "
            "non-empty."
        ),
    )
    changes: list[dict[str, Any]] = Field(
        description=(
            "List of changes to apply. Each item must include a `type` field "
            "and is validated against the Adeu DocumentChange schema:\n\n"
            "MODIFY (search and replace text — most common):\n"
            "  {'type': 'modify', 'target_text': 'exact phrase to find', "
            "'new_text': 'replacement text', 'comment': 'optional rationale', "
            "'match_mode': 'strict'|'first'|'all', 'regex': false|true}\n"
            "  - target_text must match uniquely (when match_mode='strict'); if ambiguous, either "
            "include surrounding context, or set match_mode='first' (first occurrence) or "
            "match_mode='all' (every occurrence).\n"
            "  - new_text supports Markdown: '# Heading 1' through "
            "'###### Heading 6', '**bold**', '_italic_', and '\\n\\n' to split "
            "into multiple paragraphs.\n"
            "  - empty new_text deletes the matched text.\n"
            "  - do NOT write CriticMarkup tags ({++, {--, {>>) — use the "
            "comment parameter for comments instead.\n\n"
            "ACCEPT or REJECT (finalize or revert a tracked change by ID):\n"
            "  {'type': 'accept', 'target_id': 'Chg:12'}\n"
            "  {'type': 'reject', 'target_id': 'Chg:12'}\n\n"
            "REPLY (respond to a comment by ID):\n"
            "  {'type': 'reply', 'target_id': 'Com:5', 'text': 'response body'}\n\n"
            "INSERT_ROW or DELETE_ROW (structural table edits):\n"
            "  {'type': 'insert_row', 'target_text': 'cell text in adjacent row', "
            "'position': 'above'|'below', 'cells': ['col1', 'col2', ...]}\n"
            "  {'type': 'delete_row', 'target_text': 'unique text inside row to delete'}\n\n"
            "ID VOLATILITY: 'Chg:N' and 'Com:N' shift between document states. "
            "Always read the document immediately before any accept/reject/reply — "
            "do not reuse IDs from earlier turns.\n\n"
            "BATCH SEMANTICS: Changes apply sequentially — each change is "
            "evaluated against the document state produced by the changes "
            "before it, so dependent edits may be chained in one batch (e.g. "
            "rename X to Y, then modify Y — the second edit must target Y, "
            "the text as it reads after the rename). Any validation failure "
            "rejects the whole batch transactionally."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path for the edited output. When omitted, defaults to "
            "'<stem>_processed.docx' in the same directory as the input "
            "(or overwrites the input if its stem already ends in '_processed' "
            "or '_redlined'). Must differ from input_path unless the input is "
            "already a processed/redlined file. Ignored when dry_run=True."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, simulate the batch without writing any file and return a "
            "detailed per-edit preview report. Each edit's report includes the "
            "applied/failed status, a CriticMarkup preview of the change in "
            "context, a clean-text preview of how the document would read after "
            "acceptance, plus any per-edit warnings or errors. Use this to "
            "verify ambiguous edits before committing, or to let the agent "
            "self-review a batch before producing the final output. The input "
            "file is never modified. Default False."
        ),
    )


_DESCRIPTION = (
    "Apply a batch of edits and review actions to a Microsoft Word (.docx) "
    "file. Edits are committed as native Word Track Changes attributed to "
    "the author_name you provide.\n\n"
    "Supported change types: 'modify' (search-and-replace text with optional "
    "comment), 'accept' or 'reject' (finalize/revert a tracked change by ID), "
    "'reply' (respond to a comment by ID), 'insert_row' or 'delete_row' "
    "(structural table edits).\n\n"
    "Changes in a batch apply SEQUENTIALLY: each change is evaluated against "
    "the document state produced by the changes before it, so dependent edits "
    "may be chained in one batch (e.g. rename X to Y, then modify Y — the "
    "second edit must target Y, the text as it reads after the rename). If "
    "any change fails validation, the whole batch is rejected "
    "transactionally.\n\n"
    "ID VOLATILITY: 'Chg:N' and 'Com:N' identifiers shift between document "
    "states. Always call adeu_read_docx immediately before any "
    "accept/reject/reply action to get fresh IDs — do not reuse IDs from "
    "earlier in the conversation.\n\n"
    "If validation fails (e.g. target_text doesn't uniquely match), the "
    "whole batch is rejected transactionally and the tool returns the "
    "per-edit error list as content with success=False in the artifact, so "
    "you can correct the errors and retry. Edits can still be skipped for "
    "non-validation reasons at apply time — check "
    "artifact['edits_applied'] vs artifact['edits_skipped'].\n\n"
    "Set dry_run=True to simulate the batch without writing any file. The "
    "response includes a per-edit preview report (CriticMarkup preview, "
    "clean-text preview, status, warnings, errors) so you can verify "
    "ambiguous edits before committing."
)


class AdeuApplyChanges(BaseTool):
    """LangChain tool: apply tracked-change edits to a .docx file."""

    name: str = "adeu_apply_changes"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuApplyChangesInput  # type: ignore[assignment]
    response_format: Literal["content_and_artifact"] = "content_and_artifact"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        file_path: str,
        author_name: str,
        changes: list[dict[str, Any]],
        output_path: str | None = None,
        dry_run: bool = False,
    ) -> tuple[str, dict[str, Any]]:

        if not author_name.strip():
            raise ToolException(
                "author_name cannot be empty or whitespace-only. Provide a non-blank identifier such as 'AI Reviewer'."
            )

        if not changes:
            raise ToolException(
                "changes list cannot be empty. Provide at least one change "
                "(type: modify, accept, reject, reply, insert_row, or delete_row)."
            )

        source = validate_docx_path(file_path, label="DOCX file")

        try:
            # Replaced list[DocumentChange] with BatchChanges to automatically
            # rescue stringified JSON elements sent by Gemini and other LLMs.
            adapter = TypeAdapter(BatchChanges)
            validated_changes = adapter.validate_python(changes)
        except ValidationError as e:
            return _format_validation_failure_content(e), _failure_artifact(
                source, output_path, author_name, [_format_pydantic_error(e)]
            )

        # Resolve the destination only when we're actually going to write.
        # For dry-run we still accept output_path (so the agent can pre-plan
        # the eventual destination) but don't bind it to a target Path —
        # the overwrite guard would otherwise complain about paths that
        # will never be touched on this turn.
        target = None if dry_run else _resolve_output_path(source, output_path)

        raw_bytes = source.read_bytes()
        sanitized_bytes = strip_bom_from_docx_bytes(raw_bytes)
        stream = BytesIO(sanitized_bytes)

        engine = RedlineEngine(stream, author=author_name)

        try:
            stats = engine.process_batch(validated_changes, dry_run=dry_run)
        except BatchValidationError as e:
            content = "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(e.errors)
            return content, _failure_artifact(source, output_path, author_name, e.errors, dry_run=dry_run)

        # Success path: write the output (skipped in dry-run) and return per-edit stats.
        if not dry_run:
            assert target is not None  # narrow for type-checkers; guaranteed by the branch above
            result_stream = engine.save_to_stream()
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(result_stream.getvalue())

        content_lines = _build_success_content(stats, target, dry_run=dry_run)

        artifact: dict[str, Any] = {
            "input_path": str(source),
            "output_path": str(target) if target is not None else None,
            "author_name": author_name,
            "dry_run": dry_run,
            "success": True,
            "validation_errors": None,
            "actions_applied": stats["actions_applied"],
            "actions_skipped": stats["actions_skipped"],
            "edits_applied": stats["edits_applied"],
            "edits_skipped": stats["edits_skipped"],
            "skipped_details": stats.get("skipped_details", []),
            "edits": stats.get("edits", []),
        }
        return "\n".join(content_lines), artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        author_name: str,
        changes: list[dict[str, Any]],
        output_path: str | None = None,
        dry_run: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self._run, reasoning, file_path, author_name, changes, output_path, dry_run)


def _resolve_output_path(source: Path, requested: str | None) -> Path:
    """Decide where the processed file should be written.

    Mirrors the MCP server's convention: if the source stem already ends
    with `_processed` or `_redlined`, the default is to overwrite the
    source (the agent is iterating on its own output). Otherwise default
    to `<stem>_processed.docx` next to the source.
    """
    if requested is None:
        if source.stem.endswith("_processed") or source.stem.endswith("_redlined"):
            return source
        return source.with_name(f"{source.stem}_processed{source.suffix}")

    target = validate_docx_path(requested, must_exist=False, label="output path")

    # Allow overwrite only when the source is already a processed/redlined
    # iteration of the agent's own work. Refuse silent destruction of the
    # original draft.
    if target == source and not (source.stem.endswith("_processed") or source.stem.endswith("_redlined")):
        raise ToolException(
            f"Output path must differ from input path; refusing to overwrite "
            f"the source file at {source}. Pick a different output_path, or "
            f"omit output_path to use the default '<stem>_processed.docx'."
        )
    return target


def _failure_artifact(
    source: Path,
    output_path: str | None,
    author_name: str,
    errors: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build an artifact for a rejected batch — no output file was written."""
    return {
        "input_path": str(source),
        "output_path": None,
        "requested_output_path": output_path,
        "author_name": author_name,
        "dry_run": dry_run,
        "success": False,
        "validation_errors": errors,
        "actions_applied": 0,
        "actions_skipped": 0,
        "edits_applied": 0,
        "edits_skipped": 0,
        "skipped_details": [],
        "edits": [],
    }


def _build_success_content(
    stats: dict[str, Any],
    target: Path | None,
    *,
    dry_run: bool,
) -> list[str]:
    """Assemble the human-readable content block for a successful batch.

    Mirrors the per-edit detail format used by the MCP server's
    `process_document_batch` dry-run path so behavior is consistent across
    surfaces. Each per-edit report carries enough preview context (CriticMarkup
    string, clean text, warnings) for an LLM to self-review and decide whether
    to commit, abort, or rewrite ambiguous edits.
    """
    if dry_run:
        lines = ["Dry-run simulation complete. No file was written."]
    else:
        lines = [f"Batch complete. Saved to: {target}"]

    lines.append(f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.")
    lines.append(f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped.")

    edit_reports = stats.get("edits") or []
    if edit_reports:
        lines.append("")
        lines.append("Detailed Edit Reports:")
        for i, report in enumerate(edit_reports, start=1):
            status = "applied" if report.get("status") == "applied" else "failed"
            indicator = "[applied]" if status == "applied" else "[failed]"
            lines.append(f"Edit {i} {indicator}:")
            lines.append(f"  Target: '{report.get('target_text', '')}'")
            lines.append(f"  New text: '{report.get('new_text', '')}'")
            if report.get("warning"):
                lines.append(f"  Warning: {report['warning']}")
            if report.get("error"):
                lines.append(f"  Error: {report['error']}")
            if report.get("critic_markup"):
                lines.append(f"  Preview (CriticMarkup): {report['critic_markup']}")
            if report.get("clean_text"):
                lines.append(f"  Clean text preview: {report['clean_text']}")

    if stats.get("skipped_details"):
        lines.append("")
        lines.append("Skipped Details:")
        lines.extend(stats["skipped_details"])

    return lines


def _format_pydantic_error(e: Any) -> str:
    """Compact string for a Pydantic ValidationError for artifact storage."""
    return str(e)


def _format_validation_failure_content(e: Any) -> str:
    """Human-readable content for a Pydantic ValidationError.

    Pydantic's default repr is verbose but informative. We add a leading
    line that frames this as a recoverable validation step the LLM can
    fix and retry, not a generic tool failure.
    """
    return f"Batch rejected during schema validation. Fix the per-field errors below and resubmit:\n\n{e}"


__all__ = ["AdeuApplyChanges", "AdeuApplyChangesInput"]
