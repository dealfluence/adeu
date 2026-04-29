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

        # 4. Verify that the output uses the flattened state machine format
        # The text 'sleepy' is an insertion, so it gets {++ ++}.
        # The comment metadata is attached to the same block.
        assert "{++sleepy++}" in content, f"Insertion tag missing! Extracted: {content}"
        assert "{--quick--}" in content, f"Deletion tag missing! Extracted: {content}"
        assert "{=={++" not in content, "Tags should be flattened, not nested!"

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

        # 4. Parity Verification
        # Both engines should extract the exact same critical markup representation.
        # Word's track changes behaves differently based on Smart Cut/Paste settings,
        # so we verify the outputs against each other, rather than a hardcoded string.
        assert "{++Modified text++}" in live_text
        assert "{==parity==}" in live_text
        assert "Parity comment" in live_text

        assert live_text == xml_text

    asyncio.run(run_test())


def test_live_word_complex_formatting(active_word_app):
    """
    Ensures that when the LLM supplies Markdown via Live COM ModifyText,
    the Markdown is correctly parsed and native Word fonts (Bold/Italic) are applied.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    async def run_test():
        changes = [ModifyText(target_text="brown fox", new_text="**bold** and _italic_", comment=None)]

        res = await process_active_word_batch(ctx, changes=changes, author_name="Agent")
        assert "Applied: 1" in res

        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        # Live COM extraction now parses formatting parity identically to Disk XML
        assert "{++**bold** and _italic_++}" in content
        assert "{--brown fox--}" in content

    asyncio.run(run_test())


def test_live_word_cross_boundary_edits_rescue_comments(active_word_app):
    """
    Validates Bug 1 fix: Replacing text that contains a comment should rescue
    the comment and re-anchor it to the newly inserted text, instead of destroying it.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    doc.Range(0, doc.Content.End).Text = "Initial manuscript document.\n"

    # Add comment specifically targeting "manuscript"
    start_idx = doc.Content.Text.find("manuscript")
    doc.Comments.Add(doc.Range(start_idx, start_idx + 10), "Editorial comment")

    async def run_test():
        changes = [ModifyText(target_text="manuscript", new_text="typescript", comment=None)]
        await process_active_word_batch(ctx, changes=changes, author_name="Agent")

        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        # Verify the original comment wasn't silently destroyed!
        assert "Editorial comment" in content
        assert "{++typescript++}" in content

    asyncio.run(run_test())


def test_live_word_multiple_comments_overwrite(active_word_app):
    """
    Validates Bug 1 & 3 fix: Ensures multiple comments do not overwrite
    each other in live memory due to shared w:id="0" states before a save.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    doc.Range(0, doc.Content.End).Text = "Initial document.\n"

    # Add comment 1
    doc.Comments.Add(doc.Range(0, 7), "Comment One")

    # Add comment 2
    doc.Comments.Add(doc.Range(8, 16), "Comment Two")

    async def run_test():
        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        # Both comments should appear distinctly with their own IDs
        assert "Comment One" in content
        assert "Comment Two" in content
        assert "Com:0" in content
        assert "Com:1" in content

    asyncio.run(run_test())


def test_live_word_table_structure_and_mapping(active_word_app):
    """
    Validates Bug 1d (Table Structure) and 1a-1c (Table Modification Mapping).
    Ensures live COM natively extracts table cells with `|` and successfully
    maps string replacements perfectly inside complex cell boundaries.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    # Create a 2x2 table natively in Word
    doc.Range(0, doc.Content.End).Text = ""
    table = doc.Tables.Add(doc.Range(0, 0), NumRows=2, NumColumns=2)
    table.Cell(1, 1).Range.Text = "Region"
    table.Cell(1, 2).Range.Text = "Revenue"
    table.Cell(2, 1).Range.Text = "North"
    table.Cell(2, 2).Range.Text = "500"

    async def run_test():
        # Replace cell content, testing the mapping array offsets
        changes = [ModifyText(target_text="North", new_text="North America", comment=None)]
        await process_active_word_batch(ctx, changes=changes, author_name="Agent")

        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        # Verify table structure markers (`|`) are present (Bug 1d)
        assert "Region | Revenue" in content
        # Verify atomic insertion is fully intact (Bug 1a, 1b, 1c)
        assert "North{++ America++}" in content
        assert "}North" not in content  # No phantom trailing text

    asyncio.run(run_test())


