# FILE: tests/test_stringified_json_search_page.py
"""
Regression tests for two behaviors:
  1. Stringified JSON elements in the `changes` array (Gemini quirk).
  2. The `page` parameter in search mode acts as a DOCUMENT-page filter
     (not a result paginator).
"""

import asyncio
import json

import pytest
from docx import Document

from adeu.mcp_components._response_builders import BuilderError, build_search_response
from adeu.mcp_components.tools.document import process_document_batch


class MockContext:
    """Mock FastMCP Context to absorb async logging calls during tests."""

    async def info(self, msg, **kwargs):
        pass

    async def debug(self, msg, **kwargs):
        pass

    async def warning(self, msg, **kwargs):
        pass

    async def error(self, msg, **kwargs):
        pass


@pytest.fixture
def sample_docx(tmp_path) -> str:
    """Creates a basic DOCX file for testing."""
    doc = Document()
    doc.add_paragraph("This is the original text.")
    path = tmp_path / "sample.docx"
    doc.save(path)
    return str(path)


# ---------------------------------------------------------------------------
# Bug 1: stringified JSON elements in `changes` (unchanged)
# ---------------------------------------------------------------------------


def test_process_document_batch_accepts_stringified_changes(sample_docx, tmp_path):
    """
    Gemini (and occasionally other LLM clients) wraps each object in the
    `changes` array as a JSON-encoded string. Pydantic should accept this
    via the BeforeValidator and the batch should apply normally.
    """
    ctx = MockContext()
    output_path = tmp_path / "output.docx"

    stringified_changes = [
        json.dumps(
            {
                "type": "modify",
                "target_text": "original text",
                "new_text": "new text",
                "comment": "Test comment",
            }
        )
    ]

    result = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=sample_docx,
            author_name="AI Agent",
            ctx=ctx,  # type: ignore[arg-type]
            changes=stringified_changes,  # type: ignore[arg-type]
            output_path=str(output_path),
        )
    )

    assert "Batch complete" in result
    assert "Edits: 1 applied, 0 skipped" in result
    assert output_path.exists()


def test_process_document_batch_accepts_mixed_string_and_dict(sample_docx, tmp_path):
    """A list containing both stringified and real objects should also work."""
    ctx = MockContext()
    output_path = tmp_path / "output_mixed.docx"

    mixed_changes = [
        json.dumps({"type": "modify", "target_text": "original", "new_text": "first"}),
        {"type": "modify", "target_text": "text", "new_text": "second"},
    ]

    result = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=sample_docx,
            author_name="AI Agent",
            ctx=ctx,  # type: ignore[arg-type]
            changes=mixed_changes,  # type: ignore[arg-type]
            output_path=str(output_path),
        )
    )

    # Both edits target text that exists; both should apply.
    assert "Edits: 2 applied" in result
    assert output_path.exists()


def test_process_document_batch_rejects_unparseable_string(sample_docx, tmp_path):
    """
    A string that is not valid JSON cannot be salvaged into a change. Under the
    per-change validation contract this is no longer raised as an exception;
    instead the element is reported back as a validation rejection in the tool's
    text response, no edits are applied, and the error must be a clear
    validation message (not an opaque internal `_applied_status`-style error).
    """
    ctx = MockContext()

    bad_changes = ["this is not json at all"]

    result = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=sample_docx,
            author_name="AI Agent",
            ctx=ctx,  # type: ignore[arg-type]
            changes=bad_changes,  # type: ignore[arg-type]
            output_path=str(tmp_path / "fail.docx"),
        )
    )

    # The single unsalvageable element means there are no valid changes, so the
    # tool returns the all-failed message rather than raising.
    msg = result.lower()
    assert "failed validation" in msg
    # The rejection must reference the offending index, not leak engine internals.
    assert "changes[0]" in result
    assert "_applied_status" not in msg
    # And nothing should have been written.
    assert not (tmp_path / "fail.docx").exists()


# ---------------------------------------------------------------------------
# Bug 2: `page` is a document-page FILTER in search mode
# ---------------------------------------------------------------------------
#
# Helper: build a body large enough to span multiple pagination pages.
# The paginator targets ~19,000 chars per page. We construct a body where
# distinct marker phrases appear on known different doc pages.


# Paginator target is 19,000 chars per page (see adeu.pagination.PAGE_TARGET_CHARS).
# Each filler block must exceed that threshold so the paginator emits it on its
# own page, forcing the next marker phrase onto the following page. We use a
# little slack above the target to ensure deterministic page boundaries even if
# the constant nudges slightly in future.
PAGE_FILLER_CHARS = 20_000


