# FILE: langchain/langchain_adeu/accept_all_changes.py
"""Accept all tracked changes in a .docx file, producing a finalized clean copy.

Wraps `adeu.RedlineEngine.accept_all_revisions`. Use this when a document
review is fully complete and every pending insertion, deletion, and
formatting change should be incorporated as final text. For selective
acceptance of specific changes, use `AdeuApplyChanges` with `accept`
actions targeting individual change IDs.

This tool is destructive in the sense that, in the output document, no
tracked-change history remains. The input document is left untouched.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from adeu import RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuAcceptAllChangesInput(BaseModel):
    """Input schema for `AdeuAcceptAllChanges`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description=(
            "Why am I accepting all tracked changes in this document? State this reason before any other parameter."
        ),
    )
    file_path: str = Field(
        description=("Absolute path to the .docx file containing tracked changes to accept."),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path for the cleaned output file. When omitted, "
            "defaults to '<stem>_clean.docx' in the same directory as the "
            "input. The output path must not equal the input path — "
            "overwriting the source is rejected to prevent data loss."
        ),
    )
    remove_comments: bool = Field(
        default=True,
        description=(
            "Also delete every comment. Defaults to True because this tool exists to "
            "produce a distributable clean document and comments are internal review "
            "notes that must not travel to a counterparty. Pass False to accept the "
            "tracked changes while keeping the comments (review still live). Comments "
            "anchored to text an accepted deletion consumes are removed either way, "
            "exactly as Word does."
        ),
    )


_DESCRIPTION = (
    "Accept ALL tracked changes in a Microsoft Word (.docx) file and produce "
    "a finalized clean copy. Every pending insertion is incorporated, every "
    "pending deletion is applied, every formatting change is committed, and "
    "all tracked-change history is removed from the output document.\n\n"
    "remove_comments (boolean, DEFAULT TRUE): also delete every comment. The default is TRUE "
    "because this tool's purpose is a distributable clean document, and comments are internal "
    "review notes that must not travel to a counterparty. Pass remove_comments=false to accept "
    "the tracked changes while KEEPING the comments — use that when the review conversation is "
    "still live. Either way the response reports how many comments were deleted and names each "
    "one with its author, and comments whose anchored text an accepted deletion consumes are "
    "removed regardless, exactly as Word does.\n\n"
    "The input file is never modified. The output file goes to the path you "
    "provide via `output_path`, or to `<stem>_clean.docx` in the same "
    "directory by default.\n\n"
    "Use this when a document review is fully complete. For selective "
    "acceptance or rejection of specific changes by ID, use "
    "`adeu_apply_changes` with `accept`/`reject` actions instead."
)


class AdeuAcceptAllChanges(BaseTool):
    """LangChain tool: accept all tracked changes, producing a clean copy."""

    name: str = "adeu_accept_all_changes"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuAcceptAllChangesInput  # type: ignore[assignment]
    response_format: Literal["content_and_artifact"] = "content_and_artifact"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        file_path: str,
        output_path: str | None = None,
        remove_comments: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        source = validate_docx_path(file_path, label="DOCX file")

        target = _resolve_output_path(source, output_path)

        raw_bytes = source.read_bytes()
        sanitized_bytes = strip_bom_from_docx_bytes(raw_bytes)
        stream = BytesIO(sanitized_bytes)

        engine = RedlineEngine(stream)
        counts = engine.accept_all_revisions(remove_comments=remove_comments)
        removed_comment_notes = list(engine.removed_comment_notes)

        result_stream = engine.save_to_stream()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(result_stream.getvalue())

        accepted_ins = counts.get("accepted_insertions", 0)
        accepted_del = counts.get("accepted_deletions", 0)
        accepted_fmt = counts.get("accepted_formatting", 0)
        removed = counts.get("removed_comments", 0)

        artifact: dict[str, Any] = {
            "input_path": str(source),
            "output_path": str(target),
            "remove_comments": remove_comments,
            "accepted_insertions": accepted_ins,
            "accepted_deletions": accepted_del,
            "accepted_formatting": accepted_fmt,
            "removed_comments": removed,
            "removed_comment_notes": removed_comment_notes,
        }

        if accepted_ins + accepted_del + accepted_fmt + removed == 0:
            content = f"No tracked changes or comments to accept — the document is already clean. Saved to: {target}"
            return content, artifact

        lines = [
            f"Accepted all changes. Saved to: {target}",
            f"Insertions accepted: {accepted_ins}",
            f"Deletions accepted: {accepted_del}",
            f"Formatting changes accepted: {accepted_fmt}",
            f"Comments removed: {removed}",
        ]
        if removed_comment_notes:
            lines.append("Comments deleted: " + ", ".join(removed_comment_notes))
            if not remove_comments:
                lines.append(
                    "Note: these comments were anchored to text an accepted deletion "
                    "consumed, so Word removes them too. Nothing else was deleted."
                )
        elif not remove_comments:
            lines.append("Comments kept (remove_comments=False).")
        return "\n".join(lines), artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        output_path: str | None = None,
        remove_comments: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self._run, reasoning, file_path, output_path, remove_comments)


def _resolve_output_path(source: Path, requested: str | None) -> Path:
    """Decide where the cleaned file should be written.

    When `requested` is None, default to `<stem>_clean.docx` next to the
    source. When `requested` resolves to the same physical path as the
    source, raise — silently overwriting the input on an LLM-driven
    workflow is almost always a mistake.
    """
    if requested is None:
        return source.with_name(f"{source.stem}_clean{source.suffix}")

    target = validate_docx_path(requested, must_exist=False, label="output path")

    if target == source:
        raise ToolException(
            f"Output path must differ from input path; refusing to overwrite "
            f"the source file at {source}. Pick a different output_path or "
            "omit output_path to use the default '<stem>_clean.docx'."
        )
    return target


__all__ = ["AdeuAcceptAllChanges", "AdeuAcceptAllChangesInput"]
