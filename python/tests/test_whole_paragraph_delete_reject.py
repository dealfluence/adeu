import io

from docx import Document
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_ooxml_original_paragraphs(docx_bytes: bytes) -> list[str]:
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


def test_consecutive_whole_paragraph_deletions_preserve_original_paragraphs():
    doc = Document()
    doc.add_paragraph("5.1 Close-out Amount")
    doc.add_paragraph("Upon the occurrence of an Early Termination Date, the amount shall be determined.")
    doc.add_paragraph("The Determining Party may consider the following sources:")
    doc.add_paragraph("Surviving paragraph at the end of the section.")
    buf = io.BytesIO()
    doc.save(buf)
    orig_bytes = buf.getvalue()

    edits = [
        ModifyText(type="modify", target_text="5.1 Close-out Amount\n\n", new_text="", comment=None),
        ModifyText(
            type="modify",
            target_text="Upon the occurrence of an Early Termination Date, the amount shall be determined.\n\n",
            new_text="",
            comment=None,
        ),
        ModifyText(
            type="modify",
            target_text="The Determining Party may consider the following sources:\n\n",
            new_text="",
            comment=None,
        ),
    ]

    eng = RedlineEngine(io.BytesIO(orig_bytes), author="Reviewer")
    applied, skipped = eng.apply_edits(edits)
    assert applied == 3 and skipped == 0

    # Surviving paragraph (the 4th paragraph) must NOT have a <w:del/> on its paragraph mark
    surviving_p = eng.doc.element.body.findall(qn("w:p"))[-1]
    pPr = surviving_p.find(qn("w:pPr"))
    rPr = pPr.find(qn("w:rPr")) if pPr is not None else None
    assert rPr is None or rPr.find(qn("w:del")) is None

    redlined_bytes = eng.save_to_stream().getvalue()

    # 1. Clean view shows only the surviving paragraph
    assert (
        extract_text_from_stream(io.BytesIO(redlined_bytes), clean_view=True, include_appendix=False).strip()
        == "Surviving paragraph at the end of the section."
    )

    # 2. Original-view reader sees all 4 separate paragraphs (not concatenated into 1)
    assert _extract_ooxml_original_paragraphs(redlined_bytes) == _extract_ooxml_original_paragraphs(orig_bytes)

    # 3. reject_all_revisions() restores all 4 separate paragraphs
    eng_rej = RedlineEngine(io.BytesIO(redlined_bytes))
    eng_rej.reject_all_revisions()
    rej_bytes = eng_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(rej_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(orig_bytes), clean_view=True
    )

    # 4. accept_all_revisions() leaves only the surviving paragraph
    eng_acc = RedlineEngine(io.BytesIO(redlined_bytes))
    eng_acc.accept_all_revisions()
    acc_bytes = eng_acc.save_to_stream().getvalue()
    assert (
        extract_text_from_stream(io.BytesIO(acc_bytes), clean_view=True, include_appendix=False).strip()
        == "Surviving paragraph at the end of the section."
    )
