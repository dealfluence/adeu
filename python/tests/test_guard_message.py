import io

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.models import ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine


def _create_doc_with_foreign_ins(author: str = "Supplier's Counsel", ins_id: str = "201") -> io.BytesIO:
    doc = Document()
    p = doc.add_paragraph("The party shall provide ")
    p_el = p._element

    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), ins_id)
    ins.set(qn("w:author"), author)
    ins.set(qn("w:date"), "2026-06-30T08:00:00Z")

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "written notice"
    r.append(t)
    ins.append(r)
    p_el.append(ins)

    r2 = OxmlElement("w:r")
    t2 = OxmlElement("w:t")
    t2.text = " within 30 days."
    r2.append(t2)
    p_el.append(r2)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def test_guard_names_the_accept_action():
    stream = _create_doc_with_foreign_ins(author="Supplier's Counsel", ins_id="201")
    engine = RedlineEngine(stream, author="Reviewer AI")
    edit = ModifyText(target_text="written notice", new_text="email notification", match_mode="all")

    with pytest.raises(BatchValidationError) as exc_info:
        engine.process_batch([edit])

    msg = exc_info.value.errors[0]
    assert '{"type": "accept", "target_id": "Chg:201"}' in msg


def test_guard_names_the_narrowing_alternative():
    stream = _create_doc_with_foreign_ins(author="Supplier's Counsel", ins_id="201")
    engine = RedlineEngine(stream, author="Reviewer AI")
    edit = ModifyText(target_text="written notice", new_text="email notification", match_mode="all")

    with pytest.raises(BatchValidationError) as exc_info:
        engine.process_batch([edit])

    msg = exc_info.value.errors[0]
    assert "match_mode" in msg
    assert "strict" in msg
    assert "first" in msg


def test_guard_still_names_author_and_ids():
    stream = _create_doc_with_foreign_ins(author="Supplier's Counsel", ins_id="201")
    engine = RedlineEngine(stream, author="Reviewer AI")
    edit = ModifyText(target_text="written notice", new_text="email notification", match_mode="all")

    with pytest.raises(BatchValidationError) as exc_info:
        engine.process_batch([edit])

    msg = exc_info.value.errors[0]
    assert "Supplier's Counsel" in msg
    assert "Chg:201" in msg


def test_guard_message_token_budget():
    stream = _create_doc_with_foreign_ins(author="Supplier's Counsel", ins_id="201")
    engine = RedlineEngine(stream, author="Reviewer AI")
    edit = ModifyText(target_text="written notice", new_text="email notification", match_mode="all")

    with pytest.raises(BatchValidationError) as exc_info:
        engine.process_batch([edit])

    msg = exc_info.value.errors[0]
    approx_tokens = len(msg) // 4
    assert approx_tokens <= 70


def test_strict_edit_inside_foreign_insertion_still_allowed():
    stream = _create_doc_with_foreign_ins(author="Supplier's Counsel", ins_id="201")
    engine = RedlineEngine(stream, author="Reviewer AI")
    edit = ModifyText(target_text="written notice", new_text="email notification", match_mode="strict")

    res = engine.process_batch([edit])
    assert res["edits_applied"] == 1
