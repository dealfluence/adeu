import io

from docx import Document
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def test_fuzzy_newline_in_unchanged_context_does_not_split_paragraph():
    doc = Document()
    doc.add_paragraph("Atlas Securities LLC 610 Lexington Avenue, 28th Floor New York, NY 10022")
    buf = io.BytesIO()
    doc.save(buf)
    orig_bytes = buf.getvalue()

    eng = RedlineEngine(io.BytesIO(orig_bytes), author="Counsel")
    applied, skipped = eng.apply_edits(
        [
            ModifyText(
                type="modify",
                target_text="Atlas Securities LLC\n\n610 Lexington Avenue, 28th Floor",
                new_text="Atlas Securities LLC\n\n600 Lexington Avenue, 28th Floor",
                comment=None,
            )
        ]
    )
    assert applied == 1 and skipped == 0

    # Must remain a single paragraph (not split into 2 <w:p> elements)
    ps = eng.doc.element.body.findall(qn("w:p"))
    assert len(ps) == 1

    out_bytes = eng.save_to_stream().getvalue()
    clean_txt = extract_text_from_stream(io.BytesIO(out_bytes), clean_view=True, include_appendix=False).strip()
    assert clean_txt == "Atlas Securities LLC 600 Lexington Avenue, 28th Floor New York, NY 10022"
