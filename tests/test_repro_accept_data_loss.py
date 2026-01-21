import io

import structlog
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit, ReviewAction
from adeu.redline.engine import RedlineEngine

logger = structlog.get_logger(__name__)


def test_repro_accept_deletion_keeps_insertion():
    """
    Scenario:
    1. Modifying text creates a Deletion (ID 1) and Insertion (ID 2).
    2. User accepts Deletion (ID 1).
    3. Insertion (ID 2) MUST remain.
    """
    doc = Document()
    doc.add_paragraph("Old Text")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 1. Apply Edit
    engine = RedlineEngine(stream, author="Me")
    edit = DocumentEdit(target_text="Old Text", new_text="New Text")
    engine.apply_edits([edit])

    stream_edited = engine.save_to_stream()

    # Verify Intermediate State
    text_mid = extract_text_from_stream(stream_edited)
    # Should see {--Old Text--} and {++New Text++}
    # Note: IDs might be 1 and 2
    assert "Old Text" in text_mid
    assert "New Text" in text_mid

    # 2. Accept the Deletion (ID 1)
    # We need to find the ID. In a clean doc, it starts at 1.
    # Typically Modification = Del(1) then Ins(2).

    # Debug: Print text to see IDs
    print(f"Mid Text: {text_mid}")

    # Assuming ID 1 is the deletion (track_delete_run called first)
    engine2 = RedlineEngine(stream_edited, author="Reviewer")
    action = ReviewAction(action="ACCEPT", target_id="1")
    engine2.apply_review_actions([action])

    stream_final = engine2.save_to_stream()
    text_final = extract_text_from_stream(stream_final)
    print(f"Final Text: {text_final}")

    # Check:
    # 1. Old Text should be GONE (Accepted Deletion)
    assert "Old Text" not in text_final, "Old Text should be removed"

    # 2. New Text should be PRESENT (Pending Insertion)
    # It should still be wrapped in {++...++} unless we accepted it too
    assert "New Text" in text_final, "New Text should remain"
    assert "{++New Text++}" in text_final, "New Text should still be an insertion"


def test_id_collision_prevention():
    """
    Ensure RedlineEngine respects existing IDs in the document.
    """
    doc = Document()
    p = doc.add_paragraph("Start")
    # Manually inject an ID=5 insertion
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "5")
    ins.set(qn("w:author"), "Existing")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "Existing"
    r.append(t)
    ins.append(r)
    p._element.append(ins)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # Create Engine
    engine = RedlineEngine(stream)

    # The next ID should be > 5 (i.e., 6)
    next_id = engine._get_next_id()
    assert int(next_id) > 5, f"Engine should pick ID > 5, got {next_id}"
