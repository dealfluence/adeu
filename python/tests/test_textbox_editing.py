import io

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml import parse_xml
from docx.oxml.ns import qn

from adeu.fields import collect_fields, field_summary
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.redline.mapper import DocumentMapper

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
V_NS = "urn:schemas-microsoft-com:vml"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"


def make_ac_textbox_run_xml(
    inner_choice_xml: str,
    inner_fallback_xml: str | None = None,
    *,
    doc_pr_id: int = 7,
    shape_name: str = "Text Box 7",
) -> str:
    fb_xml = inner_fallback_xml if inner_fallback_xml is not None else inner_choice_xml
    return (
        f'<w:r xmlns:w="{W_NS}" xmlns:mc="{MC_NS}" xmlns:wp="{WP_NS}" '
        f'xmlns:a="{A_NS}" xmlns:wps="{WPS_NS}" xmlns:v="{V_NS}" xmlns:w14="{W14_NS}">'
        "<mc:AlternateContent>"
        '<mc:Choice Requires="wps">'
        "<w:drawing>"
        '<wp:anchor behindDoc="0" distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="2" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        '<wp:extent cx="2540000" cy="1270000"/>'
        "<wp:wrapNone/>"
        f'<wp:docPr id="{doc_pr_id}" name="{shape_name}"/>'
        f'<a:graphic><a:graphicData uri="{WPS_NS}">'
        f"<wps:wsp><wps:txbx><w:txbxContent>{inner_choice_xml}</w:txbxContent></wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic>"
        "</wp:anchor>"
        "</w:drawing>"
        "</mc:Choice>"
        "<mc:Fallback>"
        "<w:pict>"
        f'<v:shape id="{shape_name}" style="position:absolute">'
        f"<v:textbox><w:txbxContent>{fb_xml}</w:txbxContent></v:textbox>"
        "</v:shape>"
        "</w:pict>"
        "</mc:Fallback>"
        "</mc:AlternateContent>"
        "</w:r>"
    )


def _to_stream(doc: DocumentObject) -> io.BytesIO:
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


