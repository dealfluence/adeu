# FILE: langchain/langchain_adeu/diff_docx.py
"""Generate a word-level diff between two .docx files.

Wraps `adeu.diff.generate_edits_from_text` to produce a custom
`@@ Word Patch @@` diff format. The custom format is deliberately
sub-word level (not standard Unified Diff) because LangChain agents
reason much better about explicit "this phrase changed to that phrase"
hunks than about line-level patches that lump unrelated edits together.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream

# Intentional import from a non-public path: `_create_diff_output` is the
# canonical formatter for Adeu's word-patch diff and currently lives only
# in the MCP tool module. We accept the coupling rather than duplicating
# the formatter; in a future Adeu release this helper should be promoted
# to `adeu.diff` proper. Track in the adeu monorepo when that happens.
from adeu.mcp_components.tools.document import _create_diff_output
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuDiffDocxInput(BaseModel):
    """Input schema for `AdeuDiffDocx`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="Why am I comparing these two documents? State this reason before any other parameter.",
    )
    original_path: str = Field(
        description=("Absolute path to the baseline .docx file (the 'before' document)."),
    )
    modified_path: str = Field(
        description=("Absolute path to the new .docx file (the 'after' document)."),
    )
    compare_clean: bool = Field(
        default=True,
        description=(
            "When True (default), compares the 'Accepted' finalized state of "
            "both documents — what the text would be if every tracked change "
            "were accepted. This is what reviewers usually want. "
            "Set False to compare raw text including CriticMarkup tags for "
            "tracked changes (useful only for debugging Adeu itself)."
        ),
    )


_DESCRIPTION = (
    "Compare two Microsoft Word (.docx) files and return a word-level diff "
    "in `@@ Word Patch @@` format. Each hunk shows surrounding context, then "
    "the removed phrase (prefixed `-`) and the added phrase (prefixed `+`).\n\n"
    "Use this to see exactly what changed between two versions of a document. "
    "Compares the 'Accepted' state by default (i.e. what the text would be "
    "if every tracked change were accepted), which is what reviewers usually "
    "want. Set compare_clean=False to compare the raw underlying text including "
    "any CriticMarkup for tracked changes."
)

_NO_DIFF_MESSAGE = "No text differences found between the documents."


class AdeuDiffDocx(BaseTool):
    """LangChain tool: word-level diff between two .docx files.

    Use this tool to surface what changed between two versions of a
    document so the agent can summarize, validate, or reason about the
    delta before taking further action.
    """

    name: str = "adeu_diff_docx"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuDiffDocxInput  # type: ignore[assignment]
    response_format: Literal["content"] = "content"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        original_path: str,
        modified_path: str,
        compare_clean: bool = True,
    ) -> str:
        orig = validate_docx_path(original_path, label="original document")
        mod = validate_docx_path(modified_path, label="modified document")

        if orig == mod:
            return _NO_DIFF_MESSAGE

        text_orig = _read_text(orig, compare_clean)
        text_mod = _read_text(mod, compare_clean)

        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            return _NO_DIFF_MESSAGE

        return _create_diff_output(str(orig), str(mod), text_orig, edits)

    async def _arun(
        self,
        reasoning: str,
        original_path: str,
        modified_path: str,
        compare_clean: bool = True,
    ) -> str:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            original_path,
            modified_path,
            compare_clean,
        )


def _read_text(path: Path, clean_view: bool) -> str:
    """Extract text from a .docx for diffing.

    Reads the file into memory once (the engine needs a stream, not a path).
    `extract_text_from_stream` is the same entry point `diff_docx_files`
    uses in the MCP server, so behavior is identical across surfaces.
    """
    from io import BytesIO

    with open(path, "rb") as f:
        stream = BytesIO(f.read())
    return extract_text_from_stream(stream, filename=path.name, clean_view=clean_view)


__all__ = ["AdeuDiffDocx", "AdeuDiffDocxInput"]
