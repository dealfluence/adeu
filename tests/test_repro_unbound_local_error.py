import io
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine


def test_repro_unbound_local_curr_ins_id():
    """
    Scenario: A run exists that produces no text (empty string).
    In mapper.py, the `if full_seg_text:` block is skipped, leaving `curr_ins_id` undefined.
    Usage of `is_redline = bool(curr_ins_id) ...` subsequently raises UnboundLocalError.
    """
    doc = Document()
    p = doc.add_paragraph()

    # 1. Normal Run
    p.add_run("Start")

    # 2. Empty Run (No text, maybe just rPr)
    # add_run() creates a w:r element. Without adding text, it's effectively empty.
    p.add_run()

    # 3. Another Normal Run
    p.add_run("End")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # Apply any edit to trigger mapper build
    engine = RedlineEngine(stream)
    edit = DocumentEdit(target_text="Start", new_text="Changed")

    # This should not raise UnboundLocalError
    try:
        engine.apply_edits([edit])
    except UnboundLocalError as e:
        pytest.fail(f"UnboundLocalError raised: {e}")