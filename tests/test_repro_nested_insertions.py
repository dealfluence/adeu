# FILE: tests/test_repro_nested_insertions.py

import io

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def test_repro_nested_insertions_visibility_and_editing():
    """
    Validates Issue 4 Fix: Text inside nested insertions must be visible
    to the ingest system and editable via the mapper.

    Simulates XML from Round 3 of a negotiation where Round 2 inserted text
    inside a Round 1 insertion:
    <w:ins w:id="1">
       <w:r><w:t>Confidential </w:t></w:r>
       <w:ins w:id="2">
           <w:r><w:t>and Proprietary </w:t></w:r>
       </w:ins>
       <w:r><w:t>Information</w:t></w:r>
    </w:ins>
    """
    doc = Document()
    p = doc.add_paragraph()

    # Base text (Not tracked)
    r_base = OxmlElement("w:r")
    t_base = OxmlElement("w:t")
    t_base.text = "This is "
    r_base.append(t_base)
    p._element.append(r_base)

    # Outer Insertion (Round 1)
    ins_outer = OxmlElement("w:ins")
    ins_outer.set(qn("w:id"), "1")
    ins_outer.set(qn("w:author"), "Alice")

    r_outer_1 = OxmlElement("w:r")
    t_outer_1 = OxmlElement("w:t")
    t_outer_1.text = "Confidential "
    r_outer_1.append(t_outer_1)
    ins_outer.append(r_outer_1)

    # Inner Insertion (Round 2)
    ins_inner = OxmlElement("w:ins")
    ins_inner.set(qn("w:id"), "2")
    ins_inner.set(qn("w:author"), "Bob")

    r_inner = OxmlElement("w:r")
    t_inner = OxmlElement("w:t")
    t_inner.text = "and Proprietary "
    r_inner.append(t_inner)
    ins_inner.append(r_inner)

    # Append inner ins to outer ins
    ins_outer.append(ins_inner)

    r_outer_2 = OxmlElement("w:r")
    t_outer_2 = OxmlElement("w:t")
    t_outer_2.text = "Information"
    r_outer_2.append(t_outer_2)
    ins_outer.append(r_outer_2)

    # Append outer ins to paragraph
    p._element.append(ins_outer)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 1. TEST INGEST VISIBILITY
    text = extract_text_from_stream(stream)

    # Before the fix, "and Proprietary " would be completely missing.
    assert "and Proprietary" in text, "Nested insertion text was completely swallowed!"

    # Because of the wrapper tracking, it should render nested tags correctly
    # or at minimum render the text.
    # Example expected: This is {++Confidential {++and Proprietary ++}Information++}
    assert "Confidential" in text
    assert "Information" in text

    # 2. TEST EDITABILITY (MAPPER VISIBILITY)
    # Target the text that was inside the nested insertion
    edit = ModifyText(target_text="and Proprietary ", new_text="and Secret ")

    stream.seek(0)
    engine = RedlineEngine(stream, author="Charlie")
    applied, skipped = engine.apply_edits([edit])

    # Before the fix, the mapper would return -1 (Not Found) and skip the edit
    assert applied == 1, f"Edit failed! Engine could not map to the nested text. Applied: {applied}, Skipped: {skipped}"
    assert skipped == 0

    # Verify the final document actually contains the replacement
    final_stream = engine.save_to_stream()
    final_text = extract_text_from_stream(final_stream, clean_view=True)

    assert "and Secret " in final_text, "Replacement text was not applied to the nested structure."
    assert "and Proprietary " not in final_text, "Old nested text was not deleted."