def test_live_word_accept_reject_reply(active_word_app):
    """
    End-to-end test: Validates that AcceptChange, RejectChange, and ReplyComment
    payloads are correctly applied to the active MS Word COM object, modifying
    its state and successfully reflecting in a subsequent read.
    """
    import asyncio

    from adeu.models import AcceptChange, ReplyComment

    app, doc = active_word_app
    ctx = AsyncMock()

    # Initial Setup
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    doc.TrackRevisions = True

    # Create a Deletion
    start_del = doc.Content.Text.find("brown ")
    doc.Range(start_del, start_del + 6).Delete()

    # Create an Insertion
    doc.Range(start_del, start_del).Text = "red "

    # Create a Comment
    start_com = doc.Content.Text.find("quick")
    doc.Comments.Add(doc.Range(start_com, start_com + 5), "Is it really quick?")

    doc.TrackRevisions = False  # Disable tracking so our API test runs cleanly

    async def run_test():
        # 1. Read to ensure tags and metadata blocks exist
        res = await read_active_word_document(ctx, clean_view=False)
        content = res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)

        assert "{--brown --}" in content
        assert "{++red ++}" in content
        assert "Is it really quick?" in content

        # 2. Fire the Review APIs
        # Based on COM extraction behavior, revisions will be Chg:1 and Chg:2. Comment will be Com:0.
        # We will Accept both changes to arrive at "The quick red fox."
        changes = [
            AcceptChange(target_id="Chg:1"),
            AcceptChange(target_id="Chg:2"),
            ReplyComment(target_id="Com:0", text="Yes, absolutely."),
        ]

        process_res = await process_active_word_batch(ctx, changes=changes, author_name="QA Agent")
        assert "Failed: 0" in process_res, f"Batch apply failed: {process_res}"

        # 3. Verify final state
        final_res = await read_active_word_document(ctx, clean_view=False)
        final_content = (
            final_res.structured_content["markdown"] if isinstance(final_res, ToolResult) else str(final_res)
        )

        # Redlines should be resolved (no markup tags)
        assert "{++" not in final_content
        assert "{--" not in final_content

        # Content should reflect accepted edits
        assert "brown" not in final_content
        assert "red fox" in final_content

        # The reply comment must be present in the metadata block
        assert "Yes, absolutely." in final_content

    asyncio.run(run_test())


def test_live_word_explicit_vs_inherited_formatting(active_word_app):
    """
    Regression test for BUG 1 and 2 (Round 13):
    Ensures that inherited bold (from styles like Heading 1 or Strong) does not
    trigger ** emission, but explicit bold does. Ensures italic survives in headings.
    """
    import asyncio

    from fastmcp.tools.tool import ToolResult

    app, doc = active_word_app
    ctx = AsyncMock()

    # Create Paragraph 1: Heading with Italic
    doc.Range(0, doc.Content.End).Text = "Heading with italic\n"
    p1 = doc.Paragraphs(1)
    p1.Style = -2  # wdStyleHeading1
    doc.Range(p1.Range.Start + 13, p1.Range.Start + 19).Italic = True

    # Create Paragraph 2: Strong with Italic
    p2_start = doc.Content.End - 1
    doc.Range(p2_start, p2_start).Text = "Strong with italic\n"
    p2 = doc.Paragraphs(2)
    try:
        strong_style = doc.Styles.Add("TestStrong", 1)  # 1 = wdStyleTypeParagraph
        strong_style.Font.Bold = True
        p2.Style = strong_style
    except Exception:
        p2.Range.Bold = True  # fallback
    doc.Range(p2.Range.Start + 12, p2.Range.Start + 18).Italic = True

    # Create Paragraph 3: Normal with Explicit Bold
    p3_start = doc.Content.End - 1
    doc.Range(p3_start, p3_start).Text = "Normal with bold\n"
    p3 = doc.Paragraphs(3)
    doc.Range(p3.Range.Start + 12, p3.Range.Start + 16).Bold = True

    async def run_test():
        res = await read_active_word_document(ctx, clean_view=False)
        content = res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)

        # 1. Heading italic MUST survive, heading bold MUST NOT emit **
        assert "# Heading with _italic_" in content, f"Heading formatting failed: {content}"

        # 2. Strong paragraph MUST NOT emit ** for its inherited bold, but italic MUST survive
        assert "Strong with _italic_" in content, f"Strong paragraph formatting failed: {content}"

        # 3. Explicit bold in Normal paragraph MUST emit **
        assert "Normal with **bold**" in content, f"Explicit bold failed: {content}"

    asyncio.run(run_test())


