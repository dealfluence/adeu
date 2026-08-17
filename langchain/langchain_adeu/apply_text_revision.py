# FILE: langchain/langchain_adeu/apply_text_revision.py
"""Apply a whole-document text revision as tracked changes.

Wraps `adeu.text_revision.apply_text_revision_core`. Instead of
hand-authoring a `changes` batch (as `AdeuApplyChanges` requires), the
agent supplies the COMPLETE revised clean text of the document; the engine
diffs it against the document's clean view and materializes the delta as
native Word tracked changes.

Two invariants make this tool safe to hand to a model, and both are
reported as `success=False` payloads rather than raised errors so the agent
can correct its input and retry:

  - **Clean-text verification gate.** After applying, the engine re-reads
    the result's clean view and compares it against the supplied text. On a
    mismatch it writes a `<stem>.unverified.docx` diagnostic copy and does
    NOT write the requested output file. That is a failure, and the tool
    reports it as one (`status="verification_failed"`). The engine attaches
    its own post-gate stats to the exception — every edit re-reported as
    `status="failed"`, `edits_applied` zeroed and rolled into
    `edits_skipped` (`text_revision.py:259-273`) — and the artifact relays
    them verbatim, so the agent sees what the engine actually reported
    rather than a synthesized blank. Only `status` is overridden
    (the engine's batch status stays `"ok"`/`"partial"` because the batch
    itself applied; the run as a whole did not).
  - **Major-deletion guard.** Text that is >50% shorter than the document's
    clean text (>75% for documents under 2000 characters) is refused
    unless `allow_major_deletions=True`, because it is nearly always a
    partial extract rather than an intentional mass deletion
    (`status="error"`).

Only input-shape problems detected before the engine call — a bad path, a
blank `revised_text`, or CriticMarkup in `revised_text` — raise
`ToolException`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from adeu.text_revision import (
    TextRevisionVerificationError,
    apply_text_revision_core,
    check_criticmarkup,
)
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuApplyTextRevisionInput(BaseModel):
    """Input schema for `AdeuApplyTextRevision`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description=("Why am I rewriting this document wholesale? State this reason before any other parameter."),
    )
    file_path: str = Field(
        description="Absolute path to the source .docx file.",
    )
    revised_text: str = Field(
        description=(
            "The COMPLETE revised clean text of the WHOLE document. This is "
            "diffed against the document's clean view, so any content you omit "
            "is applied as a tracked deletion — passing a single page will "
            "delete every other page. Read the document with "
            "`adeu_read_docx(clean_view=True, page='all')` first, edit that "
            "text, and pass all of it back. Must contain NO CriticMarkup "
            "({++..++}, {--..--}, {~~..~>..~~}, {==..==}, {>>..<<}): the "
            "comparison is against clean text, so markup tokens would be "
            "inserted into the document as literal prose."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path for the redlined output .docx. When omitted, "
            "defaults to '<stem>_redlined.docx' next to the input (an input "
            "whose stem already ends in '_redlined' or '_processed' is "
            "updated in place)."
        ),
    )
    author: str | None = Field(
        default=None,
        description=(
            "Author name recorded on the generated tracked changes. Defaults "
            "to the ADEU_AUTHOR environment variable, then the OS user, then "
            "'Adeu AI'."
        ),
    )
    allow_major_deletions: bool = Field(
        default=False,
        description=(
            "Allow deleting >50% of the document's characters (>75% for "
            "documents under 2000 characters). Without this, such a revision "
            "is refused with success=False, because a text that short is "
            "almost always a partial extract rather than an intentional mass "
            "deletion. Set True only when the deletion really is intended."
        ),
    )


_DESCRIPTION = (
    "Apply a whole-document text revision to a Microsoft Word (.docx) file. "
    "You supply the COMPLETE revised clean text; Adeu diffs it against the "
    "document's clean view and writes the delta as native Word tracked "
    "changes, preserving formatting, styles, and XML structure.\n\n"
    "Use this when you are rewriting substantial prose and it is easier to "
    "hand back the whole edited text than to enumerate edits. For targeted "
    "search-and-replace edits, comments, or accept/reject actions, use "
    "`adeu_apply_changes` instead.\n\n"
    "CRITICAL: `revised_text` must cover the ENTIRE document. Anything you "
    "omit becomes a tracked deletion. Read with "
    "`adeu_read_docx(clean_view=True, page='all')`, edit that text, and pass "
    "all of it back — never a single page, and never text containing "
    "CriticMarkup.\n\n"
    "Two guards return success=False with an explanation instead of writing a "
    "file, so you can correct and retry:\n"
    "- Post-apply verification: if the applied document's clean text does not "
    "match your text (e.g. headings or table cells that cannot be deleted via "
    "text replacement), nothing is written to output_path and a "
    "'<stem>.unverified.docx' diagnostic copy is kept — it is NOT the "
    "requested document.\n"
    "- Major-deletion guard: text >50% shorter than the document (>75% under "
    "2000 characters) is refused unless allow_major_deletions=True.\n\n"
    "The input file is never modified."
)


