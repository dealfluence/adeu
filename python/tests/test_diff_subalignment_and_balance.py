import io

from docx import Document

from adeu.diff import generate_structured_edits
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.redline.engine import RedlineEngine


def test_structured_diff_self_contained_uniqueness_and_balanced_markdown():
    doc1 = Document()
    doc1.add_paragraph(
        "The initial ratings are BBB/Baa2 below the current ratings "
        "assigned by each of the Rating Agencies, respectively."
    )
    p1 = doc1.add_paragraph("(a) ")
    r1 = p1.add_run("BBB−")
    r1.bold = True
    p1.add_run(" (or its equivalent) by Agency One;")

    p2 = doc1.add_paragraph("(c) ")
    r2 = p2.add_run("BBB−")
    r2.bold = True
    p2.add_run(" (or its equivalent) by Agency Two.")

    p3 = doc1.add_paragraph("Accordingly, the trigger thresholds of BBB−/Baa3 are ")
    r3 = p3.add_run("one notch")
    r3.bold = True
    p3.add_run(" below the current ratings assigned by each of the Rating Agencies, respectively.")

    doc2 = Document()
    doc2.add_paragraph(
        "The initial ratings are BBB/Baa2 below the current ratings "
        "assigned by each of the Rating Agencies, respectively."
    )
    q1 = doc2.add_paragraph("(a) ")
    qr1 = q1.add_run("BB")
    qr1.bold = True
    q1.add_run(" (or its equivalent) by Agency One;")

    q2 = doc2.add_paragraph("(c) ")
    qr2 = q2.add_run("BB")
    qr2.bold = True
    q2.add_run(" (or its equivalent) by Agency Two.")

    q3 = doc2.add_paragraph("Accordingly, the trigger thresholds of BB/Ba2 are ")
    qr3 = q3.add_run("three notches")
    qr3.bold = True
    q3.add_run(
        " below the current ratings assigned by each of the Rating Agencies, respectively "
        "(providing a meaningful cushion before termination)."
    )

    b1, b2 = io.BytesIO(), io.BytesIO()
    doc1.save(b1)
    doc2.save(b2)
    v1, v2 = b1.getvalue(), b2.getvalue()

    t1, s1 = _extract_text_from_doc(
        Document(io.BytesIO(v1)), clean_view=True, include_appendix=False, return_structure=True
    )
    t2, s2 = _extract_text_from_doc(
        Document(io.BytesIO(v2)), clean_view=True, include_appendix=False, return_structure=True
    )
    assert isinstance(t1, str) and isinstance(t2, str)
    assert s1 is not None and not isinstance(s1, dict)
    assert s2 is not None and not isinstance(s2, dict)
    edits, _ = generate_structured_edits(t1, s1, t2, s2)
    for e in edits:
        e._match_start_index = None

    eng = RedlineEngine(io.BytesIO(v1), author="Reviewer")
    applied, skipped = eng.apply_edits(edits)  # type: ignore[arg-type]
    assert skipped == 0
    assert applied == len(edits)

    out = eng.save_to_stream().getvalue()
    clean_out = extract_text_from_stream(io.BytesIO(out), clean_view=True, include_appendix=False)
    clean_v2 = extract_text_from_stream(io.BytesIO(v2), clean_view=True, include_appendix=False)
    assert clean_out == clean_v2


def test_unequal_replace_block_subalignment_preserves_rejected_paragraphs_and_defined_terms():
    doc1 = Document()
    doc1.add_paragraph("(c) changes to the methodology for compounding of the Adjusted Reference Rate;")
    doc1.add_paragraph("(d) changes to the spread adjustment methodology, including rounding conventions; and")
    doc1.add_paragraph(
        "(e) any other changes that the Calculation Agent determines are consistent with market practice."
    )

    doc2 = Document()
    doc2.add_paragraph("(c) changes to the methodology for compounding of the applicable fallback rate; and")
    p_mod = doc2.add_paragraph("(d) any other changes that the ")
    r_def = p_mod.add_run('"Co-Calculation Agent"')
    r_def.bold = True
    p_mod.add_run(" determines are consistent with market practice.")

    b1, b2 = io.BytesIO(), io.BytesIO()
    doc1.save(b1)
    doc2.save(b2)
    v1, v2 = b1.getvalue(), b2.getvalue()

    t1, s1 = _extract_text_from_doc(
        Document(io.BytesIO(v1)), clean_view=True, include_appendix=False, return_structure=True
    )
    t2, s2 = _extract_text_from_doc(
        Document(io.BytesIO(v2)), clean_view=True, include_appendix=False, return_structure=True
    )
    edits, _ = generate_structured_edits(t1, s1, t2, s2)

    eng = RedlineEngine(io.BytesIO(v1), author="Reviewer")
    applied, skipped = eng.apply_edits(edits)
    assert skipped == 0 and applied > 0
    out = eng.save_to_stream().getvalue()

    # Clean view (with appendix) must match v2 (including the defined term inside <w:ins>)
    assert extract_text_from_stream(io.BytesIO(out), clean_view=True) == extract_text_from_stream(
        io.BytesIO(v2), clean_view=True
    )

    # Rejecting all revisions must restore all 3 separate original paragraphs
    eng_rej = RedlineEngine(io.BytesIO(out))
    eng_rej.reject_all_revisions()
    rej = eng_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(rej), clean_view=True) == extract_text_from_stream(
        io.BytesIO(v1), clean_view=True
    )
