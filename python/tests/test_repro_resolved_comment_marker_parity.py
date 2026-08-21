"""
Resolved comment threads must be marked `(RESOLVED)` in every projection.

Word records resolution in `commentsExtended.xml` (`w15:done="1"`). Three of the
four projections rendered it — `DocumentMapper._map_comments`, and both Node
projections — but `adeu.ingest.render_comment` dropped it, so `read_docx`
showed a settled thread and an open one identically:

    [Com:1] Author @ 2026-04-18T21:02:00Z: Resolved comment
    [Com:4] Author @ 2026-04-18T21:02:00Z: Open comment

An agent reading that will happily re-litigate a comment the parties already
closed, and Python disagreed with Node on the same file.

Uses shared/fixtures/dirty_sample.docx, which carries one resolved thread
(Com:1) and one open thread (Com:4).
"""

import io
from pathlib import Path

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.redline.mapper import DocumentMapper

FIXTURE = Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "dirty_sample.docx"


@pytest.fixture(scope="module")
def dirty_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "[Com:" in line]


def test_ingest_marks_the_resolved_thread(dirty_bytes: bytes) -> None:
    text = extract_text_from_stream(io.BytesIO(dirty_bytes), clean_view=False, include_appendix=False)
    lines = _comment_lines(text)
    resolved = [line for line in lines if "(RESOLVED)" in line]
    assert len(resolved) == 1, f"expected exactly one resolved thread, got {lines}"
    assert "Resolved comment" in resolved[0]


def test_ingest_does_not_mark_the_open_thread(dirty_bytes: bytes) -> None:
    text = extract_text_from_stream(io.BytesIO(dirty_bytes), clean_view=False, include_appendix=False)
    open_lines = [line for line in _comment_lines(text) if "Open comment" in line]
    assert open_lines, "open comment missing from the projection"
    assert "(RESOLVED)" not in open_lines[0], open_lines[0]


def test_ingest_and_mapper_render_the_same_comment_lines(dirty_bytes: bytes) -> None:
    """
    The two projections must agree on the comment block, or the resolved
    marker shifts every offset after it.
    """
    ingest_text = extract_text_from_stream(io.BytesIO(dirty_bytes), clean_view=False, include_appendix=False)
    mapper_text = DocumentMapper(Document(io.BytesIO(dirty_bytes))).full_text
    assert _comment_lines(ingest_text) == _comment_lines(mapper_text)


def test_clean_view_hides_comments_entirely(dirty_bytes: bytes) -> None:
    text = extract_text_from_stream(io.BytesIO(dirty_bytes), clean_view=True, include_appendix=False)
    assert "[Com:" not in text
    assert "(RESOLVED)" not in text
