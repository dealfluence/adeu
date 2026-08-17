# FILE: langchain/langchain_adeu/read_docx.py
"""Read a DOCX file into LLM-friendly Markdown.

Wraps `adeu.RedlineEngine`'s read path (via `adeu.ingest._extract_text_from_doc`
and the `mcp_components._response_builders`). The tool returns a two-tuple
`(content, artifact)`:

  - `content`: paginated/projected Markdown the model reads directly.
  - `artifact`: dict with `markdown`, `title`, `file_path`, plus the page /
    total_pages metadata so downstream LangGraph nodes can paginate or
    reason about document structure without re-parsing the content.

`page` accepts a single page number, a page range such as `"2-6"` (capped at
8 pages by the engine), or `"all"`; the grammar is parsed by
`adeu.pagination.parse_page_arg` so it cannot drift from the engine.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, Literal

from adeu.ingest import _extract_text_from_doc
from adeu.mcp_components._response_builders import (
    build_appendix_response,
    build_budget_guard_message,
    build_changes_response,
    build_full_document_response,
    build_outline_response,
    build_page_range_response,
    build_paginated_response,
    build_search_response,
)
from adeu.pagination import parse_page_arg
from adeu.payloads import response_budget_limit
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes
from docx import Document as load_document
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from langchain_adeu._shared import (
    LANGCHAIN_ID_DISCOVERY_HINT,
    validate_docx_path,
    wrap_tool_errors,
)


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
    mode: Literal["full", "outline", "appendix", "changes"] = Field(
        default="full",
        description=(
            "Read mode. 'full' (default) returns paginated body content. "
            "'outline' returns a structural heading map of the document — "
            "start here for large documents to plan targeted reads. "
            "'appendix' returns defined terms, named anchors, cross-references, "
            "and semantic diagnostics (e.g. likely typos, unresolved references) "
            "— consult before editing legal or technical documents to avoid "
            "breaking references. "
            "'changes' returns a concise ledger of every tracked change and "
            "comment with its id, author, page, and text — use this before any "
            "accept/reject/reply, and instead of reading full pages just to "
            "find ids."
        ),
    )
    page: int | str | None = Field(
        default=None,
        description=(
            "1-indexed page number, a page range such as '2-6', or 'all'. "
            "For mode='full' and mode='appendix' an omitted page means page 1; "
            "'all' (mode='full' only) returns the whole document in one response "
            "without page banners. With search_query, page restricts matches to that "
            "document page and an omitted page searches every page. Pages are "
            "virtual: bounded by content size (~19k chars), not by Word page breaks."
        ),
    )

    @field_validator("page")
    @classmethod
    def _validate_page(cls, v: int | str | None) -> int | str | None:
        # Delegate to the engine's parser so the accepted grammar (int, 'N-M',
        # 'all') can never drift from adeu.pagination.parse_page_arg
        # (python/src/adeu/pagination.py:26). The value is passed through
        # unchanged; _run re-parses it for dispatch, exactly like document.py.
        parse_page_arg(v)
        return v

    force: bool = Field(
        default=False,
        description=(
            "Only for page='all': return the whole document even when it exceeds the "
            "response budget. Without this, an oversized unbounded read is refused with "
            "a list of bounded alternatives (page ranges, search, outline)."
        ),
    )

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
    max_matches: int = Field(
        default=20,
        description=(
            "For search queries only: maximum number of match snippets to return "
            "(default 20). 0 returns the counts header with no snippets."
        ),
    )
    match_offset: int = Field(
        default=0,
        description=(
            "For search queries only: 0-based match index to start from, for paging "
            "through a large result set (default 0)."
        ),
    )
    full_paragraph: bool = Field(
        default=False,
        description=(
            "For search queries only: return the full paragraph around each match "
            "instead of clamping the snippet to +/-120 characters."
        ),
    )
    changes_author: str | None = Field(
        default=None,
        description="For mode='changes' only: only list changes and comments by this author.",
    )
    changes_offset: int = Field(
        default=0,
        description="For mode='changes' only: 0-based entry offset for paging through a long ledger.",
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
    "Consult before editing legal/technical docs to avoid breaking references.\n"
    "- 'changes': a concise ledger of every tracked change and comment with its "
    "id, author, page, and text. Use this before any accept/reject/reply, and "
    "instead of reading full pages just to find ids."
    "\n\n"
    "page='all' returns the whole document, and is refused with a recipe of bounded "
    "alternatives when the document exceeds the response budget — pass force=True to override."
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
        mode: Literal["full", "outline", "appendix", "changes"] = "full",
        page: int | str | None = None,
        force: bool = False,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
        search_query: str | None = None,
        search_regex: bool = False,
        search_case_sensitive: bool = True,
        max_matches: int = 20,
        match_offset: int = 0,
        full_paragraph: bool = False,
        changes_author: str | None = None,
        changes_offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        path = validate_docx_path(file_path, label="DOCX file")

        if mode == "changes":
            if clean_view:
                raise ToolException(
                    "clean_view=True cannot be used with mode='changes': the ledger is built "
                    "from the raw tracked-change projection. Drop clean_view or pick another mode."
                )

        # `page` means "document page" and defaults to 1 everywhere EXCEPT search
        # and mode='changes', where None means "every page" — the response builder
        # needs to tell "omitted" from an explicit 1 (document.py:1384-1389).
        if search_query is None and mode != "changes" and page is None:
            page = 1

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

        if search_query is not None:
            result = build_search_response(
                text,
                search_query,
                search_regex,
                search_case_sensitive,
                page,
                str(path),
                max_matches=max_matches,
                match_offset=match_offset,
                full_paragraph=full_paragraph,
            )
        elif mode == "changes":
            # One engine load serves both enrichments the ledger wants:
            # comments_data (author + reply threading) and the authoritative set
            # of change ids still present in the XML, which filters ids that only
            # survive in the projection (document.py:436-465). `_existing_change_ids`
            # is private, but it is the same call the MCP layer makes (:447).
            comments_data: dict[str, Any] | None
            existing_change_ids: set[str] | None
            try:
                engine = RedlineEngine(BytesIO(sanitized_bytes), id_discovery_hint=LANGCHAIN_ID_DISCOVERY_HINT)
                comments_data = engine.comments_manager.extract_comments_data()
                existing_change_ids = set(engine._existing_change_ids())
            except Exception:
                # The ledger degrades gracefully without them (both parameters
                # are Optional); a broken comments part must not fail the read.
                comments_data = None
                existing_change_ids = None
            result = build_changes_response(
                text,
                str(path),
                comments_data=comments_data,
                author_filter=changes_author,
                page=page,
                offset=changes_offset,
                existing_change_ids=existing_change_ids,
            )
        elif mode == "outline":
            result = build_outline_response(
                doc,
                text,
                str(path),
                outline_max_level=outline_max_level,
                outline_verbose=outline_verbose,
                paragraph_offsets=paragraph_offsets,
            )
        else:
            kind, page_val = parse_page_arg(page)
            if mode == "appendix":
                if kind == "range":
                    raise ToolException("Page range pagination is only supported in 'full' mode, not 'appendix' mode.")
                if kind == "all":
                    raise ToolException(f"Invalid page parameter: '{page}'. Provide a positive integer.")
                assert isinstance(page_val, int)
                result = build_appendix_response(text, page_val, str(path))
            elif kind == "all":
                if not force and len(text) > response_budget_limit():
                    raise ToolException(build_budget_guard_message(text, str(path), doc=doc))
                result = build_full_document_response(text, str(path))
            elif kind == "range":
                assert isinstance(page_val, tuple)
                start_p, end_p = page_val
                result = build_page_range_response(text, start_p, end_p, str(path))
            else:
                assert isinstance(page_val, int)
                result = build_paginated_response(text, page_val, str(path))

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
        mode: Literal["full", "outline", "appendix", "changes"] = "full",
        page: int | str | None = None,
        force: bool = False,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
        search_query: str | None = None,
        search_regex: bool = False,
        search_case_sensitive: bool = True,
        max_matches: int = 20,
        match_offset: int = 0,
        full_paragraph: bool = False,
        changes_author: str | None = None,
        changes_offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            file_path,
            clean_view,
            mode,
            page,
            force,
            outline_max_level,
            outline_verbose,
            search_query,
            search_regex,
            search_case_sensitive,
            max_matches,
            match_offset,
            full_paragraph,
            changes_author,
            changes_offset,
        )


__all__ = ["AdeuReadDocx", "AdeuReadDocxInput"]