class TestTextboxProjectionAndMapping:
    def test_modern_alternate_content_textbox_projects_full_text_without_truncation_or_image_marker(self):
        long_tail = (
            "Tail obligation beyond three hundred characters: the Supplier shall maintain "
            "escrow deposits in Zurich until final acceptance is countersigned."
        )
        para_one = (
            "Cover Box Paragraph One: This Master Services Agreement is entered into by "
            "Alpha Holdings GmbH and Beta Operations Ltd. with registered offices in Berlin "
            "and London respectively, subject to the schedules attached hereto and all "
            "ancillary data-processing addenda executed on the Effective Date."
        )
        assert len(para_one) + len(long_tail) > 350

        inner = (
            f'<w:p><w:r><w:t xml:space="preserve">{para_one}</w:t></w:r></w:p>'
            f'<w:p><w:r><w:t xml:space="preserve">{long_tail}</w:t></w:r></w:p>'
        )
        doc = Document()
        host = doc.add_paragraph("Body anchor paragraph.")
        host._p.append(parse_xml(make_ac_textbox_run_xml(inner)))
        doc.add_paragraph("Body closing paragraph.")

        stream = _to_stream(doc)
        text = extract_text_from_stream(stream, clean_view=False, include_appendix=False)

        assert para_one in text
        assert long_tail in text
        assert f"{para_one}\n\n{long_tail}" in text
        assert text.count(para_one) == 1
        assert "docx-image:" not in text
        assert "![Text box" not in text

        reloaded = Document(io.BytesIO(stream.getvalue()))
        mapper = DocumentMapper(reloaded)
        assert mapper.full_text == text
        box_idx = mapper.full_text.index(para_one)
        assert mapper.part_kind_at(box_idx) == "textbox"
        spans = [s for s in mapper.spans if s.run is not None and s.start <= box_idx < s.end]
        assert spans, "boxed text must map to real run-backed spans"

    def test_existing_redlines_and_formatting_inside_textbox_project_criticmarkup_and_clean_view(self):
        choice_inner = (
            "<w:p>"
            '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Notice:</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> Address is </w:t></w:r>'
            '<w:del w:id="1" w:author="Counterparty" w:date="2026-11-17T09:00:00Z">'
            "<w:r><w:delText>10 Old Road</w:delText></w:r>"
            "</w:del>"
            '<w:ins w:id="2" w:author="Counterparty" w:date="2026-11-17T09:00:00Z">'
            "<w:r><w:t>25 New Avenue</w:t></w:r>"
            "</w:ins>"
            "</w:p>"
        )
        fb_inner = choice_inner.replace('w:id="1"', 'w:id="3"').replace('w:id="2"', 'w:id="4"')

        doc = Document()
        p = doc.add_paragraph("Main clause.")
        p._p.append(parse_xml(make_ac_textbox_run_xml(choice_inner, fb_inner)))
        stream = _to_stream(doc)

        raw = extract_text_from_stream(io.BytesIO(stream.getvalue()), clean_view=False, include_appendix=False)
        clean = extract_text_from_stream(io.BytesIO(stream.getvalue()), clean_view=True, include_appendix=False)

        assert "**Notice:**" in raw
        assert "{--10 Old Road--}" in raw
        assert "{++25 New Avenue++}" in raw
        assert "10 Old Road" not in clean
        assert "**Notice:** Address is 25 New Avenue" in clean

        d_loaded = Document(io.BytesIO(stream.getvalue()))
        assert DocumentMapper(d_loaded, clean_view=False).full_text == raw
        assert DocumentMapper(d_loaded, clean_view=True).full_text == clean

    def test_bare_vml_textbox_and_header_footer_textboxes_project_as_textbox_parts(self):
        doc = Document()
        sec = doc.sections[0]
        hdr_p = sec.header.paragraphs[0]
        hdr_p.text = "Running Header"
        hdr_p._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>Letterhead Box Suite 400</w:t></w:r></w:p>",
                    doc_pr_id=11,
                    shape_name="Header Box",
                )
            )
        )

        body_p = doc.add_paragraph("Main body covenant.")
        vml_run = (
            f'<w:r xmlns:w="{W_NS}" xmlns:v="{V_NS}">'
            "<w:pict>"
            '<v:shape id="LegacyBox" style="position:absolute">'
            "<v:textbox><w:txbxContent>"
            "<w:p><w:r><w:t>Legacy VML callout obligation.</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox>"
            "</v:shape>"
            "</w:pict>"
            "</w:r>"
        )
        body_p._p.append(parse_xml(vml_run))

        ftr_p = sec.footer.paragraphs[0]
        ftr_p.text = "Running Footer"
        ftr_p._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>Footer Registered Office</w:t></w:r></w:p>",
                    doc_pr_id=12,
                    shape_name="Footer Box",
                )
            )
        )

        stream = _to_stream(doc)
        reloaded = Document(io.BytesIO(stream.getvalue()))
        text = _extract_text_from_doc(reloaded, include_appendix=False)
        mapper = DocumentMapper(reloaded)

        assert mapper.full_text == text
        expected_order = [
            "Running Header",
            "Letterhead Box Suite 400",
            "Main body covenant.",
            "Legacy VML callout obligation.",
            "Running Footer",
            "Footer Registered Office",
        ]
        positions = [text.index(fragment) for fragment in expected_order]
        assert positions == sorted(positions), f"unexpected projection order:\n{text}"

        for box_frag in ("Letterhead Box Suite 400", "Legacy VML callout obligation.", "Footer Registered Office"):
            assert mapper.part_kind_at(mapper.full_text.index(box_frag)) == "textbox"

    def test_table_and_content_controls_inside_textbox_preserve_ordinals_and_twin_sync(self):
        doc = Document()
        body = doc.element.body
        sect_pr = body.find(qn("w:sectPr"))

        body_sdt = parse_xml(
            f'<w:sdt xmlns:w="{W_NS}">'
            '<w:sdtPr><w:alias w:val="BodyParty"/><w:text/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>Acme Corp</w:t></w:r></w:p></w:sdtContent>"
            "</w:sdt>"
        )
        body.insert(list(body).index(sect_pr), body_sdt)

        box_inner = (
            "<w:sdt>"
            '<w:sdtPr><w:alias w:val="BoxAddress"/><w:text/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>123 Main St</w:t></w:r></w:p></w:sdtContent>"
            "</w:sdt>"
            "<w:tbl>"
            "<w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
            "<w:tr>"
            "<w:tc><w:p><w:r><w:t>Key</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Val</w:t></w:r></w:p></w:tc>"
            "</w:tr>"
            "<w:tr>"
            "<w:tc><w:p><w:r><w:t>City</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Zurich</w:t></w:r></w:p></w:tc>"
            "</w:tr>"
            "</w:tbl>"
        )
        anchor_p = parse_xml(f'<w:p xmlns:w="{W_NS}"><w:r><w:t>Anchor</w:t></w:r></w:p>')
        anchor_p.append(parse_xml(make_ac_textbox_run_xml(box_inner)))
        body.insert(list(body).index(sect_pr), anchor_p)

        ftr = doc.sections[0].footer
        ftr.is_linked_to_previous = False
        ftr._element.append(
            parse_xml(
                f'<w:sdt xmlns:w="{W_NS}">'
                '<w:sdtPr><w:alias w:val="FooterRef"/><w:text/></w:sdtPr>'
                "<w:sdtContent><w:p><w:r><w:t>REF-9</w:t></w:r></w:p></w:sdtContent>"
                "</w:sdt>"
            )
        )

        stream = _to_stream(doc)
        reloaded = Document(io.BytesIO(stream.getvalue()))
        raw = _extract_text_from_doc(reloaded, clean_view=False, include_appendix=False)
        assert isinstance(raw, str)
        mapper = DocumentMapper(reloaded)

        assert mapper.full_text == raw
        total, empty, locked, bound = field_summary(reloaded)
        assert total == 3, f"expected 3 unique SDTs (mc:Fallback excluded), got {total}"

        entries = collect_fields(reloaded, raw)
        assert [(e.ordinal, e.alias, e.value) for e in entries] == [
            (1, "BodyParty", "Acme Corp"),
            (2, "BoxAddress", "123 Main St"),
            (3, "FooterRef", "REF-9"),
        ]
        assert "Key | Val\n--- | ---\nCity | Zurich" in raw

    def test_deleted_or_inserted_anchor_run_hides_textbox_in_clean_or_original_view(self):
        doc = Document()
        p = doc.add_paragraph("Host prose.")
        del_box_xml = make_ac_textbox_run_xml(
            "<w:p><w:r><w:t>Deleted Box Text</w:t></w:r></w:p>", doc_pr_id=21, shape_name="DelBox"
        )
        ins_box_xml = make_ac_textbox_run_xml(
            "<w:p><w:r><w:t>Inserted Box Text</w:t></w:r></w:p>", doc_pr_id=22, shape_name="InsBox"
        )
        del_wrap = parse_xml(
            f'<w:del xmlns:w="{W_NS}" w:id="10" w:author="A" w:date="2026-11-17T09:00:00Z">{del_box_xml}</w:del>'
        )
        ins_wrap = parse_xml(
            f'<w:ins xmlns:w="{W_NS}" w:id="11" w:author="A" w:date="2026-11-17T09:00:00Z">{ins_box_xml}</w:ins>'
        )
        p._p.append(del_wrap)
        p._p.append(ins_wrap)

        stream = _to_stream(doc)
        reloaded = Document(io.BytesIO(stream.getvalue()))

        raw = _extract_text_from_doc(reloaded, clean_view=False, include_appendix=False)
        clean = _extract_text_from_doc(reloaded, clean_view=True, include_appendix=False)

        assert "Deleted Box Text" in raw
        assert "Inserted Box Text" in raw
        assert "docx-image:" not in raw
        assert "Deleted Box Text" not in clean
        assert "Inserted Box Text" in clean

        assert DocumentMapper(reloaded, clean_view=False).full_text == raw
        assert DocumentMapper(reloaded, clean_view=True).full_text == clean

        orig_mapper = DocumentMapper(reloaded, original_view=True)
        assert "Deleted Box Text" in orig_mapper.full_text
        assert "Inserted Box Text" not in orig_mapper.full_text
