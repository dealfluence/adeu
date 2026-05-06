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


def test_live_word_table_structure_and_mapping(active_word_app):
    """
    Validates Bug 1d (Table Structure) and 1a-1c (Table Modification Mapping).
    Ensures live COM natively extracts table cells with `|` and successfully
    maps string replacements perfectly inside complex cell boundaries.
    """
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = ""
    table = doc.Tables.Add(doc.Range(0, 0), NumRows=2, NumColumns=2)
    table.Cell(1, 1).Range.Text = "Region"
    table.Cell(1, 2).Range.Text = "Revenue"
    table.Cell(2, 1).Range.Text = "North"
    table.Cell(2, 2).Range.Text = "500"

    async def run_test():
        changes = [ModifyText(target_text="North", new_text="North America")]
        await process_active_word_batch(ctx, changes=changes, author_name="Agent")
        content = extract_content(await read_active_word_document(ctx))

        assert "Region | Revenue" in content
        assert "North{++ America++}" in content
        assert "}North" not in content

    run_async(run_test())


def test_live_word_accept_reject_reply(active_word_app):
    """
    End-to-end test: Validates that AcceptChange, RejectChange, and ReplyComment
    payloads are correctly applied to the active MS Word COM object.
    """
    from adeu.models import AcceptChange, ReplyComment

    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"
    doc.TrackRevisions = True

    start_del = doc.Content.Text.find("brown ")
    doc.Range(start_del, start_del + 6).Delete()
    doc.Range(start_del, start_del).Text = "red "

    start_com = doc.Content.Text.find("quick")
    doc.Comments.Add(doc.Range(start_com, start_com + 5), "Is it really quick?")
    doc.TrackRevisions = False

    async def run_test():
        content = extract_content(await read_active_word_document(ctx, clean_view=False))
        assert "{--brown --}" in content and "{++red ++}" in content

        changes = [
            AcceptChange(target_id="Chg:1"),
            AcceptChange(target_id="Chg:2"),
            ReplyComment(target_id="Com:0", text="Yes, absolutely."),
        ]
        await process_active_word_batch(ctx, changes=changes, author_name="QA Agent")

        final_content = extract_content(await read_active_word_document(ctx, clean_view=False))
        assert "{++" not in final_content and "{--" not in final_content
        assert "red fox" in final_content
        assert "Yes, absolutely." in final_content

    run_async(run_test())


def test_live_word_explicit_vs_inherited_formatting(active_word_app):
    """
    Ensures that inherited bold (from styles like Heading 1 or Strong) does not
    trigger ** emission, but explicit bold does. Ensures italic survives in headings.
    """
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "Heading with italic\n"
    p1 = doc.Paragraphs(1)
    p1.Style = -2  # wdStyleHeading1
    doc.Range(p1.Range.Start + 13, p1.Range.Start + 19).Italic = True

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

    p3_start = doc.Content.End - 1
    doc.Range(p3_start, p3_start).Text = "Normal with bold\n"
    p3 = doc.Paragraphs(3)
    doc.Range(p3.Range.Start + 12, p3.Range.Start + 16).Bold = True

    async def run_test():
        content = extract_content(await read_active_word_document(ctx))
        assert "# Heading with _italic_" in content
        assert "Strong with _italic_" in content
        assert "Normal with **bold**" in content

    run_async(run_test())


def test_live_word_body_bold_after_heading_sticky_state(active_word_app):
    """
    Ensures the 'in_heading' bold-suppression state properly resets at paragraph
    boundaries, so legitimate bold markers in body text/tables are not lost.
    """
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "Quarterly Report\nRegion | Revenue\n"
    doc.Paragraphs(1).Style = -2
    doc.Paragraphs(2).Range.Bold = True

    async def run_test():
        content = extract_content(await read_active_word_document(ctx))
        assert "**Quarterly Report**" not in content
        assert "# Quarterly Report" in content
        assert "**Region | Revenue**" in content

    run_async(run_test())


def test_live_word_overlapping_annotations(active_word_app):
    """
    Ensures that overlapping annotations (e.g. a comment wrapping a redline deletion)
    do not corrupt the generated CriticMarkup due to index drift.
    """
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"
    doc.TrackRevisions = True

    start_del = doc.Content.Text.find("brown ")
    doc.Range(start_del, start_del + 6).Delete()
    doc.Range(start_del, start_del).Text = "red "

    start_com = doc.Content.Text.find("quick")
    end_com = doc.Content.Text.find("fox") + 3
    doc.Comments.Add(doc.Range(start_com, end_com), "Color comment")

    async def run_test():
        content = extract_content(await read_active_word_document(ctx))
        assert content.count("{==") == 2 and content.count("==}") == 2
        assert content.count("{++") == 1 and content.count("++}") == 1
        assert content.count("{--") == 1 and content.count("--}") == 1
        assert "{={++=" not in content

    run_async(run_test())


