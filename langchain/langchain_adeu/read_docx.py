# FILE: langchain/langchain_adeu/read_docx.py
"""Read a DOCX file into LLM-friendly Markdown.

Wraps `adeu.RedlineEngine`'s read path (via `adeu.ingest._extract_text_from_doc`
and the `mcp_components._response_builders`). The tool returns a two-tuple
`(content, artifact)`:

  - `content`: paginated/projected Markdown the model reads directly.
  - `artifact`: dict with `markdown`, `title`, `file_path`, plus the page /
    total_pages metadata so downstream LangGraph nodes can paginate or
    reason about document structure without re-parsing the content.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, Literal

from adeu.ingest import _extract_text_from_doc
from adeu.mcp_components._response_builders import (
    build_appendix_response,
    build_full_document_response,
    build_outline_response,
    build_paginated_response,
    build_search_response,
)
from adeu.utils.docx import strip_bom_from_docx_bytes
from docx import Document as load_document
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from langchain_adeu._shared import validate_docx_path, wrap_tool_errors


class AdeuReadDocxInput(BaseModel):
    """Input schema for `AdeuReadDocx`."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="Why do I need to read this docx document? State this reason before any other parameter.",
    )
    file_path: str = Field(
        description=(
            "Absolute filesystem path to the .docx file to read. "
            "Paths are resolved against the current working directory; "
            "use absolute paths to avoid ambiguity."
        ),
    )
    clean_view: bool = Field(
        default=False,
        description=(
            "When False (default), returns the raw text with inline CriticMarkup "
            "for tracked changes and comments: {++inserted++}, {--deleted--}, "
            "{==highlighted==}{>>comment<<}. When True, returns the finalized "
            "'Accepted' text without any markup."
        ),
    )
    mode: Literal["full", "outline", "appendix"] = Field(
        default="full",
        description=(
            "Read mode. 'full' (default) returns paginated body content. "
            "'outline' returns a structural heading map of the document — "
            "start here for large documents to plan targeted reads. "
            "'appendix' returns defined terms, named anchors, cross-references, "
            "and semantic diagnostics (e.g. likely typos, unresolved references) "
            "— consult before editing legal or technical documents to avoid "
            "breaking references."
        ),
    )
    page: int | str = Field(
        default=1,
        description=(
            "1-indexed page number for mode='full' or mode='appendix', or 'all' "
            "to return the full document without page boundaries. Defaults to 1. "
            "Ignored for mode='outline'. Pages are virtual: bounded by content "
            "size (~19k chars each), not by visual Word page breaks."
        ),
    )

    @field_validator("page")
    @classmethod
    def _validate_page(cls, v: int | str) -> int | str:
        if isinstance(v, int):
            if v < 1:
                raise ValueError("page must be an integer >= 1, or 'all'")
            return v
        s = str(v).strip().lower()
        if s == "all":
            return "all"
        if s.isdigit() or (s.startswith(("-", "+")) and s[1:].isdigit()):
            val = int(s)
            if val < 1:
                raise ValueError("page must be an integer >= 1, or 'all'")
            return val
        raise ValueError("page must be an integer >= 1, or 'all'")

    outline_max_level: int = Field(
        default=2,
        ge=1,
        le=6,
        description=(
            "For mode='outline' only: only show headings at this level or "
            "shallower (1-6). Default 2 keeps output usable on large documents. "
            "Raise to 3-6 to see deeper headings. Ignored for other modes."
        ),
    )
    outline_verbose: bool = Field(
        default=False,
        description=(
            "For mode='outline' only: when True, includes per-heading style "
            "name, table presence, and footnote IDs. Off by default to "
            "minimize payload size."
        ),
    )
    search_query: str | None = Field(
        default=None,
        description=(
            "The substring or regex pattern to search for. When provided, filters results to matching paragraphs."
        ),
    )
    search_regex: bool = Field(
        default=False,
        description="Set to True to interpret search_query as a regular expression.",
    )
    search_case_sensitive: bool = Field(
        default=True,
        description="Set to False to perform case-insensitive matching.",
    )