def _multi_page_body() -> str:
    """
    Build a body that spans 5 document pages under the greedy bin-packing
    paginator:
      - page 1: "alpha-needle" + small text (well under 19k)
      - page 2: filler_a (oversized solo block)
      - page 3: "beta-needle" (small block, packs alone after oversize flush)
      - page 4: filler_b (oversized solo block)
      - page 5: "gamma-needle some text gamma-needle"

    Note: callers shouldn't depend on the exact page assignments above — they
    should ask the paginator at runtime which page each marker landed on. The
    multi-page tests below do that via the response builder's own
    `### Match N (pM)` annotations.

    Distribution-summary semantics only require that matches span >1 doc page,
    which this body guarantees.
    """
    filler_a = "a" * PAGE_FILLER_CHARS
    filler_b = "b" * PAGE_FILLER_CHARS
    return f"alpha-needle\n\n{filler_a}\n\nbeta-needle\n\n{filler_b}\n\ngamma-needle some text gamma-needle"


def _single_page_body() -> str:
    """A body that fits on a single document page."""
    return "The needle appears here. And again the needle. And once more: needle."


# Case 1: page omitted on single-page doc -> all matches, no distribution line
def test_search_page_omitted_single_page_no_distribution():
    body = _single_page_body()
    result = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )

    md = result.structured_content["markdown"]
    assert "Distribution across" not in md
    assert "Found 3 matches" in md
    # All matches present; index/page tag present.
    assert "### Match 1 (p1)" in md
    assert "### Match 3 (p1)" in md


# Case 2: page='all' explicit -> identical behavior to omitting
def test_search_page_all_explicit_matches_omitted():
    body = _single_page_body()
    a = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    b = build_search_response(
        text=body,
        search_query="all",  # NOTE: different query — just verifying 'all' string accepted below
        search_regex=False,
        search_case_sensitive=True,
        page="all",
        file_path="doc.docx",
    )
    # Sanity: this test pair only cares that page='all' doesn't error; identical
    # output assertion needs the same query, so do a second comparison:
    c = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page="all",
        file_path="doc.docx",
    )
    assert a.structured_content["markdown"] == c.structured_content["markdown"]
    # Also accept "ALL" case-insensitively.
    d = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page="ALL",
        file_path="doc.docx",
    )
    assert a.structured_content["markdown"] == d.structured_content["markdown"]
    # `b` exists only to confirm string "all" doesn't blow up on a different query path:
    assert "No matches found" in b.structured_content["markdown"]


# Case 3: page omitted on multi-page doc -> distribution summary appears
def test_search_page_omitted_multi_page_shows_distribution():
    body = _multi_page_body()
    result = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )

    md = result.structured_content["markdown"]
    # 4 matches: alpha-, beta-, gamma- (x2)
    assert "Found 4 matches" in md
    # Distribution line should be present and span >1 doc page. Don't pin
    # exact page counts — the paginator decides where each marker lands.
    import re

    dist_match = re.search(r"Distribution across (\d+) document pages", md)
    assert dist_match, f"Expected distribution summary in:\n{md}"
    n_pages_with_hits = int(dist_match.group(1))
    assert n_pages_with_hits >= 2

    # Per-page counts should sum to 4 (the total match count).
    per_page = re.findall(r"p(\d+): (\d+)", md)
    assert per_page, "Expected per-page count entries in distribution line"
    total = sum(int(c) for _p, c in per_page)
    assert total == 4


# Case 4: page=N as filter -> only matches on doc page N rendered
def test_search_page_filter_restricts_to_doc_page():
    body = _multi_page_body()

    # First, learn the doc-page distribution from an unfiltered query.
    import re

    unfiltered = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    unfiltered_md = unfiltered.structured_content["markdown"]
    match_pages = [int(p) for p in re.findall(r"### Match \d+ \(p(\d+)\)", unfiltered_md)]
    assert match_pages, "Expected matches in unfiltered output"

    # Pick a target page that has at least one hit and where other pages also
    # have hits (so we can also verify the 'Additional matches' hint).
    from collections import Counter

    page_counts = Counter(match_pages)
    multi_hit_pages = sorted(page_counts.keys())
    assert len(multi_hit_pages) >= 2, f"Test fixture must produce matches on >=2 doc pages; got {page_counts}"
    target_page = multi_hit_pages[-1]  # last page with hits
    other_pages = [p for p in multi_hit_pages if p != target_page]

    result = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=target_page,
        file_path="doc.docx",
    )
    md = result.structured_content["markdown"]

    # Header reports filter context
    assert f"on document page {target_page}" in md
    assert "(4 total in document)" in md
    # All rendered match headers should be (p<target_page>)
    rendered_pages = re.findall(r"### Match \d+ \(p(\d+)\)", md)
    assert rendered_pages, "Expected at least one rendered match header"
    assert all(int(p) == target_page for p in rendered_pages)
    # Hint about other pages with hits.
    other_pages_str = ", ".join(str(p) for p in other_pages)
    assert "Additional matches exist on page" in md
    assert other_pages_str in md