def test_live_word_pure_comment_same_text(active_word_app):
    """Validates Bug Fix: Pure comment should not produce any tracked revisions."""
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.Range(0, doc.Content.End).Text = "MUTUAL NON-DISCLOSURE AGREEMENT\n"
    doc.TrackRevisions = True

    async def run_test():
        changes = [
            ModifyText(
                target_text="MUTUAL NON-DISCLOSURE AGREEMENT",
                new_text="MUTUAL NON-DISCLOSURE AGREEMENT",
                comment="Needs legal review.",
            )
        ]
        await process_active_word_batch(ctx, changes=changes, author_name="Claude AI")

        assert doc.Revisions.Count == 0
        assert doc.Comments.Count == 1

        content = extract_content(await read_active_word_document(ctx))
        assert "{==MUTUAL NON-DISCLOSURE AGREEMENT==}" in content
        assert "{++" not in content and "{--" not in content
        assert "Needs legal review." in content

    run_async(run_test())


def test_live_word_read_returns_filepath_in_content(active_word_app, tmp_path):
    """Validates that reading without providing a path includes the absolute file path in the content."""
    from fastmcp.tools.tool import ToolResult

    app, doc = active_word_app
    ctx = get_mock_ctx()

    temp_file = tmp_path / "live_path_test.docx"
    doc.SaveAs2(str(temp_file))

    async def run_test():
        res = await read_active_word_document(ctx, clean_view=False)
        if isinstance(res, ToolResult):
            if isinstance(res.content, list):
                content = "".join(getattr(c, "text", str(c)) for c in res.content)
            else:
                content = str(res.content)
        else:
            content = str(res)

        assert str(temp_file) in content

    run_async(run_test())


def test_live_word_multi_paragraph_insert_split_deletion(active_word_app):
    """
    Test to explicitly demonstrate BUG-03 and BUG-04.
    Replaces a single line with two paragraphs without displacing deletions.
    """
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.TrackRevisions = False
    doc.Range(0, doc.Content.End).Text = "Single paragraph. Replace this sentence.\n"

    async def run_test():
        changes = [
            ModifyText(
                target_text="Replace this sentence.",
                new_text="Line 1 of new content.\nLine 2 of new content.",
                comment="Anchored comment.",
            )
        ]
        await process_active_word_batch(ctx, changes=changes, author_name="Claude AI")
        content = extract_content(await read_active_word_document(ctx))

        del_idx = content.find("{--Replace this sentence.--}")
        line2_idx = content.find("Line 2 of new content.")
        assert del_idx < line2_idx
        assert "Anchored comment." in content

    run_async(run_test())


def test_live_word_bug_04_garbled_text(active_word_app):
    """Ensures new_text with paragraph breaks and formatting does not garble text."""
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.TrackRevisions = False
    original = "Company, incorporated under the laws of [Country], business identity code [ID], having its principal place of business.\n"
    doc.Range(0, doc.Content.End).Text = original

    async def run_test():
        changes = [
            ModifyText(
                target_text="business identity code [ID]",
                new_text="**business identity code** [ID]\n\nTest second paragraph inserted here.",
            )
        ]
        await process_active_word_batch(ctx, changes=changes, author_name="Claude AI")
        content = extract_content(await read_active_word_document(ctx))

        assert "{--business identity code [ID]--}" in content
        assert "{++**business identity code** [ID]++}" in content
        assert "the laws of [Country], business identity code" not in content.replace(original, "")

    run_async(run_test())


def test_live_word_obs_02_deletion_insertion_order(active_word_app):
    """Ensures Live COM automation forces the insertion AFTER the original text before deleting."""
    app, doc = active_word_app
    ctx = get_mock_ctx()

    doc.TrackRevisions = False
    doc.Range(0, doc.Content.End).Text = "The quick brown fox.\n"

    async def run_test():
        changes = [ModifyText(target_text="brown", new_text="red")]
        await process_active_word_batch(ctx, changes=changes, author_name="Testing Agent")
        content = extract_content(await read_active_word_document(ctx))

        assert "{--brown--}{++red++}" in content

    run_async(run_test())
