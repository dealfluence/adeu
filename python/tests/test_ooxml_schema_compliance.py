import io
import re
import zipfile

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from adeu.models import AcceptChange, ModifyText, RejectChange
from adeu.redline.engine import RedlineEngine


def test_save_to_stream_repairs_undeclared_mc_ignorable_prefixes():
    doc = Document()
    doc.add_paragraph("Indenture covenant text.")
    buf = io.BytesIO()
    doc.save(buf)

    # Simulate a document whose <w:document> declares mc:Ignorable="w14 wp14"
    # without xmlns:w14 or xmlns:wp14 on the root element.
    in_mem = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue()), "r") as zin, zipfile.ZipFile(in_mem, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                xml_txt = data.decode("utf-8")
                xml_txt = re.sub(
                    r"<w:document[^>]*>",
                    '<w:document xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/main" '
                    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                    'mc:Ignorable="w14 wp14">',
                    xml_txt,
                    count=1,
                )
                data = xml_txt.encode("utf-8")
            zout.writestr(item, data)

    eng = RedlineEngine(io.BytesIO(in_mem.getvalue()), author="Counsel")
    eng.apply_edits([ModifyText(type="modify", target_text="covenant", new_text="revised covenant", comment=None)])
    saved = eng.save_to_stream().getvalue()

    with zipfile.ZipFile(io.BytesIO(saved), "r") as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    m = re.search(r"<w:document[^>]*>", doc_xml)
    assert m is not None
    root_tag = m.group(0)
    assert 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"' in root_tag
    assert 'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"' in root_tag


_FOUR = ["Alpha clause one.", "Bravo clause two.", "Charlie clause three.", "Delta clause four."]
_MARK_EDITS = {
    "delete whole paragraph": ModifyText(target_text="Bravo clause two.\n\n", new_text=""),
    "split paragraph": ModifyText(target_text="Alpha clause one.", new_text="Alpha clause\n\none."),
    "insert paragraph": ModifyText(
        target_text="Charlie clause three.", new_text="Charlie clause three.\n\nNew paragraph."
    ),
    "merge paragraphs": ModifyText(
        target_text="Charlie clause three.\n\nDelta clause four.", new_text="Charlie clause three. Delta clause four."
    ),
}
_EMPTY_CONTAINER = re.compile(r"<w:pPr/>|<w:pPr></w:pPr>|<w:rPr/>|<w:rPr></w:rPr>")


@pytest.mark.parametrize("resolve", ["accept", "reject"])
@pytest.mark.parametrize("case", list(_MARK_EDITS))
def test_resolving_paragraph_mark_revisions_leaves_no_empty_property_containers(case, resolve):
    # Accepting or rejecting a tracked paragraph mark used to leave `<w:pPr><w:rPr/></w:pPr>` on the paragraph
    # (stetdoc: empty_properties). Node twin in ooxml_schema_compliance.test.ts.
    doc = Document()
    for t in _FOUR:
        doc.add_paragraph(t)
    buf = io.BytesIO()
    doc.save(buf)
    eng = RedlineEngine(io.BytesIO(buf.getvalue()), author="Counsel")
    eng.process_batch([_MARK_EDITS[case]])
    edited = eng.save_to_stream().getvalue()

    for how in ("all", "by id"):
        e2 = RedlineEngine(io.BytesIO(edited))
        if how == "all":
            (e2.accept_all_revisions if resolve == "accept" else e2.reject_all_revisions)()
        else:
            ids = sorted({el.get(qn("w:id")) for tag in ("w:ins", "w:del") for el in e2.doc.element.iter(qn(tag))})
            action = AcceptChange if resolve == "accept" else RejectChange
            e2.process_batch([action(target_id=f"Chg:{rid}") for rid in ids])
        with zipfile.ZipFile(e2.save_to_stream()) as z:
            body = z.read("word/document.xml").decode("utf-8")
        assert not _EMPTY_CONTAINER.findall(body), f"{case} / {resolve} {how}"


def test_unbold_replacement_prunes_empty_rpr_and_para_rpr_maintains_canonical_order():
    doc = Document()
    p = doc.add_paragraph()
    r = p.add_run("Bold Phrase")
    r.bold = True

    p2 = doc.add_paragraph("Paragraph to delete.")
    p2_pPr = p2._element.get_or_add_pPr()
    p2_pPr.append(parse_xml(f'<w:rPr {nsdecls("w")}><w:sz w:val="24"/></w:rPr>'))
    doc.add_paragraph("Surviving paragraph.")

    buf = io.BytesIO()
    doc.save(buf)

    eng = RedlineEngine(io.BytesIO(buf.getvalue()), author="Counsel")
    applied, skipped = eng.apply_edits(
        [
            ModifyText(type="modify", target_text="**Bold Phrase**", new_text="Plain Phrase", comment=None),
            ModifyText(type="modify", target_text="Paragraph to delete.\n\n", new_text="", comment=None),
        ]
    )
    assert applied == 2 and skipped == 0

    # 1. No empty <w:rPr/> elements anywhere in the document body
    for rpr in eng.doc.element.body.findall(f".//{qn('w:rPr')}"):
        assert len(rpr) > 0 or len(rpr.attrib) > 0, "Empty <w:rPr/> element should be pruned"

    # 2. In p2's <w:pPr><w:rPr>, <w:del> must precede <w:sz> (CT_ParaRPr canonical order)
    p2_el = eng.doc.element.body.findall(qn("w:p"))[1]
    p2_rPr = p2_el.find(qn("w:pPr")).find(qn("w:rPr"))
    tags = [c.tag.split("}")[-1] for c in p2_rPr]
    assert tags.index("del") < tags.index("sz"), f"Expected del before sz in CT_ParaRPr, got {tags}"
