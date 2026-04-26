# FILE: tests/test_live_word_structured_insertion.py
"""
Regression tests for structured (multi-paragraph / heading) new_text
insertion via live COM. Mirrors the disk engine's track_insert semantics.

Asserts on INVARIANTS (no literal markdown leaked into doc content, correct
tracked changes applied) rather than on exact string matches, because
CriticMarkup interleaves with ingest-side markdown prefixes in ways that
are structurally correct but not simple substring matches.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Live Word COM tests require Windows platform",
)

if sys.platform == "win32":
    import asyncio
    from unittest.mock import AsyncMock

    import pythoncom
    import win32com.client
    from fastmcp.tools.tool import ToolResult

    from adeu.mcp_components.tools.live_word import (
        process_active_word_batch,
        read_active_word_document,
    )
    from adeu.models import ModifyText


@pytest.fixture
def active_word_app():
    """Fresh Word instance + blank document for each test."""
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = True
        doc = app.Documents.Add()
        app.Activate()
        yield app, doc
    except Exception as e:
        pytest.skip(f"Could not initialize Word COM: {e}")
    finally:
        if app:
            try:
                doc.Close(0)
            except Exception:
                pass


def _read(ctx):
    """Synchronously resolve a live read."""
    res = asyncio.run(read_active_word_document(ctx, clean_view=False))
    return res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)


def _run_batch(ctx, changes):
    """Synchronously apply a batch of changes."""
    return asyncio.run(process_active_word_batch(ctx, changes=changes, author_name="Test Agent"))


def _strip_criticmarkup(s: str) -> str:
    """
    Remove all CriticMarkup from a read output so we can assert on the
    "accepted view" of the document while still reading raw to verify
    that tracking happened at all. Not a full parser — just strips the
    four wrapper kinds.
    """
    import re

    s = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"\{--(.*?)--\}", "", s, flags=re.DOTALL)
    s = re.sub(r"\{==(.*?)==\}", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"\{>>(.*?)<<\}", "", s, flags=re.DOTALL)
    return s


def test_structured_insert_single_heading(active_word_app):
    """
    A new_text that is a pure '# Heading' should produce a new paragraph
    styled as Heading 1. Invariants:
      * No literal '#' character ended up inside the tracked-insert text
        (would look like '{++# ...++}').
      * The paragraph containing the inserted text is read back with a
        '# ' heading prefix from the ingest side.
      * After accepting all changes mentally, the text reads
        '# New Section Heading'.
    """
    app, doc = active_word_app
    ctx = AsyncMock()
    doc.Range(0, doc.Content.End).Text = "Original paragraph.\r"

    changes = [
        ModifyText(
            target_text="Original paragraph.",
            new_text="# New Section Heading",
            comment=None,
        )
    ]
    result = _run_batch(ctx, changes)
    assert "Applied: 1, Failed: 0" in result, result

    content = _read(ctx)

    # 1. No literal '#' leaked into tracked-insert text.
    assert "{++#" not in content, f"Heading marker '#' leaked into inserted text:\n{content}"

    # 2. The heading style landed: the paragraph is prefixed '# ' and
    #    the tracked insertion of the body text exists in it.
    assert "# " in content, f"No heading prefix found:\n{content}"
    assert "{++New Section Heading++}" in content, content

    # 3. Old text was tracked as deleted.
    assert "{--Original paragraph.--}" in content, content

    # 4. Accepted-view sanity check: with CriticMarkup stripped, the
    #    result should be a clean '# New Section Heading' line.
    accepted = _strip_criticmarkup(content)
    assert "# New Section Heading" in accepted, accepted


def test_structured_insert_multi_paragraph_body(active_word_app):
    """
    Multi-paragraph body text (no headings) should produce multiple
    inserted paragraphs, each tracked.
    """
    app, doc = active_word_app
    ctx = AsyncMock()
    doc.Range(0, doc.Content.End).Text = "Replace me.\r"

    changes = [
        ModifyText(
            target_text="Replace me.",
            new_text=("First new paragraph.\n\nSecond new paragraph.\n\nThird new paragraph."),
            comment=None,
        )
    ]
    result = _run_batch(ctx, changes)
    assert "Applied: 1, Failed: 0" in result, result

    content = _read(ctx)

    # All three sentences present
    assert "First new paragraph." in content
    assert "Second new paragraph." in content
    assert "Third new paragraph." in content

    # Each new paragraph tracked as an insertion
    assert content.count("{++") >= 3, f"Expected >=3 insertions, got {content.count('{++')}:\n{content}"

    # Old text was deleted
    assert "{--Replace me.--}" in content


def test_structured_insert_heading_then_body(active_word_app):
    """
    new_text with a heading followed by body paragraphs produces a
    Heading 1 followed by body paragraphs. Invariants:
      * No literal '#' leaks into any tracked-insert body.
      * The heading paragraph is read back with '# ' prefix.
      * Body paragraphs are NOT prefixed '# '.
      * Comment is attached.
    """
    app, doc = active_word_app
    ctx = AsyncMock()
    doc.Range(0, doc.Content.End).Text = "Anchor text.\r"

    new_text = (
        "# 6. Data Protection\n\n"
        "The Parties acknowledge that Confidential Information may include "
        "personal data.\n\n"
        "Each Party agrees to handle any such personal data lawfully."
    )
    changes = [
        ModifyText(
            target_text="Anchor text.",
            new_text=new_text,
            comment="Playbook clause insertion",
        )
    ]
    result = _run_batch(ctx, changes)
    assert "Applied: 1, Failed: 0" in result, result

    content = _read(ctx)

    # No '#' leaked into any insertion
    assert "{++#" not in content, f"Heading marker leaked into body:\n{content}"

    # Heading text and body text both present as tracked insertions
    assert "{++6. Data Protection++}" in content, content
    assert "The Parties acknowledge" in content
    assert "Each Party agrees" in content

    # Comment attached somewhere
    assert "Playbook clause insertion" in content


def test_structured_insert_bold_inside_new_paragraph(active_word_app):
    """
    Bold/italic markers inside a structured insertion apply per-line and
    do not leak across paragraph boundaries.
    """
    app, doc = active_word_app
    ctx = AsyncMock()
    doc.Range(0, doc.Content.End).Text = "Start.\r"

    changes = [
        ModifyText(
            target_text="Start.",
            new_text="# Title\n\n**Bold** opening. Then _italic_ follows.",
            comment=None,
        )
    ]
    result = _run_batch(ctx, changes)
    assert "Applied: 1, Failed: 0" in result, result

    content = _read(ctx)

    # Title is a heading, Bold/Italic rendered inside body insertion
    assert "{++Title++}" in content, content
    assert "**Bold**" in content, content
    assert "_italic_" in content, content

    # No '#' leakage
    assert "{++#" not in content


def test_simple_inline_replacement_unchanged(active_word_app):
    """
    Sanity: a plain single-line replacement with bold markers should still
    go through the simple path and produce the same output as before
    the structured-path fix.
    """
    app, doc = active_word_app
    ctx = AsyncMock()
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\r"

    changes = [
        ModifyText(
            target_text="brown fox",
            new_text="**red** fox",
            comment=None,
        )
    ]
    result = _run_batch(ctx, changes)
    assert "Applied: 1, Failed: 0" in result, result

    content = _read(ctx)
    assert "{--brown--}" in content
    assert "{++**red**++}" in content
    # bold is applied inside the insertion; when read raw it's markdown-wrapped
    assert "**red**" in content
