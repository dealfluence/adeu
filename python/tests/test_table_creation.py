import io

from docx import Document
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def test_markdown_pipe_table_in_new_text_creates_valid_w_tbl():
    doc = Document()
    doc.add_paragraph("INDENTURE")
    doc.add_paragraph("Section 1.01. Definitions.")
    buf = io.BytesIO()
    doc.save(buf)
    orig_bytes = buf.getvalue()

    table_md = (
        "## Executive Summary\n\n"
        "| Issue / Section | Draft Provision | Proposed Position | Priority |\n"
        "|---|---|---|---|\n"
        "| Section 4.01 | 30-day grace | **15-day grace** | High |\n"
        "| Section 4.07 | 2.0x ratio | **3.5x ratio** | Medium |\n\n"
        "INDENTURE"
    )

    eng = RedlineEngine(io.BytesIO(orig_bytes), author="Counsel")
    applied, skipped = eng.apply_edits(
        [ModifyText(type="modify", target_text="INDENTURE", new_text=table_md, comment=None)]
    )
    assert applied == 1 and skipped == 0

    out_bytes = eng.save_to_stream().getvalue()
    reloaded = Document(io.BytesIO(out_bytes))
    tables = reloaded.element.body.findall(qn("w:tbl"))
    assert len(tables) == 1

    # Verify table has 3 rows (1 header + 2 data) and 4 columns
    rows = tables[0].findall(qn("w:tr"))
    assert len(rows) == 3
    for r in rows:
        assert len(r.findall(qn("w:tc"))) == 4

    # Verify all <w:shd> elements carry required w:val="clear"
    for shd in tables[0].findall(f".//{qn('w:shd')}"):
        assert shd.get(qn("w:val")) == "clear"

    clean_txt = extract_text_from_stream(io.BytesIO(out_bytes), clean_view=True, include_appendix=False)
    assert "**Issue / Section** | **Draft Provision** | **Proposed Position** | **Priority**" in clean_txt
    assert "Section 4.01 | 30-day grace | **15-day grace** | High" in clean_txt
