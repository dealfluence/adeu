import io

from docx import Document
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText, RejectChange
from adeu.redline.engine import RedlineEngine

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_ooxml_original_paragraphs(docx_bytes: bytes) -> list[str]:
    """Read original-view paragraphs using standard OOXML pilcrow w:ins semantics."""
    doc = Document(io.BytesIO(docx_bytes))
    out: list[str] = []
    carry = ""
    for p in doc.element.body.iter(f"{W}p"):
        chunks: list[str] = []

        def walk(node, acc=chunks):
            tag = node.tag.split("}")[-1] if isinstance(node.tag, str) else ""
            if tag in ("ins", "pPr"):
                return
            if tag in ("t", "delText"):
                if node.text:
                    acc.append(node.text)
                return
            for child in node:
                walk(child, acc)

        walk(p)
        pPr = p.find(f"{W}pPr")
        rPr = pPr.find(f"{W}rPr") if pPr is not None else None
        pilcrow_ins = rPr is not None and rPr.find(f"{W}ins") is not None
        carry += "".join(chunks)
        if not pilcrow_ins:
            if carry.strip():
                out.append(carry.strip())
            carry = ""
    if carry.strip():
        out.append(carry.strip())
    return out


def test_prefix_multiline_split_preserves_original_paragraph_on_reject():
    doc = Document()
    doc.add_paragraph(
        'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"), '
        "acting in its sole capacity as Calculation Agent, "
        "as designated under Section 6.8 of the Schedule to the Agreement."
    )
    doc.add_paragraph("The Calculation Agent shall determine all rates and amounts.")
    buf = io.BytesIO()
    doc.save(buf)
    orig_bytes = buf.getvalue()

    edit = ModifyText(
        type="modify",
        target_text=(
            'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"), '
            "acting in its sole capacity as Calculation Agent,"
        ),
        new_text=(
            "_Drafting note: See rider._\n\n"
            "**2.1 Calculation Agent [Alternative A]**\n\n"
            'The Calculation Agent for purposes of this Transaction shall be National Bank ("Party B"),'
        ),
        comment=None,
    )

    eng = RedlineEngine(io.BytesIO(orig_bytes), author="Reviewer")
    applied, skipped = eng.apply_edits([edit])
    assert applied == 1 and skipped == 0
    redlined_bytes = eng.save_to_stream().getvalue()

    # 1. Standard OOXML Original-view reader must see the exact 2 original paragraphs
    assert _extract_ooxml_original_paragraphs(redlined_bytes) == _extract_ooxml_original_paragraphs(orig_bytes)

    # 2. reject_all_revisions() must restore the exact original clean text
    eng_rej = RedlineEngine(io.BytesIO(redlined_bytes))
    eng_rej.reject_all_revisions()
    rej_bytes = eng_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(rej_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(orig_bytes), clean_view=True
    )

    # 3. Targeted RejectChange on Chg:1 must also restore the exact original clean text
    eng_single_rej = RedlineEngine(io.BytesIO(redlined_bytes))
    eng_single_rej.apply_review_actions([RejectChange(type="reject", target_id="Chg:1", part=None, comment=None)])
    single_rej_bytes = eng_single_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(single_rej_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(orig_bytes), clean_view=True
    )


def test_full_paragraph_multiline_replacement_does_not_stamp_bogus_del_pilcrow():
    doc = Document()
    doc.add_paragraph("Original single paragraph that will be replaced by three paragraphs.")
    doc.add_paragraph("Following untouched paragraph.")
    buf = io.BytesIO()
    doc.save(buf)
    orig_bytes = buf.getvalue()

    edit = ModifyText(
        type="modify",
        target_text="Original single paragraph that will be replaced by three paragraphs.",
        new_text="First replacement paragraph.\n\nSecond replacement paragraph.\n\nThird replacement paragraph.",
        comment=None,
    )
    eng = RedlineEngine(io.BytesIO(orig_bytes), author="Reviewer")
    applied, skipped = eng.apply_edits([edit])
    assert applied == 1 and skipped == 0

    # Source paragraph (index 0) now contains inline <w:ins> for "First replacement paragraph."
    # so its paragraph mark must NOT be marked <w:del/>.
    p0 = eng.doc.element.body.findall(qn("w:p"))[0]
    pPr = p0.find(qn("w:pPr"))
    rPr = pPr.find(qn("w:rPr")) if pPr is not None else None
    assert rPr is None or rPr.find(qn("w:del")) is None

    redlined_bytes = eng.save_to_stream().getvalue()
    assert _extract_ooxml_original_paragraphs(redlined_bytes) == _extract_ooxml_original_paragraphs(orig_bytes)

    eng_rej = RedlineEngine(io.BytesIO(redlined_bytes))
    eng_rej.reject_all_revisions()
    rej_bytes = eng_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(rej_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(orig_bytes), clean_view=True
    )
