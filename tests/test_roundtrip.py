import io
from docx import Document
from adeu.models import ComplianceEdit, EditOperationType
from adeu.redline.engine import RedlineEngine
from adeu.ingest import extract_text_from_stream

def test_full_roundtrip_workflow(simple_docx_stream):
    """
    Tests the full lifecycle:
    1. Ingest DOCX -> Text
    2. Simulate LLM creating an Edit
    3. Apply Edit -> Redlined DOCX
    """
    # 1. Ingestion
    extracted_text = extract_text_from_stream(simple_docx_stream)
    assert "Contract Agreement" in extracted_text
    assert "Seller" in extracted_text

    # 2. Simulate LLM Response (ComplianceEdit)
    # Let's change "Seller" to "Vendor"
    edit = ComplianceEdit(
        operation=EditOperationType.MODIFICATION,
        target_text_to_change_or_anchor="Seller",
        proposed_new_text="Vendor",
        thought_process="Standardizing terminology."
    )

    # 3. Injection (Redlining)
    # Reset stream pointer for the engine (simulating fresh read)
    simple_docx_stream.seek(0)
    engine = RedlineEngine(simple_docx_stream)
    
    engine.apply_edits([edit])
    
    result_stream = engine.save_to_stream()
    
    # 4. Verification
    # Parse the result and check for Track Changes XML
    doc = Document(result_stream)
    
    # Check text content (python-docx .text property usually shows the "current" view, 
    # but exact behavior depends on how it parses w:ins/w:del. 
    # We check the XML directly for the most robust test.)
    
    xml_content = doc.element.xml
    
    # Check for Deletion tag
    assert "w:del" in xml_content
    assert "<w:delText>Seller</w:delText>" in xml_content
    
    # Check for Insertion tag
    assert "w:ins" in xml_content
    assert "<w:t>Vendor</w:t>" in xml_content

def test_split_run_behavior():
    """
    Tests that the engine correctly splits a run when the target text 
    is in the middle of a sentence.
    """
    doc = Document()
    p = doc.add_paragraph()
    run = p.add_run("The quick brown fox.")
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    edit = ComplianceEdit(
        operation=EditOperationType.DELETION,
        target_text_to_change_or_anchor="brown", # Middle of the run
        proposed_new_text=None
    )
    
    engine = RedlineEngine(stream)
    engine.apply_edits([edit])
    
    result_stream = engine.save_to_stream()
    
    # Check XML
    doc = Document(result_stream)
    xml_content = doc.element.xml
    
    # "brown" should be wrapped in delete
    # "The quick " and " fox." should remain as runs (or be split out)
    assert "<w:delText>brown</w:delText>" in xml_content