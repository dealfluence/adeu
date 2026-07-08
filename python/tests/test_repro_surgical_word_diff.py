import io
from docx import Document
from adeu.redline.engine import RedlineEngine
from adeu.models import ModifyText
from adeu.ingest import extract_text_from_stream

def test_surgical_interior_word_diff():
    doc = Document()
    doc.add_paragraph("The quick brown fox jumped.")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    
    engine = RedlineEngine(stream, author="Test AI")
    engine.process_batch([
        ModifyText(
            type="modify",
            target_text="The quick brown fox jumped.",
            new_text="The slow brown fox leapt."
        )
    ])
    
    result_text = extract_text_from_stream(engine.save_to_stream(), clean_view=False)
    
    # Assertions:
    # 1. "brown fox" should NOT be inside a deletion or insertion tag.
    assert "{--The quick brown fox jumped.--}" not in result_text
    # 2. It should surgically strike "quick" and "jumped"
    assert "{--quick--}{++slow++}" in result_text
    assert " brown fox " in result_text
    assert "{--jumped--}{++leapt++}" in result_text
