import sys

import pytest

from tests.utils import extract_content, get_mock_ctx, run_async

# Only run these tests on Windows since COM requires it
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Live Word COM tests require Windows platform")

if sys.platform == "win32":
    from adeu.mcp_components.tools.live_word import (
        process_active_word_batch,
        read_active_word_document,
    )
    from adeu.models import ModifyText


def test_live_word_read_and_modify(active_word_app):
    """End-to-end test: Reads from COM, issues a ModifyText payload, and verifies the redline."""
    app, doc = active_word_app
    ctx = get_mock_ctx()

    async def run_test():
        # Step 1: Verify Initial Extraction
        content = extract_content(await read_active_word_document(ctx))
        assert "Hello world!" in content

        # Step 2: Apply a Modification
        changes = [ModifyText(target_text="live testing document", new_text="fully verified dynamic canvas")]
        result = await process_active_word_batch(ctx, changes=changes, author_name="Testing Agent")
        assert "Applied: 1, Failed: 0" in result

        # Step 3: Re-read to verify CriticMarkup injection
        updated_content = extract_content(await read_active_word_document(ctx))
        assert "{--live testing document--}" in updated_content
        assert "{++fully verified dynamic canvas++}" in updated_content

    run_async(run_test())


def test_live_word_modify_with_comment(active_word_app):
    """Validates that a comment is correctly attached and extracted."""
    app, doc = active_word_app
    ctx = get_mock_ctx()

    # Reset document content
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    async def run_test():
        changes = [ModifyText(target_text="quick", new_text="sleepy", comment="Foxes are very tired today.")]
        res = await process_active_word_batch(ctx, changes=changes, author_name="Testing Agent")
        assert "Applied: 1, Failed: 0" in res

        # Check if comment was physically added and extracted back
        assert doc.Comments.Count == 1
        content = extract_content(await read_active_word_document(ctx))
        assert "Foxes are very tired today." in content
        assert "{++sleepy++}" in content and "{--quick--}" in content

    run_async(run_test())


def test_live_word_vs_redline_engine_parity(active_word_app, tmp_path):
    """Ensures parity between LiveWordEngine (COM) and XML-based RedlineEngine."""
    import io

    from adeu.ingest import extract_text_from_stream

    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "Base text for parity test.\n"
    doc.TrackRevisions = True
    doc.Range(0, 9).Text = "Modified text"
    doc.Comments.Add(doc.Range(doc.Content.Text.find("parity"), doc.Content.Text.find("parity") + 6), "Parity comment")

    async def run_test():
        live_text = extract_content(await read_active_word_document(ctx))

        temp_file = tmp_path / "parity.docx"
        doc.SaveAs2(str(temp_file))

        with open(temp_file, "rb") as f:
            xml_text = extract_text_from_stream(io.BytesIO(f.read()))

        for text in [live_text, xml_text]:
            assert "{++Modified text++}" in text
            assert "{==parity==}" in text
            assert "Parity comment" in text

    run_async(run_test())


@pytest.mark.parametrize(
    "target, new, expected_markup",
    [
        ("brown fox", "**bold** and _italic_", "{++**bold** and _italic_++}"),
    ],
)
def test_live_word_complex_formatting(active_word_app, target, new, expected_markup):
    """Ensures Markdown via Live COM is correctly parsed and applied."""
    app, doc = active_word_app
    ctx = get_mock_ctx()
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    async def run_test():
        changes = [ModifyText(target_text=target, new_text=new)]
        await process_active_word_batch(ctx, changes=changes, author_name="Agent")
        content = extract_content(await read_active_word_document(ctx))
        assert expected_markup in content
        assert "{--brown fox--}" in content

    run_async(run_test())


def test_live_word_cross_boundary_edits_rescue_comments(active_word_app):
    """Validates that replacing text containing a comment rescues the comment."""
    app, doc = active_word_app
    ctx = get_mock_ctx()
    doc.Range(0, doc.Content.End).Text = "Initial manuscript document.\n"
    start_idx = doc.Content.Text.find("manuscript")
    doc.Comments.Add(doc.Range(start_idx, start_idx + 10), "Editorial comment")

    async def run_test():
        changes = [ModifyText(target_text="manuscript", new_text="typescript")]
        await process_active_word_batch(ctx, changes=changes, author_name="Agent")
        content = extract_content(await read_active_word_document(ctx))
        assert "Editorial comment" in content
        assert "{++typescript++}" in content

    run_async(run_test())


def test_live_word_multiple_comments_overwrite(active_word_app):
    """Ensures multiple comments do not overwrite each other in live memory."""
    app, doc = active_word_app
    ctx = get_mock_ctx()
    doc.Range(0, doc.Content.End).Text = "Initial document.\n"
    doc.Comments.Add(doc.Range(0, 7), "Comment One")
    doc.Comments.Add(doc.Range(8, 16), "Comment Two")

    async def run_test():
        content = extract_content(await read_active_word_document(ctx))
        assert "Comment One" in content and "Comment Two" in content
        assert "Com:0" in content and "Com:1" in content

    run_async(run_test())