_DESCRIPTION = (
    "Read a Microsoft Word (.docx) file. Returns the document text with inline "
    "CriticMarkup for any tracked changes and comments: {++inserted++}, "
    "{--deleted--}, {==highlighted==}{>>comment<<}. "
    "\n\n"
    "Set clean_view=True to see the finalized 'Accepted' text without markup. "
    "\n\n"
    "Modes:\n"
    "- 'full' (default): paginated body content. Use page=N to navigate.\n"
    "- 'outline': heading map only — start here for large docs to plan "
    "targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 "
    "to see deeper structure.\n"
    "- 'appendix': defined terms, anchors, and cross-reference targets. "
    "Consult before editing legal/technical docs to avoid breaking references."
)


class AdeuReadDocx(BaseTool):
    """LangChain tool: read a .docx file into projected Markdown.

    Use this tool to inspect the contents of a Word document before
    proposing edits. Reading with clean_view=False (the default) lets
    the model see existing tracked changes and comments inline, which
    is essential for review-and-respond workflows.
    """

    name: str = "adeu_read_docx"
    description: str = _DESCRIPTION
    args_schema: type[BaseModel] = AdeuReadDocxInput  # type: ignore[assignment]
    response_format: Literal["content_and_artifact"] = "content_and_artifact"

    @wrap_tool_errors
    def _run(
        self,
        reasoning: str,
        file_path: str,
        clean_view: bool = False,
        mode: Literal["full", "outline", "appendix"] = "full",
        page: int | str = 1,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
        search_query: str | None = None,
        search_regex: bool = False,
        search_case_sensitive: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        path = validate_docx_path(file_path, label="DOCX file")

        raw_bytes = path.read_bytes()
        sanitized_bytes = strip_bom_from_docx_bytes(raw_bytes)
        doc = load_document(BytesIO(sanitized_bytes))

        needs_appendix = mode == "appendix"
        needs_offsets = mode == "outline"

        extract_result = _extract_text_from_doc(
            doc,
            clean_view=clean_view,
            include_appendix=needs_appendix,
            return_paragraph_offsets=needs_offsets,
        )
        if needs_offsets:
            text, paragraph_offsets = extract_result
        else:
            text = extract_result
            paragraph_offsets = None

        page_str = str(page).strip().lower() if page is not None else "1"

        if search_query is not None:
            result = build_search_response(text, search_query, search_regex, search_case_sensitive, page, str(path))
        elif mode == "full" and page_str == "all":
            result = build_full_document_response(text, str(path))
        else:
            page_num = 1
            if page is not None:
                s_page = str(page).strip()
                is_signed = s_page.startswith(("-", "+")) and s_page[1:].isdigit()
                if s_page.isdigit() or is_signed:
                    page_num = int(s_page)

            if mode == "outline":
                result = build_outline_response(
                    doc,
                    text,
                    str(path),
                    outline_max_level=outline_max_level,
                    outline_verbose=outline_verbose,
                    paragraph_offsets=paragraph_offsets,
                )
            elif mode == "appendix":
                result = build_appendix_response(text, page_num, str(path))
            else:
                result = build_paginated_response(text, page_num, str(path))

        artifact = dict(result.structured_content) if result.structured_content else {}
        ui_markdown = artifact.get("markdown")

        if ui_markdown is None:
            blocks = result.content if isinstance(result.content, list) else [result.content]
            ui_markdown = "".join(getattr(b, "text", str(b)) for b in blocks if b is not None)

        content = f"> **File Path:** `{path}`\n\n{ui_markdown}"

        return content, artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        clean_view: bool = False,
        mode: Literal["full", "outline", "appendix"] = "full",
        page: int | str = 1,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
        search_query: str | None = None,
        search_regex: bool = False,
        search_case_sensitive: bool = True,
    ) -> tuple[str, dict[str, Any]]:

        return await asyncio.to_thread(
            self._run,
            reasoning,
            file_path,
            clean_view,
            mode,
            page,
            outline_max_level,
            outline_verbose,
            search_query,
            search_regex,
            search_case_sensitive,
        )


__all__ = ["AdeuReadDocx", "AdeuReadDocxInput"]