# Case 5: page=N valid but no hits there, hits exist elsewhere
def test_search_page_filter_empty_with_hits_elsewhere():
    body = _multi_page_body()
    # page=2 has only "beta-needle", so search for "alpha-needle" with page=2
    # yields zero on that page but hits elsewhere.
    result = build_search_response(
        text=body,
        search_query="alpha-needle",
        search_regex=False,
        search_case_sensitive=True,
        page=2,
        file_path="doc.docx",
    )

    md = result.structured_content["markdown"]
    assert "No matches on document page 2" in md
    assert "DOES appear elsewhere" in md
    assert "page 1" in md  # alpha-needle is on p1
    assert "Omit `page` or pass `page='all'`" in md


# Case 6: page=N exceeding total document pages -> ToolError
def test_search_page_filter_exceeds_total_pages_raises():
    body = _single_page_body()  # single doc page
    with pytest.raises(BuilderError) as exc_info:
        build_search_response(
            text=body,
            search_query="needle",
            search_regex=False,
            search_case_sensitive=True,
            page=5,
            file_path="doc.docx",
        )
    msg = str(exc_info.value)
    assert "Document page 5 is out of range" in msg
    assert "1 page" in msg


# Case 7: invalid page values -> ToolError
@pytest.mark.parametrize("bad_page", [0, -1, "garbage", "1.5"])
def test_search_invalid_page_values_raise(bad_page):
    body = _single_page_body()
    with pytest.raises(BuilderError) as exc_info:
        build_search_response(
            text=body,
            search_query="needle",
            search_regex=False,
            search_case_sensitive=True,
            page=bad_page,
            file_path="doc.docx",
        )
    assert "Invalid page value" in str(exc_info.value)


# Case 8: no matches anywhere -> standard empty message
def test_search_no_matches_anywhere():
    body = _single_page_body()
    result = build_search_response(
        text=body,
        search_query="haystack",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    md = result.structured_content["markdown"]
    assert "No matches found" in md


# Case 9: occurrence counts remain global when filtering
def test_search_occurrence_counts_remain_global_under_filter():
    """
    When filtering to a single page, the `*Occurrences:* appears X times` line
    must reflect the count across the WHOLE document, not just the filtered page.
    """
    import re

    body = _multi_page_body()

    # Locate which page the 'gamma-needle' marker lives on by running an
    # unfiltered search.
    unfiltered = build_search_response(
        text=body,
        search_query="gamma-needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    gamma_pages = [int(p) for p in re.findall(r"### Match \d+ \(p(\d+)\)", unfiltered.structured_content["markdown"])]
    assert gamma_pages, "Expected at least one gamma-needle match"
    gamma_target = gamma_pages[0]  # both gamma-needle hits live on the same page

    result = build_search_response(
        text=body,
        search_query="gamma-needle",
        search_regex=False,
        search_case_sensitive=True,
        page=gamma_target,
        file_path="doc.docx",
    )
    md = result.structured_content["markdown"]
    # Global count for "gamma-needle" is 2; the occurrence line must report
    # that even though we filtered to a single page.
    assert "appears 2 times in the document" in md

    # Stronger test: with query "needle" (4 global matches), filter to the
    # page containing alpha-needle. The single rendered match must still
    # carry the global "appears 4 times" annotation.
    unfiltered_all = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=None,
        file_path="doc.docx",
    )
    all_md = unfiltered_all.structured_content["markdown"]
    all_pages = [int(p) for p in re.findall(r"### Match \d+ \(p(\d+)\)", all_md)]
    assert len(all_pages) == 4
    # alpha-needle is the first match in document order.
    alpha_page = all_pages[0]

    result2 = build_search_response(
        text=body,
        search_query="needle",
        search_regex=False,
        search_case_sensitive=True,
        page=alpha_page,
        file_path="doc.docx",
    )
    md2 = result2.structured_content["markdown"]
    assert "appears 4 times in the document" in md2
