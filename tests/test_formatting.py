import io
import pytest
from docx import Document
from adeu.models import DocumentEdit, EditOperationType
from adeu.redline.engine import RedlineEngine

def _is_element_bold(run_element) -> bool:
    """
    Checks if an lxml Run element is effectively bold.
    """
    # python-docx handles 'w' namespace automatically in xpath
    # Find <w:b> tag under <w:rPr>
    b_tags = run_element.xpath('./w:rPr/w:b')
    
    if not b_tags:
        return False
        
    # Check val attribute (default is true if attribute missing)
    # We must access the attribute using the fully qualified name or the namespace map
    # b_tags[0] is an lxml element.
    # The 'val' attribute in Word is w:val.
    
    # We can try to get it via the qname:
    val = b_tags[0].get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val')
    
    if val is None:
        return True
        
    if val.lower() in ('0', 'false', 'off'):
        return False
        
    return True

def test_insertion_inherits_next_run_style_heuristic():
    """
    Scenario: "Start [Bold]Important"
    Insert "Very " -> "Start [Bold]Very Important"
    """
    doc = Document()
    p = doc.add_paragraph()
    r1 = p.add_run("Start ")
    r1.bold = False
    r2 = p.add_run("Important")
    r2.bold = True
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    edit = DocumentEdit(
        operation=EditOperationType.INSERTION,
        target_text="", 
        new_text="Very ",
    )
    edit._match_start_index=6
    
    engine = RedlineEngine(stream)
    engine.apply_edits([edit])
    
    result_stream = engine.save_to_stream()
    doc = Document(result_stream)
    
    # Use XPath to find the run containing "Very "
    # Note: We use contains() because w:t might have xml:space="preserve"
    runs = doc.element.xpath('//w:r[w:t[contains(text(), "Very ")]]')
    assert len(runs) >= 1, "Inserted text 'Very ' not found in any run"
    
    target_run = runs[0]
    
    assert _is_element_bold(target_run), f"Inserted text should inherit Bold. XML: {target_run.xml}"

def test_insertion_defaults_to_prev_run_style_if_no_space():
    """
    Scenario: "Hello [Bold]World"
    Insert "Big" (no space) -> "HelloBig [Bold]World"
    Should stay Normal (inherit from "Hello ").
    """
    doc = Document()
    p = doc.add_paragraph()
    r1 = p.add_run("Hello ")
    r1.bold = False
    r2 = p.add_run("World")
    r2.bold = True
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    edit = DocumentEdit(
        operation=EditOperationType.INSERTION,
        target_text="",
        new_text="Big",
    )
    edit._match_start_index = 6
    
    engine = RedlineEngine(stream)
    engine.apply_edits([edit])
    
    result_stream = engine.save_to_stream()
    doc = Document(result_stream)
    
    runs = doc.element.xpath('//w:r[w:t[contains(text(), "Big")]]')
    assert len(runs) >= 1, "Inserted text 'Big' not found"
    target_run = runs[0]
    
    assert not _is_element_bold(target_run), f"Inserted text 'Big' should NOT be bold. XML: {target_run.xml}"