class AdeuApplyTextRevision(BaseTool):
    """LangChain tool: apply revised whole-document text as tracked changes."""

    name: str = "adeu_apply_text_revision"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuApplyTextRevisionInput  # type: ignore[assignment]
    response_format: Literal["content_and_artifact"] = "content_and_artifact"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        file_path: str,
        revised_text: str,
        output_path: str | None = None,
        author: str | None = None,
        allow_major_deletions: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        source = validate_docx_path(file_path, label="DOCX file")

        if not revised_text or not revised_text.strip():
            raise ToolException(
                "revised_text cannot be empty. Provide the complete revised clean text of the document."
            )

        # The engine owns the token set (`check_criticmarkup`,
        # python/src/adeu/text_revision.py:66-73) — call it rather than copying
        # it, so a change to the tokens can never silently diverge here. It is
        # re-checked inside the engine call; running it up front turns the
        # failure into a raised ToolException (an input-shape problem the agent
        # must fix) instead of a success=False payload.
        try:
            check_criticmarkup(revised_text)
        except ValueError as e:
            raise ToolException(str(e)) from e

        # No default is computed here: the engine owns the '<stem>_redlined.docx'
        # convention, including updating an already-redlined input in place
        # (python/src/adeu/text_revision.py:239-246).
        target: Path | None = None
        if output_path is not None:
            target = validate_docx_path(output_path, must_exist=False, label="output path")

        try:
            # Returns (stats, output_path) — in that order (text_revision.py:215).
            stats, out_path = apply_text_revision_core(
                str(source),
                revised_text,
                str(target) if target is not None else None,
                author,
                allow_major_deletions,
            )
        except TextRevisionVerificationError as e:
            # The engine wrote the '.unverified.docx' diagnostic copy and did NOT
            # write the requested file (text_revision.py:250-277). Relay its own
            # post-gate stats — the per-edit `status="failed"` reports, the
            # zeroed counters, `verification_error` — and overlay only what the
            # engine cannot know (input_path, author) or states differently:
            # `status` is the run's outcome here, not the batch's ("ok").
            engine_stats: dict[str, Any] = getattr(e, "stats", {}) or {}
            failure: dict[str, Any] = {
                **engine_stats,
                "input_path": str(source),
                "output_path": None,
                "success": False,
                "verified": False,
                "author": author,
                "status": "verification_failed",
                "error": str(e),
                "unverified_output_path": str(e.unverified_path),
            }
            return str(e), failure
        except ValueError as e:
            # Major-deletion guard, paginated-extract input, or a CriticMarkup
            # token our pre-check does not cover. Nothing was written and the
            # engine produced no stats; the agent can fix the text (or pass
            # allow_major_deletions=True) and retry.
            return str(e), {
                "input_path": str(source),
                "output_path": None,
                "success": False,
                "verified": False,
                "author": author,
                "edits_applied": 0,
                "edits_skipped": 0,
                "edits": [],
                "status": "error",
                "error": str(e),
            }

        applied = stats.get("edits_applied", 0)
        content = f"Text revision complete. Saved to: {out_path}\nEdits: {applied} applied."
        artifact: dict[str, Any] = {
            "input_path": str(source),
            "output_path": str(out_path),
            "success": True,
            "verified": True,
            "author": author,
            "edits_applied": applied,
            "edits_skipped": stats.get("edits_skipped", 0),
            "edits": stats.get("edits", []),
            "status": stats.get("status", "ok"),
        }
        return content, artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        revised_text: str,
        output_path: str | None = None,
        author: str | None = None,
        allow_major_deletions: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            file_path,
            revised_text,
            output_path,
            author,
            allow_major_deletions,
        )


__all__ = ["AdeuApplyTextRevision", "AdeuApplyTextRevisionInput"]
