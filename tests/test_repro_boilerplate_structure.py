import io

from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import get_visible_runs


def test_ingest_detects_structural_info():
    """
    FIXED: The extracted text should now contain Markdown headers.
    """
    doc = Document()
    doc.add_heading("1. Assignment", level=1)
    doc.add_paragraph("This is the body text.")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    extracted = extract_text_from_stream(stream)

    # SUCCESS EXPECTATION: "# 1. Assignment"
    assert "# 1. Assignment" in extracted


def test_insert_boilerplate_creates_paragraphs():
    """
    FIXED: Inserting multi-paragraph text creates actual new paragraphs in DOCX.
    """
    doc = Document()
    doc.add_paragraph("Clause 1: Term.")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # Simulating LLM inserting a full new clause with structure
    boilerplate = "\n\nClause 2: Termination.\nEither party may terminate this agreement."

    edit = DocumentEdit(target_text="Clause 1: Term.", new_text="Clause 1: Term." + boilerplate)

    engine = RedlineEngine(stream)
    engine.apply_edits([edit])

    result_stream = engine.save_to_stream()
    doc_result = Document(result_stream)

    # SUCCESS EXPECTATION:
    # Original: 1 paragraph
    # Inserted: 2 new paragraphs (Clause 2, Either party)
    # Total: 3 paragraphs
    assert len(doc_result.paragraphs) == 3

    # Check text content of new paragraphs
    # Note: docx.Paragraph.text does not see tracked changes (w:ins). We must use our helper.
    p1_text = "".join(r.text for r in get_visible_runs(doc_result.paragraphs[1]))
    p2_text = "".join(r.text for r in get_visible_runs(doc_result.paragraphs[2]))

    assert p1_text == "Clause 2: Termination."
    assert p2_text == "Either party may terminate this agreement."
