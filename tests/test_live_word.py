import sys
from unittest.mock import AsyncMock

import pytest

# Only run these tests on Windows since COM requires it
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Live Word COM tests require Windows platform")

if sys.platform == "win32":
    import pythoncom
    import win32com.client
    from fastmcp.tools.tool import ToolResult

    from adeu.mcp_components.tools.live_word import process_active_word_batch, read_active_word_document
    from adeu.models import ModifyText


@pytest.fixture
def active_word_app():
    """
    Creates an ephemeral, visible MS Word instance with a fresh document.
    Ensures it is torn down properly after the test.
    """
    pythoncom.CoInitialize()

    app = None
    try:
        # Dispatch starts a new background instance if one doesn't exist.
        # GetActiveObject will then be able to hook into it in the tool.
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = True  # Needs to be visible/active for GetActiveObject sometimes
        doc = app.Documents.Add()

        # Bring to front so GetActiveObject definitely binds to this instance
        app.Activate()

        # Seed initial content
        doc.Range(0, 0).Text = "Hello world! This is a live testing document.\n"

        yield app, doc

    except Exception as e:
        pytest.skip(f"Could not initialize Word COM for testing: {e}")

    finally:
        if app:
            try:
                doc.Close(0)  # 0 = wdDoNotSaveChanges
            except Exception:
                pass
            # We intentionally omit app.Quit() and pythoncom.CoUninitialize()
            # to avoid Windows Access Violations (0x800706be) when Pytest holds COM locals.


def test_live_word_read_and_modify(active_word_app):
    """
    End-to-end test: Reads from COM, issues a ModifyText payload, and verifies the redline
    was correctly tracked and applied.
    """
    import asyncio

    app, doc = active_word_app

    # Create a mock FastMCP Context
    ctx = AsyncMock()

    async def run_test():
        # Step 1: Verify Initial Extraction
        content_res = await read_active_word_document(ctx, clean_view=False)
        content = (
            content_res.structured_content["markdown"] if isinstance(content_res, ToolResult) else str(content_res)
        )
        assert "Hello world!" in content

        # Step 2: Apply a Modification
        changes = [
            ModifyText(target_text="live testing document", new_text="fully verified dynamic canvas", comment=None)
        ]

        # Process batch as "Testing Agent"
        result = await process_active_word_batch(ctx, changes=changes, author_name="Testing Agent")
        assert "Applied: 1, Failed: 0" in result

        # Step 3: Re-read to verify CriticMarkup injection was correct!
        updated_content_res = await read_active_word_document(ctx, clean_view=False)
        updated_content = (
            updated_content_res.structured_content["markdown"]
            if isinstance(updated_content_res, ToolResult)
            else str(updated_content_res)
        )

        # The output should contain the CriticMarkup showing track changes:
        # {--live testing document--} and {++fully verified dynamic canvas++}
        assert "{--live testing document--}" in updated_content
        assert "{++fully verified dynamic canvas++}" in updated_content

    asyncio.run(run_test())


def test_live_word_modify_with_comment(active_word_app):
    """
    End-to-end test: Validates that when a ModifyText payload includes a comment,
    the comment is correctly attached to the newly inserted text in Word,
    and successfully extracted back out as CriticMarkup.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    # Reset document content
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    async def run_test():
        # 1. Apply a Modification WITH a comment
        changes = [ModifyText(target_text="quick", new_text="sleepy", comment="Foxes are very tired today.")]

        res = await process_active_word_batch(ctx, changes=changes, author_name="Testing Agent")
        assert "Applied: 1, Failed: 0" in res

        # 2. Check if comment was physically added to the Word COM object
        assert doc.Comments.Count == 1, "Comment was not added to the Word Document!"

        # 3. Check extraction output
        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        assert "Foxes are very tired today." in content, f"Comment missing from extraction. Extracted: {content}"

        # 4. Verify Strict Nesting (Comment wraps the Insertion, no interleaved tags)
        # We want: {=={++sleepy++}...==}
        # NOT: {++{==sleepy++}...==}
        assert "{=={++sleepy++}" in content, f"Tags interleaved incorrectly! Extracted: {content}"

    asyncio.run(run_test())


def test_live_word_vs_redline_engine_parity(active_word_app, tmp_path):
    """
    Ensures that the CriticMarkup generated by the LiveWordEngine (COM) perfectly
    aligns with the CriticMarkup generated by the XML-based RedlineEngine (ingest).
    """
    import asyncio
    import io

    from adeu.ingest import extract_text_from_stream

    app, doc = active_word_app
    ctx = AsyncMock()

    # Setup complex state in the live document
    doc.Range(0, doc.Content.End).Text = "Base text for parity test.\n"

    doc.TrackRevisions = True
    # Replace "Base text" with "Modified text"
    rng = doc.Range(0, 9)
    rng.Text = "Modified text"

    # Add comment on "parity"
    rng_comment = doc.Range(doc.Content.Text.find("parity"), doc.Content.Text.find("parity") + 6)
    doc.Comments.Add(rng_comment, "Parity comment")

    async def run_test():
        # 1. Extract via Live Word COM
        live_content_res = await read_active_word_document(ctx, clean_view=False)
        live_text = (
            live_content_res.structured_content["markdown"]
            if isinstance(live_content_res, ToolResult)
            else str(live_content_res)
        )

        # 2. Save to disk to read via XML
        temp_file = tmp_path / "parity.docx"
        doc.SaveAs2(str(temp_file))

        # 3. Extract via XML RedlineEngine
        with open(temp_file, "rb") as f:
            xml_text = extract_text_from_stream(io.BytesIO(f.read()))

        # 4. Compare critical markup elements
        # Both should identify the deletion, insertion, and comment scopes.
        assert "{--Base text--}" in live_text
        assert "{++Modified text++}" in live_text
        assert "{==parity==}" in live_text
        assert "Parity comment" in live_text

        assert "{--Base text--}" in xml_text
        assert "{++Modified text++}" in xml_text
        assert "{==parity==}" in xml_text
        assert "Parity comment" in xml_text

    asyncio.run(run_test())


def test_live_word_overlapping_annotations(active_word_app):
    """
    Ensures that overlapping annotations (e.g. a comment wrapping a redline deletion)
    do not corrupt the generated CriticMarkup due to index drift.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    doc.TrackRevisions = True

    # 1. Delete "brown "
    start_del = doc.Content.Text.find("brown ")
    doc.Range(start_del, start_del + 6).Delete()

    # 2. Insert "red "
    doc.Range(start_del, start_del).Text = "red "

    # 3. Add comment spanning the area
    # Word's Content.Text currently exposes "The quick red fox.\n"
    start_com = doc.Content.Text.find("quick")
    end_com = doc.Content.Text.find("fox") + 3
    doc.Comments.Add(doc.Range(start_com, end_com), "Color comment")

    async def run_test():
        res = await read_active_word_document(ctx, clean_view=False)
        content = res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)

        # Validate that the markup is completely balanced and uncorrupted
        assert content.count("{==") == 1
        assert content.count("==}") == 1
        assert content.count("{++") == 1
        assert content.count("++}") == 1
        assert content.count("{--") == 1
        assert content.count("--}") == 1

        # Tags should not be mangled together like {={++=
        assert "{={++=" not in content
        assert "}==}" not in content

    asyncio.run(run_test())
