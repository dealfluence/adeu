import os
import io
import pytest
from docx import Document
from adeu.redline.engine import RedlineEngine
from adeu.models import ReviewAction

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
# We use golden.docx because debug output confirmed it has the versioned 
# relationships (2016/09) that trigger the invisible comments bug.
TEST_DOC = os.path.join(FIXTURES_DIR, "golden.docx")

@pytest.mark.skipif(not os.path.exists(TEST_DOC), reason="Golden fixture not found")
def test_no_duplicate_parts_on_reply():
    """
    Loads a real Word-generated document (golden.docx) containing Modern Comments.
    Adds a reply using Adeu.
    Verifies that Adeu reuses the existing XML parts (by Content Type) 
    instead of creating duplicate parts (e.g. commentsIds1.xml) due to 
    Relationship Type mismatch.
    """
    with open(TEST_DOC, "rb") as f:
        stream = io.BytesIO(f.read())
        
    engine = RedlineEngine(stream, author="Adeu Verifier")
    
    # 1. Identify an existing comment to reply to
    comments = engine.comments_manager.extract_comments_data()
    assert len(comments) > 0, "Fixture must have comments to test reuse logic"
    root_id = list(comments.keys())[0]
    
    print(f"Replying to comment ID: {root_id}")
    
    # 2. Apply Reply
    action = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Verification Reply")
    applied, skipped = engine.apply_review_actions([action])
    assert applied == 1
    
    result_stream = engine.save_to_stream()
    
    # 3. Verify Structure (The Fix)
    doc_result = Document(result_stream)
    part_names = [p.partname for p in doc_result.part.package.parts]
    
    print("\nResulting Parts:", part_names)
    
    # FAIL CONDITION: If we see commentsIds1.xml, the bug is present.
    ids_parts = [n for n in part_names if "commentsIds" in n]
    assert len(ids_parts) == 1, f"Bug Detected! Duplicate commentsIds parts found: {ids_parts}"
    
    extended_parts = [n for n in part_names if "commentsExtended" in n]
    assert len(extended_parts) == 1, f"Bug Detected! Duplicate commentsExtended parts found: {extended_parts}"

    # Verify our reply is actually IN the file
    comments_xml = engine.comments_manager.comments_part.blob.decode("utf-8")
    assert "Verification Reply" in comments_xml
    
    # Verify Threading (Legacy w15:p) exists
    assert f'w15:p="{root_id}"' in comments_xml, "Reply should have legacy parent link"