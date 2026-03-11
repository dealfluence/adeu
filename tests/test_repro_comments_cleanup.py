import io

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.models import ReviewAction
from adeu.redline.engine import RedlineEngine


def _inject_comment_into_element(engine: RedlineEngine, target_element, comment_text: str) -> str:
    """
    Helper to inject a complete comment structure (Start, End, Reference)
    directly into a specific DOM element (like a w:ins or w:del tag) to mock
    Word's behavior of tracking changes over commented text.
    """
    c_id = engine.comments_manager.add_comment("TestAuthor", comment_text)

    start = OxmlElement("w:commentRangeStart")
    start.set(qn("w:id"), c_id)

    end = OxmlElement("w:commentRangeEnd")
    end.set(qn("w:id"), c_id)

    ref_run = OxmlElement("w:r")
    ref = OxmlElement("w:commentReference")
    ref.set(qn("w:id"), c_id)
    ref_run.append(ref)

    # Append all parts inside the target element
    target_element.append(start)
    target_element.append(end)
    target_element.append(ref_run)

    return c_id


def test_reject_change_cleans_encapsulated_comments():
    """
    Tests that rejecting a w:ins element that contains comment anchors
    successfully cascades the deletion to all 4 comment XML parts.
    """
    # 1. Setup Document
    doc = Document()
    doc.add_paragraph("Base text. ")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 2. Inject a w:ins element manually
    engine = RedlineEngine(stream, author="TestAuthor")

    ins_tag = OxmlElement("w:ins")
    ins_tag.set(qn("w:id"), "99")
    ins_tag.set(qn("w:author"), "TestAuthor")

    r_tag = OxmlElement("w:r")
    t_tag = OxmlElement("w:t")
    t_tag.text = "Inserted with comment"
    r_tag.append(t_tag)
    ins_tag.append(r_tag)

    p_element = engine.doc.paragraphs[0]._element
    p_element.append(ins_tag)

    # 3. Add a comment INSIDE the w:ins tag
    _inject_comment_into_element(engine, ins_tag, "This comment lives inside the insertion.")

    # Verify pre-condition: Comment exists in parts
    c_mgr = engine.comments_manager
    assert len(c_mgr.comments_part.element.findall(qn("w:comment"))) == 1
    if c_mgr.ids_part:
        assert len(c_mgr.ids_part.element.findall(qn("w16cid:commentId"))) == 1

    # 4. Act: Reject the change (should delete the w:ins and trigger comment cleanup)
    action = ReviewAction(action="REJECT", target_id="Chg:99")
    applied, _ = engine.apply_review_actions([action])

    assert applied == 1, "Action should be successfully applied"

    # 5. Assert: Complete Eradication
    doc_xml = engine.doc.element.xml
    assert "w:commentRangeStart" not in doc_xml, "Start anchor leaked in body"
    assert "w:commentReference" not in doc_xml, "Reference anchor leaked in body"

    assert len(c_mgr.comments_part.element.findall(qn("w:comment"))) == 0, "Leaked in comments.xml"
    if c_mgr.ids_part:
        assert len(c_mgr.ids_part.element.findall(qn("w16cid:commentId"))) == 0, "Leaked in commentsIds.xml"
    if c_mgr.extended_part:
        assert len(c_mgr.extended_part.element.findall(qn("w15:commentEx"))) == 0, "Leaked in commentsExtended.xml"
    if c_mgr.extensible_part:
        assert len(c_mgr.extensible_part.element.findall(qn("w16cex:commentExtensible"))) == 0, (
            "Leaked in commentsExtensible.xml"
        )


def test_accept_all_revisions_total_comment_wipe():
    """
    Tests that accepting all revisions completely cleans the comment subsystem,
    preventing the invisible ghost comments seen in Issue 5.
    """
    # 1. Setup Document
    doc = Document()
    doc.add_paragraph("Some text.")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 2. Inject a w:del element containing a comment
    engine = RedlineEngine(stream, author="TestAuthor")

    del_tag = OxmlElement("w:del")
    del_tag.set(qn("w:id"), "88")
    del_tag.set(qn("w:author"), "TestAuthor")

    # 3. Add comment inside the w:del tag
    _inject_comment_into_element(engine, del_tag, "This comment lives inside the deletion.")

    # Add to body
    p_element = engine.doc.paragraphs[0]._element
    p_element.append(del_tag)

    # Pre-condition verification
    c_mgr = engine.comments_manager
    assert len(c_mgr.comments_part.element.findall(qn("w:comment"))) == 1

    # 4. Act: Accept all revisions (wipes w:del from body)
    engine.accept_all_revisions()

    # 5. Assert: Total Wipe
    doc_xml = engine.doc.element.xml
    assert "w:commentRangeStart" not in doc_xml
    assert "w:commentReference" not in doc_xml

    assert len(c_mgr.comments_part.element.findall(qn("w:comment"))) == 0
    if c_mgr.ids_part:
        assert len(c_mgr.ids_part.element.findall(qn("w16cid:commentId"))) == 0
