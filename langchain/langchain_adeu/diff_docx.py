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
import json
from io import BytesIO
from typing import Any, Literal

from adeu.diff import (
    collect_media_difference_warnings,
    create_unified_diff,
    generate_edits_from_text,
    generate_structured_edits,
)
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream

# Intentional import from a non-public path: `_create_diff_output` is the
# canonical formatter for Adeu's word-patch diff and currently lives only
# in the MCP tool module. We accept the coupling rather than duplicating
# the formatter; in a future Adeu release this helper should be promoted
# to `adeu.diff` proper. Track in the adeu monorepo when that happens.
from adeu.mcp_components.tools.document import _create_diff_output
from adeu.utils.docx import strip_bom_from_docx_bytes
from docx import Document as load_document
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
    diff_format: Literal["word_patch", "unified", "structured_changes"] = Field(
        default="word_patch",
        description=(
            "Format of the output diff. 'word_patch' (default) returns Adeu's sub-word "
            "@@ Word Patch @@ format. 'unified' returns a standard Git-style unified diff. "
            "'structured_changes' returns a JSON object {'changes': [...], 'warnings': [...]} "
            "whose 'changes' array holds DocumentChange objects suitable for feeding "
            "directly into adeu_apply_changes."
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
        diff_format: Literal["word_patch", "unified", "structured_changes"] = "word_patch",
    ) -> str:
        orig = validate_docx_path(original_path, label="original document")
        mod = validate_docx_path(modified_path, label="modified document")

        if orig == mod:
            return _NO_DIFF_MESSAGE

        orig_bytes = strip_bom_from_docx_bytes(orig.read_bytes())
        mod_bytes = strip_bom_from_docx_bytes(mod.read_bytes())

        media_warnings = collect_media_difference_warnings(orig_bytes, mod_bytes)
        warning_prefix = "\n\n".join(f"⚠️  {w}" for w in media_warnings) + "\n\n" if media_warnings else ""

        if orig == mod:
            return warning_prefix + _NO_DIFF_MESSAGE if warning_prefix else _NO_DIFF_MESSAGE

        if diff_format == "structured_changes":
            doc_orig = load_document(BytesIO(orig_bytes))
            doc_mod = load_document(BytesIO(mod_bytes))
            text_orig, struct_orig = _extract_text_from_doc(
                doc_orig,
                clean_view=compare_clean,
                include_appendix=False,
                return_structure=True,
            )
            text_mod, struct_mod = _extract_text_from_doc(
                doc_mod,
                clean_view=compare_clean,
                include_appendix=False,
                return_structure=True,
            )
            # generate_structured_edits returns (edits, warnings), not a dict
            # (diff.py:961-966).
            edits, diff_warnings = generate_structured_edits(text_orig, struct_orig, text_mod, struct_mod)
            # Same serialization as `adeu diff --json` (cli.py:1183-1188): the
            # payload must validate as DocumentChange for adeu_apply_changes,
            # and the engine's own "Diff: …" labels are diagnostics — replaying
            # them would write junk comment bubbles into the document.
            edits_data: list[dict[str, Any]] = []
            for edit in edits:
                d = edit.model_dump(exclude_defaults=True)
                if isinstance(d.get("comment"), str) and d["comment"].startswith("Diff:"):
                    del d["comment"]
                edits_data.append({"type": edit.type, **d})
            output_obj = {
                "changes": edits_data,
                "warnings": media_warnings + diff_warnings,
            }
            # No warning_prefix here: this branch's whole output must stay
            # json.loads-able, and the media warnings already ride along in
            # output_obj["warnings"].
            return json.dumps(output_obj, indent=2)

        text_orig = extract_text_from_stream(
            BytesIO(orig_bytes),
            filename=orig.name,
            clean_view=compare_clean,
            include_appendix=False,
        )
        text_mod = extract_text_from_stream(
            BytesIO(mod_bytes),
            filename=mod.name,
            clean_view=compare_clean,
            include_appendix=False,
        )

        if diff_format == "unified":
            diff_str = create_unified_diff(text_orig, text_mod)
            if not diff_str:
                return warning_prefix + _NO_DIFF_MESSAGE if warning_prefix else _NO_DIFF_MESSAGE
            return warning_prefix + diff_str

        # atomic_criticmarkup=True ensures hunks never cut inside CriticMarkup delimiters (document.py:881-883)
        edits = generate_edits_from_text(text_orig, text_mod, atomic_criticmarkup=True)
        if not edits:
            return warning_prefix + _NO_DIFF_MESSAGE if warning_prefix else _NO_DIFF_MESSAGE

        diff_output = _create_diff_output(str(orig), str(mod), text_orig, edits)
        return warning_prefix + diff_output

    async def _arun(
        self,
        reasoning: str,
        original_path: str,
        modified_path: str,
        compare_clean: bool = True,
        diff_format: Literal["word_patch", "unified", "structured_changes"] = "word_patch",
    ) -> str:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            original_path,
            modified_path,
            compare_clean,
            diff_format,
        )


__all__ = ["AdeuDiffDocx", "AdeuDiffDocxInput"]
