"""Insertions next to a styled run, from a PLAIN target fuzzy-matched onto styled text.

dmp pairs the inserted words with the adjacent `**` marker, producing a hunk whose target is only a (zero-width,
virtual) style marker: `"**"` -> `", as supplemented ..."`. Node failed the whole batch on it; Python reported the edit
applied and silently dropped the inserted text. Twin: node/packages/core/src/marker_boundary_insert.test.ts.
"""

from io import BytesIO

from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def _doc(before: str, bold: str, after: str) -> bytes:
    d = Document()
    p = d.add_paragraph(before)
    p.add_run(bold).bold = True
    p.add_run(after)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _apply(src: bytes, target: str, new: str) -> "tuple[dict, bytes]":
    eng = RedlineEngine(BytesIO(src), author="Reviewer")
    stats = eng.process_batch([ModifyText(target_text=target, new_text=new)])
    return stats, eng.save_to_stream().getvalue()


def _clean(b: bytes) -> str:
    return extract_text_from_stream(BytesIO(b), clean_view=True)


def _plain(b: bytes) -> str:
    # Formatting of the inserted words follows the engine's existing style-source rules; these tests pin that the
    # words are written at the right place, not which run they inherit from.
    return _clean(b).replace("**", "")


def _rejected(b: bytes) -> str:
    eng = RedlineEngine(BytesIO(b))
    eng.reject_all_revisions()
    return _clean(eng.save_to_stream().getvalue())


def test_append_after_bold_run_before_punctuation_inserts_the_clause():
    src = _doc("The amount shall be determined in accordance with ", "Section 6(e) of the Agreement", ".")
    stats, out = _apply(
        src,
        "in accordance with Section 6(e) of the Agreement.",
        "in accordance with Section 6(e) of the Agreement, as supplemented by the provisions of this Section 5.1.",
    )
    assert (stats["edits_applied"], stats["edits_skipped"]) == (1, 0)
    assert _plain(out) == (
        "The amount shall be determined in accordance with "
        "Section 6(e) of the Agreement, as supplemented by the provisions of this Section 5.1."
    )
    assert _rejected(out) == _clean(src)


def test_insert_before_bold_run_is_written():
    src = _doc("Invoices are due ", "Net 90", " days after receipt.")
    stats, out = _apply(src, "due Net 90 days", "due within Net 90 days")
    assert (stats["edits_applied"], stats["edits_skipped"]) == (1, 0)
    assert _plain(out) == "Invoices are due within Net 90 days after receipt."
    assert _rejected(out) == _clean(src)


def test_failed_sub_edit_is_reported_under_the_callers_target(monkeypatch):
    src = _doc("The amount shall be determined in accordance with ", "Section 6(e) of the Agreement", ".")
    eng = RedlineEngine(BytesIO(src), author="Reviewer")
    # Fault injection: any apply-stage failure of a resolved sub-edit.
    monkeypatch.setattr(RedlineEngine, "_apply_single_edit_indexed", lambda self, *a, **k: False)
    target = "in accordance with Section 6(e) of the Agreement."
    applied, skipped = eng.apply_edits([ModifyText(target_text=target, new_text=target[:-1] + ", as amended.")])
    assert skipped == 1
    assert any(target[:40] in d for d in eng.skipped_details), eng.skipped_details
    assert not any("'**" in d for d in eng.skipped_details), eng.skipped_details
