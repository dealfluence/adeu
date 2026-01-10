import io
import re
import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from adeu.redline.engine import RedlineEngine
from adeu.models import DocumentEdit, EditOperationType

def test_native_comment_creation_and_linking():
    """
    Verifies that applying an edit with a 'thought_process' correctly:
    1. Creates the word/comments.xml part.
    2. Links it to the document.
    3. Inserts w:commentRangeStart/End/Reference tags.
    4. Ensures IDs match.
    """
    # 1. Setup: Create a clean doc
    doc = Document()
    doc.add_paragraph("The quick brown fox.")
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 2. Action: Apply edit with comment
    edit = DocumentEdit(
        operation=EditOperationType.MODIFICATION,
        target_text="quick",
        new_text="slow",
        comment="Foxes are not always quick."
    )

    engine = RedlineEngine(stream)
    engine.apply_edits([edit])
    
    result_stream = engine.save_to_stream()
    
    # 3. Verification
    doc = Document(result_stream)
    
    # A. Check Document XML for Anchors
    doc_xml = doc.element.xml
    assert "w:commentRangeStart" in doc_xml, "Missing comment start tag"
    assert "w:commentRangeEnd" in doc_xml, "Missing comment end tag"
    assert "w:commentReference" in doc_xml, "Missing comment reference tag"

    # Extract the ID used in the document body
    # Regex finds: <w:commentRangeStart ... w:id="123" ...>
    id_match = re.search(r'<w:commentRangeStart[^>]*w:id="(\d+)"', doc_xml)
    assert id_match, "Could not find ID in comment range tag"
    comment_id = id_match.group(1)

    # B. Check for Comments Part existence via Relationships
    comments_part = None
    for rel in doc.part.rels.values():
        if rel.reltype == RT.COMMENTS:
            comments_part = rel.target_part
            break
    
    assert comments_part is not None, "word/comments.xml part was not found in relationships"

    # C. Check Content of Comments Part
    # We inspect the raw XML of the comments part
    comments_xml = comments_part.blob.decode("utf-8")
    
    assert "Foxes are not always quick." in comments_xml, "Comment text not found in comments.xml"
    
    # D. Verify ID Integrity
    # The ID found in the document body must exist in the comments part
    assert f'w:id="{comment_id}"' in comments_xml, f"Comment ID {comment_id} mismatch between document and comments part"

def test_multiple_comments_ids():
    """
    Verifies that multiple comments get unique IDs.
    """
    doc = Document()
    doc.add_paragraph("First sentence.")
    doc.add_paragraph("Second sentence.")
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    edit1 = DocumentEdit(
        operation=EditOperationType.INSERTION,
        target_text="First",
        new_text="The ",
        comment="Comment One"
    )
    edit2 = DocumentEdit(
        operation=EditOperationType.INSERTION,
        target_text="Second",
        new_text="The ",
        comment="Comment Two"
    )

    engine = RedlineEngine(stream)
    engine.apply_edits([edit1, edit2])
    
    result_stream = engine.save_to_stream()
    doc = Document(result_stream)
    
    # Check relationships
    comments_part = None
    for rel in doc.part.rels.values():
        if rel.reltype == RT.COMMENTS:
            comments_part = rel.target_part
            break
            
    comments_xml = comments_part.blob.decode("utf-8")
    
    # We should see both comments
    assert "Comment One" in comments_xml
    assert "Comment Two" in comments_xml
    
    # We should see two distinct IDs defined
    ids = re.findall(r'<w:comment\s+[^>]*w:id="(\d+)"', comments_xml)
    assert len(set(ids)) == 2, f"Expected 2 unique comment IDs, found {ids}"