def test_live_word_body_bold_after_heading_sticky_state(active_word_app):
    """
    Regression test for BUG 1 (Round 11):
    Ensures the 'in_heading' bold-suppression state properly resets at paragraph
    boundaries (\\r), so legitimate bold markers in body text/tables are not lost.
    """
    import asyncio

    app, doc = active_word_app
    ctx = AsyncMock()

    # 1. Setup Document with Heading followed by Bold Body
    doc.Range(0, doc.Content.End).Text = "Quarterly Report\nRegion | Revenue\n"

    p1 = doc.Paragraphs(1)
    p1.Style = -2  # wdStyleHeading1

    p2 = doc.Paragraphs(2)
    p2.Range.Bold = True  # Explicit bold on body text

    async def run_test():
        res = await read_active_word_document(ctx, clean_view=False)
        content = res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)

        # Heading must NOT be bolded
        assert "**Quarterly Report**" not in content, "Heading was improperly bolded"
        assert "# Quarterly Report" in content

        # Body text MUST be bolded (This will fail in Round 11)
        assert "**Region | Revenue**" in content, "Bold suppression leaked to body text!"

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
        # With the ingest.py state machine, overlaps are flattened.
        # The comment spans across an insertion and a deletion, so it splits into two {==...==} blocks.
        assert content.count("{==") == 2
        assert content.count("==}") == 2
        assert content.count("{++") == 1
        assert content.count("++}") == 1
        assert content.count("{--") == 1
        assert content.count("--}") == 1

        # Tags should not be mangled together like {={++=
        assert "{={++=" not in content
        assert "}==}" not in content

    asyncio.run(run_test())


def test_live_word_pure_comment_same_text(active_word_app):
    """
    Validates Bug Fix: Pure comment (same target and new text) should not produce
    any tracked text revisions, only a comment.
    """
    import asyncio

    from fastmcp.tools.tool import ToolResult

    from adeu.models import ModifyText

    app, doc = active_word_app
    ctx = AsyncMock()

    doc.Range(0, doc.Content.End).Text = "MUTUAL NON-DISCLOSURE AGREEMENT\n"
    doc.TrackRevisions = True

    async def run_test():
        changes = [
            ModifyText(
                target_text="MUTUAL NON-DISCLOSURE AGREEMENT",
                new_text="MUTUAL NON-DISCLOSURE AGREEMENT",
                comment="This clause needs legal review.",
            )
        ]

        res = await process_active_word_batch(ctx, changes=changes, author_name="Claude AI")
        assert "Applied: 1, Failed: 0" in res

        # The document should have 0 tracked revisions, and exactly 1 comment.
        assert doc.Revisions.Count == 0, "Spurious tracked changes were created!"
        assert doc.Comments.Count == 1

        read_res = await read_active_word_document(ctx, clean_view=False)
        content = read_res.structured_content["markdown"] if isinstance(read_res, ToolResult) else str(read_res)

        # Assert critic markup anchor exists, but NO insertions/deletions
        assert "{==MUTUAL NON-DISCLOSURE AGREEMENT==}" in content
        assert "{++" not in content
        assert "{--" not in content
        assert "This clause needs legal review." in content

    asyncio.run(run_test())


def test_live_word_multi_paragraph_insert_split_deletion(active_word_app):
    """
    Test to explicitly demonstrate BUG-03 and BUG-04.
    Replaces a single line with two paragraphs.
    Verifies that the deletion is not pushed past the second paragraph,
    and that the comment anchor wraps BOTH paragraphs.
    """
    import asyncio
    from fastmcp.tools.tool import ToolResult
    from adeu.models import ModifyText

    app, doc = active_word_app
    ctx = AsyncMock()

    # Setup without tracking so the base text is clean
    doc.TrackRevisions = False
    doc.Range(0, doc.Content.End).Text = "This is a single paragraph. We will replace this specific sentence completely.\n"

    async def run_test():
        changes = [
            ModifyText(
                target_text="We will replace this specific sentence completely.",
                new_text="Line 1 of new content.\nLine 2 of new content.",
                comment="This comment should span both lines."
            )
        ]

        await process_active_word_batch(ctx, changes=changes, author_name="Claude AI")

        res = await read_active_word_document(ctx, clean_view=False)
        content = res.structured_content["markdown"] if isinstance(res, ToolResult) else str(res)

        deletion_str = "{--We will replace this specific sentence completely.--}"
        line1_str = "Line 1 of new content."
        line2_str = "Line 2 of new content."

        del_idx = content.find(deletion_str)
        line2_idx = content.find(line2_str)

        assert del_idx < line2_idx, f"BUG-03: Deletion was displaced past the paragraph boundary!\nContent:\n{content}"

        first_comment_end = content.find("==}", content.find(line1_str))
        assert first_comment_end > line2_idx, f"BUG-04: Comment highlight ended before Line 2!\nContent:\n{content}"

    asyncio.run(run_test())
