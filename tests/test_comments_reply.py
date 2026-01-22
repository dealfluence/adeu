import io
import re
from docx import Document
from adeu.redline.engine import RedlineEngine
from adeu.models import DocumentEdit, ReviewAction
from adeu.ingest import extract_text_from_stream

def test_reply_creates_new_comment_entry():
    """
    Verifies that replying to a comment creates a separate comment entry
    targeting the same text range, rather than appending text to the old comment.
    """
    doc = Document()
    doc.add_paragraph("Text with comment.")
    
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    # 1. Create initial comment via modification
    # We change "Text" to "TextModified" to ensure the engine processes it and attaches the comment.
    engine = RedlineEngine(stream, author="Author1")
    edit = DocumentEdit(target_text="Text", new_text="TextModified", comment="Initial Comment")
    engine.apply_edits([edit])
    
    stream_mid = engine.save_to_stream()
    
    # Verify initial state
    text_mid = extract_text_from_stream(stream_mid)
    
    # Extract Comment ID. Expect [Com:1] or similar.
    match = re.search(r"\[Com:(\d+)\]", text_mid)
    assert match, f"Initial comment not found in text: {text_mid}"
    com_id = match.group(1)
    
    # 2. Reply to the comment
    engine2 = RedlineEngine(stream_mid, author="Author2")
    action = ReviewAction(action="REPLY", target_id=f"Com:{com_id}", text="This is a reply.")
    
    applied, skipped = engine2.apply_review_actions([action])
    
    assert applied == 1, "Reply action should be applied"
    assert skipped == 0
    
    stream_final = engine2.save_to_stream()
    text_final = extract_text_from_stream(stream_final)
    
    print(f"Final Text:\n{text_final}")
    
    # Expectation: TWO distinct comment IDs in the output
    # We look for [Com:X] patterns.
    com_ids = re.findall(r"\[Com:(\d+)\]", text_final)
    unique_ids = set(com_ids)
    
    assert len(unique_ids) == 2, f"Should have 2 distinct comments, found: {unique_ids}\nText: {text_final}"
    
    # Verify content
    assert "Initial Comment" in text_final
    assert "This is a reply." in text_final
    assert "Author1" in text_final
    assert "Author2" in text_final