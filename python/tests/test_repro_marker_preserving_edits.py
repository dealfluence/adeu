"""
Edits that KEEP a cross-reference or hyperlink must edit only the text around it.

Repro (2026-11-05): a cross-reference
projects as `[~2.1~](#_Ref123)`, and the `_` in the bookmark name routed the
edit down the whole-span markdown path. The engine then deleted the REF field's
result and inserted the projection markup as literal text, so the accepted
document read "Section [~2.1~](#_Ref123), no other licence…" around an empty
field. A bold hyperlink (`[**x**](mailto:x)`) took the same path and wrote
literal "[" / "](mailto:x)" text while deleting the link text.

Node twin: node/packages/core/src/repro.marker-preserving-edits.test.ts
"""

import io
import re

from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from lxml import etree

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
XREF_SENTENCE = "Except as expressly granted in Section 2.1, nothing else is licensed hereunder."


def _doc_with_cross_reference() -> io.BytesIO:
    heading = (
        f'<w:p xmlns:w="{W}">'
        '<w:bookmarkStart w:id="1" w:name="_Ref123"/>'
        "<w:r><w:t>2.1 License Grant.</w:t></w:r>"
        '<w:bookmarkEnd w:id="1"/></w:p>'
    )
    body = (
        f'<w:p xmlns:w="{W}">'
        '<w:r><w:t xml:space="preserve">Except as expressly granted in Section </w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> REF _Ref123 \\r \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>2.1</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "<w:r><w:t>, nothing else is licensed hereunder.</w:t></w:r></w:p>"
    )
    doc = Document()
    doc.element.body.insert(0, parse_xml(body))
    doc.element.body.insert(0, parse_xml(heading))
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def _doc_with_bold_hyperlink() -> io.BytesIO:
    doc = Document()
    paragraph = doc.add_paragraph("Email the team at ")
    rel_id = doc.part.relate_to(
        "mailto:legal@adeu.com",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rpr.append(OxmlElement("w:b"))
    run.append(rpr)
    text = OxmlElement("w:t")
    text.text = "legal@adeu.com"
    run.append(text)
    hyperlink.append(run)
    paragraph._element.append(hyperlink)
    paragraph.add_run(" before Friday.")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def _apply(stream: io.BytesIO, target: str, new: str, comment=None) -> tuple[dict, etree._Element]:
    engine = RedlineEngine(stream, author="QA")
    result = engine.process_batch([ModifyText(target_text=target, new_text=new, comment=comment)])
    out = engine.save_to_stream()
    out.seek(0)
    return result, etree.fromstring(Document(out).element.xml.encode())


def _paragraph_texts(root: etree._Element, accept: bool) -> list:
    """Word's accept-all / reject-all reading of every body paragraph."""
    root = etree.fromstring(etree.tostring(root))
    drop, keep = ("w:del", "w:ins") if accept else ("w:ins", "w:del")
    for el in root.xpath(f"//{drop}", namespaces=NS):
        el.getparent().remove(el)
    if not accept:
        for dt in root.xpath("//w:delText", namespaces=NS):
            dt.tag = qn("w:t")
    return ["".join(p.xpath(".//w:t/text()", namespaces=NS)) for p in root.xpath("//w:body/w:p", namespaces=NS)]


def _inserted_text(root: etree._Element) -> str:
    return "".join(root.xpath("//w:ins//w:t/text()", namespaces=NS))


def _deleted_text(root: etree._Element) -> str:
    return "".join(root.xpath("//w:delText/text()", namespaces=NS))


def test_editing_around_a_kept_cross_reference_leaves_the_field_intact():
    result, root = _apply(
        _doc_with_cross_reference(),
        "Section [~2.1~](#_Ref123), nothing else is licensed hereunder.",
        "Section [~2.1~](#_Ref123), no other licence is granted hereunder.",
    )

    assert result["edits_applied"] == 1
    assert "[~" not in _inserted_text(root) and "](#" not in _inserted_text(root)
    assert "2.1" not in _deleted_text(root), "the REF field result was deleted"
    assert root.xpath("//w:instrText[contains(., 'REF _Ref123')]", namespaces=NS)
    assert _paragraph_texts(root, accept=True)[1] == (
        "Except as expressly granted in Section 2.1, no other licence is granted hereunder."
    )
    assert _paragraph_texts(root, accept=False)[1] == XREF_SENTENCE


def test_editing_just_before_a_kept_cross_reference_leaves_the_field_intact():
    result, root = _apply(
        _doc_with_cross_reference(),
        "expressly granted in Section [~2.1~](#_Ref123)",
        "granted in Section [~2.1~](#_Ref123)",
    )

    assert result["edits_applied"] == 1
    assert "[~" not in _inserted_text(root)
    assert _deleted_text(root).strip() == "expressly"
    assert _paragraph_texts(root, accept=True)[1] == (
        "Except as granted in Section 2.1, nothing else is licensed hereunder."
    )


def test_editing_around_a_kept_bold_hyperlink_leaves_the_link_intact():
    result, root = _apply(
        _doc_with_bold_hyperlink(),
        "Email the team at [**legal@adeu.com**](mailto:legal@adeu.com) before Friday.",
        "Please email the team at [**legal@adeu.com**](mailto:legal@adeu.com) by Friday.",
    )

    assert result["edits_applied"] == 1
    inserted = _inserted_text(root)
    assert "[" not in inserted and "](mailto" not in inserted
    assert "legal@adeu.com" not in _deleted_text(root), "the hyperlink text was deleted"
    link_text = "".join(root.xpath("//w:hyperlink//w:t/text()", namespaces=NS))
    assert link_text == "legal@adeu.com"
    assert _paragraph_texts(root, accept=True)[0] == "Please email the team at legal@adeu.com by Friday."
    assert _paragraph_texts(root, accept=False)[0] == "Email the team at legal@adeu.com before Friday."


def test_a_commented_edit_around_a_kept_cross_reference_attaches_its_comment_once():
    result, root = _apply(
        _doc_with_cross_reference(),
        "expressly granted in Section [~2.1~](#_Ref123), nothing else is licensed",
        "granted in Section [~2.1~](#_Ref123), no other licence is granted",
        comment="Tighten the grant language.",
    )

    assert result["edits_applied"] == 1
    assert "[~" not in _inserted_text(root)
    assert "2.1" not in _deleted_text(root)
    assert len(root.xpath("//w:commentReference", namespaces=NS)) == 1
    assert _paragraph_texts(root, accept=True)[1] == (
        "Except as granted in Section 2.1, no other licence is granted hereunder."
    )


def test_no_literal_projection_markup_reaches_any_run():
    _, root = _apply(
        _doc_with_bold_hyperlink(),
        "Email the team at [**legal@adeu.com**](mailto:legal@adeu.com) before Friday.",
        "Please email the team at [**legal@adeu.com**](mailto:legal@adeu.com) by Friday.",
    )
    all_text = "".join(root.xpath("//w:t/text()", namespaces=NS))
    assert not re.search(r"\]\(|\[~|\*\*", all_text)
