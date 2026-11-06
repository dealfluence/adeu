import io
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine
from tests.utils import run_cli


def test_manual_markup_mode_renders_strike_and_bold_underline_red_without_track_changes():
    doc = Document()
    doc.add_paragraph("The Notes will mature on December 15, 2030 and bear interest at 6.50% per annum.")
    buf = io.BytesIO()
    doc.save(buf)

    eng = RedlineEngine(io.BytesIO(buf.getvalue()), author="Counsel", markup_mode="manual")
    applied, skipped = eng.apply_edits(
        [
            ModifyText(
                type="modify",
                target_text="December 15, 2030",
                new_text="June 15, 2032",
                comment="Extended maturity date per term sheet.",
            )
        ]
    )
    assert applied == 1 and skipped == 0
    saved = eng.save_to_stream().getvalue()

    reloaded = Document(io.BytesIO(saved))
    body = reloaded.element.body

    # 1. Zero <w:ins> or <w:del> elements in the document
    assert len(body.findall(f".//{qn('w:ins')}")) == 0
    assert len(body.findall(f".//{qn('w:del')}")) == 0
    assert len(body.findall(f".//{qn('w:delText')}")) == 0

    # 2. Both the original struck-through phrase and the new inserted phrase exist in <w:t> runs
    runs = body.findall(f".//{qn('w:r')}")
    struck_texts = []
    inserted_texts = []
    for r in runs:
        t_el = r.find(qn("w:t"))
        if t_el is None or not t_el.text:
            continue
        rPr = r.find(qn("w:rPr"))
        if rPr is not None and rPr.find(qn("w:strike")) is not None:
            struck_texts.append(t_el.text)
        if (
            rPr is not None
            and rPr.find(qn("w:b")) is not None
            and rPr.find(qn("w:u")) is not None
            and rPr.find(qn("w:color")) is not None
            and rPr.find(qn("w:color")).get(qn("w:val")) == "C00000"
        ):
            inserted_texts.append(t_el.text)

    assert "".join(struck_texts) == "December 15, 2030"
    assert "".join(inserted_texts) == "June 15, 2032"

    # 3. Native Word comment range is attached
    assert len(body.findall(f".//{qn('w:commentRangeStart')}")) == 1
    assert len(body.findall(f".//{qn('w:commentRangeEnd')}")) == 1


def test_cli_apply_with_manual_flag(tmp_path: Path):
    doc_path = tmp_path / "indenture.docx"
    edits_path = tmp_path / "edits.json"
    out_path = tmp_path / "indenture_manual.docx"

    doc = Document()
    doc.add_paragraph("Redemption price shall be 105% of principal amount.")
    doc.save(str(doc_path))

    edits_path.write_text(
        '[{"type": "modify", "target_text": "105%", "new_text": "102%", "comment": "Lowered call premium."}]',
        encoding="utf-8",
    )

    res = run_cli("apply", str(doc_path), str(edits_path), "--manual", "-o", str(out_path))
    assert res.returncode == 0
    assert out_path.exists()

    reloaded = Document(str(out_path))
    body = reloaded.element.body
    assert len(body.findall(f".//{qn('w:ins')}")) == 0
    assert len(body.findall(f".//{qn('w:del')}")) == 0
    full_t = "".join(t.text or "" for t in body.findall(f".//{qn('w:t')}"))
    assert "105%" in full_t and "102%" in full_t
