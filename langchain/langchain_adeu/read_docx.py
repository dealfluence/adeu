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

from adeu.fields import banner_for_path
from adeu.ingest import _extract_text_from_doc
from adeu.mcp_components._response_builders import (
    build_appendix_response,
    build_bounded_full_response,
    build_changes_response,
    build_fields_response,
    build_full_document_response,
    build_outline_response,
    build_page_range_response,
    build_paginated_response,
    build_search_response,
)
from adeu.outline import extract_outline
from adeu.pagination import paginate, parse_page_arg, split_structural_appendix
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
    mode: Literal["full", "outline", "appendix", "changes", "fields"] = Field(
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
            "find ids. "
            "'fields' returns a form-fields ledger listing every content control "
            "with its CC:<N> id, tag, alias, type, state, and option list — "
            "use this before set_field."
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
        if isinstance(v, str):
            parse_page_arg(v.strip().strip("'\""))
        else:
            parse_page_arg(v)
        return v

    force: bool = Field(
        default=False,
        description=(
            "Only for page='all': return the whole document even when it exceeds the "
            "response budget. Without this, an oversized page='all' read serves the "
            "first pages that fit plus an L1 outline and continuation hint."
        ),
    )

    no_chrome: bool = Field(
        default=False,
        description=(
            "When True, strips navigation banners, page headers, and footers for "
            "raw token-minimal output. Defaults to False."
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
    fields_offset: int = Field(
        default=0,
        description="For mode='fields' only: 0-based entry offset for paging through a long fields ledger.",
    )


_DESCRIPTION = (
    "Read a Microsoft Word (.docx) file. Returns the document text with inline "
    "CriticMarkup for any tracked changes and comments: {++inserted++}, "
    "{--deleted--}, {==highlighted==}{>>comment<<}. "
    "\n\n"
    "Set clean_view=True to see the finalized 'Accepted' text without markup. "
    "\n\n"
    "Modes:\n"
    "- 'full' (default): paginated body content. Use page=N, a range like page='2-6' "
    "(up to 8 pages; also filters search_query), or page='all'.\n"
    "- 'outline': heading map only — start here for large docs to plan "
    "targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 "
    "to see deeper structure.\n"
    "- 'appendix': defined terms, anchors, and cross-reference targets. "
    "Consult before editing legal/technical docs to avoid breaking references.\n"
    "- 'changes': a concise ledger of every tracked change and comment with its "
    "id, author, page, and text. Use this before any accept/reject/reply, and "
    "instead of reading full pages just to find ids.\n"
    "- 'fields': a ledger of every content control (form field) with its CC:<N> "
    "id, tag/alias, type, lock/binding state, and current value (paginate via "
    "fields_offset)."
    "\n\n"
    "page='all' returns the whole document; when it exceeds the response budget, "
    "it serves the first pages that fit plus an L1 outline and continuation hint "
    "— pass force=True to return the full document in one call."
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
        mode: Literal["full", "outline", "appendix", "changes", "fields"] = "full",
        page: int | str | None = None,
        force: bool = False,
        no_chrome: bool = False,
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
        fields_offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        path = validate_docx_path(file_path, label="DOCX file")

        if mode == "changes":
            if clean_view:
                raise ToolException(
                    "clean_view=True cannot be used with mode='changes': the ledger is built "
                    "from the raw tracked-change projection. Drop clean_view or pick another mode."
                )

        if isinstance(page, str):
            page = page.strip().strip("'\"")

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

        text: str
        paragraph_offsets: dict[int, tuple[int, int, Any]] | None
        if needs_offsets:
            res_tuple = _extract_text_from_doc(
                doc,
                clean_view=clean_view,
                include_appendix=needs_appendix,
                return_paragraph_offsets=True,
            )
            assert isinstance(res_tuple, tuple)
            text = res_tuple[0]
            paragraph_offsets = res_tuple[1]  # type: ignore[assignment]
        else:
            res_str = _extract_text_from_doc(
                doc,
                clean_view=clean_view,
                include_appendix=needs_appendix,
                return_paragraph_offsets=False,
            )
            assert isinstance(res_str, str)
            text = res_str
            paragraph_offsets = None

        if search_query is not None:
            search_page: int | str | tuple[int, int] | None = page
            if isinstance(page, str) and "-" in page:
                try:
                    p_kind, p_val = parse_page_arg(page)
                    if p_kind == "range":
                        search_page = p_val
                except ValueError:
                    pass
            result = build_search_response(
                text,
                search_query,
                search_regex,
                search_case_sensitive,
                search_page,
                str(path),
                max_matches=max_matches,
                match_offset=match_offset,
                full_paragraph=full_paragraph,
                no_chrome=no_chrome,
            )
        elif mode == "fields":
            result = build_fields_response(doc, text, str(path), offset=fields_offset, no_chrome=no_chrome)
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
                no_chrome=no_chrome,
            )
        elif mode == "outline":
            result = build_outline_response(
                doc,
                text,
                str(path),
                outline_max_level=outline_max_level,
                outline_verbose=outline_verbose,
                paragraph_offsets=paragraph_offsets,
                no_chrome=no_chrome,
            )
        else:
            kind, page_val = parse_page_arg(page)
            fields_banner = (
                None
                if no_chrome
                else banner_for_path(
                    str(path),
                    hint=' \u00b7 adeu_read_docx mode="fields" for the field ledger',
                    loader=lambda _p: doc,
                )
            )
            if mode == "appendix":
                if kind == "range":
                    raise ToolException("Page range pagination is only supported in 'full' mode, not 'appendix' mode.")
                if kind == "all":
                    raise ToolException(f"Invalid page parameter: '{page}'. Provide a positive integer.")
                assert isinstance(page_val, int)
                result = build_appendix_response(text, page_val, str(path), no_chrome=no_chrome)
            elif kind == "all":
                if not force and len(text) > response_budget_limit():
                    body_text, _ = split_structural_appendix(text)
                    pagination = paginate(body_text, "")
                    outline_nodes = extract_outline(
                        doc,
                        body_text,
                        pagination.body_pages,
                        pagination.body_page_offsets,
                    )
                    result = build_bounded_full_response(
                        text,
                        str(path),
                        pagination,
                        outline_nodes=outline_nodes,
                    )
                else:
                    result = build_full_document_response(
                        text,
                        str(path),
                        no_chrome=no_chrome,
                        fields_banner=fields_banner,
                    )
            elif kind == "range":
                assert isinstance(page_val, tuple)
                start_page, end_page = page_val
                result = build_page_range_response(text, start_page, end_page, str(path), no_chrome=no_chrome)
            else:
                assert isinstance(page_val, int)
                result = build_paginated_response(
                    text,
                    page_val,
                    str(path),
                    no_chrome=no_chrome,
                    fields_banner=fields_banner,
                )

        artifact = dict(result.structured_content) if result.structured_content else {}
        if isinstance(result.content, str):
            content = result.content
        else:
            blocks = result.content if isinstance(result.content, list) else [result.content]
            content = "".join(getattr(b, "text", str(b)) for b in blocks if b is not None)

        return content, artifact

    async def _arun(
        self,
        reasoning: str,
        file_path: str,
        clean_view: bool = False,
        mode: Literal["full", "outline", "appendix", "changes", "fields"] = "full",
        page: int | str | None = None,
        force: bool = False,
        no_chrome: bool = False,
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
        fields_offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(
            self._run,
            reasoning,
            file_path,
            clean_view,
            mode,
            page,
            force,
            no_chrome,
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
            fields_offset,
        )


__all__ = ["AdeuReadDocx", "AdeuReadDocxInput"]
