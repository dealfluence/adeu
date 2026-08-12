#!/usr/bin/env python
"""Captures the conformance goldens from the PYTHON engine (spec §8.3).

Run from `python/` so the `adeu` package resolves:

    cd python && uv run python ../shared/conformance/capture_goldens.py

Every case calls the Python response builders with ``is_cli=False`` — the MCP
flavour the Node builders must match — and writes ``BuilderResult.content``
(the LLM-facing text, not the UI markdown) to
``shared/conformance/goldens/<case>.txt``.

Two invariants keep the goldens from churning:

* ``file_path`` is ALWAYS the placeholder ``/fixtures/<name>.docx``, never a
  real path. A real path would bake this machine's drive letter into every
  golden and make the Node comparison unwinnable.
* Files are written with ``newline="\\n"``, and the Node loader normalises
  CRLF before comparing.

The projection mirrors ``adeu.mcp_components.doc_cache._fill_view`` exactly, so
the goldens are what the MCP server really serves for these documents.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document

from adeu.ingest import _extract_text_from_doc
from adeu.mcp_components._response_builders import (
    build_budget_guard_message,
    build_changes_response,
    build_outline_response,
    build_page_range_response,
    build_search_response,
)
from adeu.mcp_components.shared import MCP_ID_DISCOVERY_HINT
from adeu.outline import extract_outline
from adeu.pagination import paginate, split_structural_appendix
from adeu.redline.comments import CommentsManager
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures"
GOLDEN_DIR = HERE / "goldens"

# The search term seeded into long_5pages (see build_fixtures.mjs SENTENCES).
SEARCH_QUERY = "Confidential Information"


class Fixture:
    """One projected fixture: everything the builders need, computed once."""

    def __init__(self, name: str) -> None:
        self.name = name
        # The placeholder every golden embeds, and the string the Node tests
        # pass in turn. NEVER the real path.
        self.file_path = f"/fixtures/{name}.docx"

        data = (FIXTURE_DIR / f"{name}.docx").read_bytes()
        self.doc = Document(BytesIO(strip_bom_from_docx_bytes(data)))
        self.text, self.paragraph_offsets = _extract_text_from_doc(
            self.doc,
            clean_view=False,
            include_appendix=False,
            return_paragraph_offsets=True,
        )
        body, _appendix = split_structural_appendix(self.text)
        self.pagination = paginate(body, structural_appendix="")
        self.outline_nodes = extract_outline(
            self.doc,
            body,
            self.pagination.body_pages,
            self.pagination.body_page_offsets,
            paragraph_offsets=self.paragraph_offsets,
        )
        self.comments_data = CommentsManager(self.doc).extract_comments_data()
        engine = RedlineEngine(BytesIO(data), id_discovery_hint=MCP_ID_DISCOVERY_HINT)
        self.change_ids = set(engine._existing_change_ids())


def write_golden(case: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{case}.txt"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"{case:<24} {len(content):>7} chars")


def ledger(fx: Fixture, **kwargs) -> str:
    return build_changes_response(
        fx.text,
        fx.file_path,
        comments_data=fx.comments_data,
        pagination_result=fx.pagination,
        existing_change_ids=fx.change_ids,
        is_cli=False,
        **kwargs,
    ).content


def page_range(fx: Fixture, start: int, end: int) -> str:
    return build_page_range_response(
        fx.text,
        start,
        end,
        fx.file_path,
        is_cli=False,
        pagination_result=fx.pagination,
    ).content


def search(fx: Fixture, **kwargs) -> str:
    return build_search_response(
        fx.text,
        SEARCH_QUERY,
        False,  # search_regex
        True,  # search_case_sensitive
        None,  # page — search the whole document
        fx.file_path,
        is_cli=False,
        pagination_result=fx.pagination,
        **kwargs,
    ).content


def main() -> None:
    multi_author = Fixture("multi_author")
    comments_threads = Fixture("comments_threads")
    tables_cells = Fixture("tables_cells")
    long5 = Fixture("long_5pages")
    dense = Fixture("dense_175")

    # --- ledger (mode='changes') -----------------------------------------
    write_golden("ledger_multi_author", ledger(multi_author))
    write_golden("ledger_comments_threads", ledger(comments_threads))
    write_golden("ledger_tables", ledger(tables_cells))
    write_golden("ledger_author_filter", ledger(multi_author, author_filter="Bob Smith"))
    write_golden("ledger_page_filter", ledger(dense, page=2))
    # dense_175 carries 350 entries, so offset 300 is the ledger's second
    # page (page size 300) and offset 0 carries the continuation pointer.
    write_golden("ledger_dense_offset0", ledger(dense, offset=0))
    write_golden("ledger_dense_offset300", ledger(dense, offset=300))

    # --- native page ranges ----------------------------------------------
    write_golden("range_2_4", page_range(long5, 2, 4))
    # dense_175 is nine pages, so 1-12 trips the eight-page cap note. On a
    # five-page document it would only ever trip the early-stop note.
    write_golden("range_cap_1_12", page_range(dense, 1, 12))
    write_golden("range_past_end", page_range(long5, 4, 9))

    # --- whole-document budget guard (A3) --------------------------------
    write_golden(
        "guard_long5",
        build_budget_guard_message(
            long5.text,
            long5.file_path,
            doc=long5.doc,
            outline_nodes=long5.outline_nodes,
            pagination_result=long5.pagination,
            paragraph_offsets=long5.paragraph_offsets,
            is_cli=False,
        ),
    )

    # --- search ----------------------------------------------------------
    write_golden("search_default", search(long5))
    write_golden("search_max2_offset2", search(long5, max_matches=2, match_offset=2))
    write_golden("search_full_paragraph", search(long5, max_matches=3, full_paragraph=True))

    # --- outline ---------------------------------------------------------
    write_golden(
        "outline_l1",
        build_outline_response(
            long5.doc,
            long5.text,
            long5.file_path,
            outline_max_level=1,
            is_cli=False,
            pagination_result=long5.pagination,
            outline_nodes=long5.outline_nodes,
        ).content,
    )

    print(f"\nWrote goldens to {GOLDEN_DIR}")


if __name__ == "__main__":
    main()
