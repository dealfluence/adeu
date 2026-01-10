import io
import pytest
from docx import Document
from adeu.redline.engine import RedlineEngine
from adeu.models import DocumentEdit, EditOperationType
from adeu.ingest import extract_text_from_stream

def test_delete_paragraph_with_newline():
    """
    Scenario: User deletes "Paragraph 1.\n\n".
    Current Behavior (Bug): Mapper fails to find "Paragraph 1.\n\n" because \n\n is virtual.
    Expected: Engine should match "Paragraph 1." and delete it.
    """
    doc = Document()
    doc.add_paragraph("Paragraph 1.")
    doc.add_paragraph("Paragraph 2.")
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    # Text: "Paragraph 1.\n\nParagraph 2.\n\n"
    
    # Edit: Delete the first paragraph AND its spacing
    edit = DocumentEdit(
        operation=EditOperationType.DELETION,
        target_text="Paragraph 1.\n\n", 
        new_text=None
    )
    
    stream.seek(0)
    engine = RedlineEngine(stream)
    # The fix ensures this is NOT skipped
    applied, skipped = engine.apply_edits([edit])
    
    assert applied == 1, "Should apply deletion even if target includes newlines"
    
    result_stream = engine.save_to_stream()
    doc = Document(result_stream)
    xml = doc.element.xml
    
    # Verify text is deleted
    assert "<w:delText>Paragraph 1.</w:delText>" in xml
    # The paragraph itself (w:p) still exists, but text is struck. 
    # This resolves the "Target Not Found" error.