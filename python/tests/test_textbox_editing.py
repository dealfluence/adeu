import io
import zipfile

import ooxml_integrity
import pytest
from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from lxml import etree

from adeu.fields import collect_fields, field_summary
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.models import AcceptChange, InsertTableRow, ModifyText, RejectChange, SetField
from adeu.redline.engine import BatchValidationError, RedlineEngine
from adeu.redline.mapper import DocumentMapper
from adeu.sanitize import sanitize_docx
from adeu.sanitize.transforms import count_tracked_changes
from adeu.text_revision import apply_text_revision_core
from tests.utils import find_duplicate_para_ids

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


class TestTextboxEditingAndInvariants:
    def test_modify_text_inside_textbox_creates_redline_and_syncs_fallback_surgically(self, tmp_path):
        doc = Document()
        host = doc.add_paragraph("Body anchor.")
        box1 = make_ac_textbox_run_xml(
            '<w:p w14:paraId="11111111" w14:textId="22222222">'
            "<w:r><w:t>Notice: delivery obligations are suspended during force majeure.</w:t></w:r>"
            "</w:p>",
            inner_fallback_xml=(
                '<w:p w14:paraId="11111112" w14:textId="22222223">'
                "<w:r><w:t>Notice: delivery obligations are suspended during force majeure.</w:t></w:r>"
                "</w:p>"
            ),
            doc_pr_id=7,
            shape_name="Box One",
        )
        box2 = make_ac_textbox_run_xml(
            '<w:p w14:paraId="33333333" w14:textId="44444444"><w:r><w:t>Untouched sidebar box.</w:t></w:r></w:p>',
            inner_fallback_xml=(
                '<w:p w14:paraId="33333334" w14:textId="44444445"><w:r><w:t>Untouched sidebar box.</w:t></w:r></w:p>'
            ),
            doc_pr_id=8,
            shape_name="Box Two",
        )
        host._p.append(parse_xml(box1))
        host._p.append(parse_xml(box2))
        stream = _to_stream(doc)

        eng = RedlineEngine(io.BytesIO(stream.getvalue()), author="Counsel")
        edit = ModifyText(target_text="force majeure", new_text="severe weather")
        stats = eng.process_batch([edit], partial=False)
        assert stats["edits_applied"] == 1
        assert stats["document_state"]["tracked_changes"] == 2
        assert stats["edits"][0]["part"] == "textbox (word/document.xml)"

        saved = eng.save_to_stream().getvalue()
        assert find_duplicate_para_ids(saved) == []

        out_path = tmp_path / "edited_box.docx"
        out_path.write_bytes(saved)
        assert ooxml_integrity.check(str(out_path)) == []

        raw = extract_text_from_stream(io.BytesIO(saved), clean_view=False, include_appendix=False)
        clean = extract_text_from_stream(io.BytesIO(saved), clean_view=True, include_appendix=False)
        assert "{--force majeure--}{++severe weather++}" in raw
        assert "Notice: delivery obligations are suspended during severe weather." in clean

        with zipfile.ZipFile(io.BytesIO(saved)) as z:
            root = etree.fromstring(z.read("word/document.xml"))
        ns = {"mc": MC_NS, "w": W_NS, "v": V_NS, "w14": W14_NS}
        acs = root.findall(".//mc:AlternateContent", ns)
        assert len(acs) == 2

        # Box 1: both Choice and Fallback carry the tracked replacement with distinct w:ids
        c1_ins = acs[0].findall("mc:Choice//w:ins", ns)
        c1_del = acs[0].findall("mc:Choice//w:del", ns)
        f1_ins = acs[0].findall("mc:Fallback//w:ins", ns)
        f1_del = acs[0].findall("mc:Fallback//w:del", ns)
        assert len(c1_ins) == 1 and len(c1_del) == 1
        assert len(f1_ins) == 1 and len(f1_del) == 1
        choice_ids = {c1_ins[0].get(f"{{{W_NS}}}id"), c1_del[0].get(f"{{{W_NS}}}id")}
        fallback_ids = {f1_ins[0].get(f"{{{W_NS}}}id"), f1_del[0].get(f"{{{W_NS}}}id")}
        assert choice_ids.isdisjoint(fallback_ids)

        # Box 2 (untouched): paraIds in Choice and Fallback stayed untouched
        f2_p = acs[1].find("mc:Fallback//w:p", ns)
        assert f2_p is not None
        assert f2_p.get(f"{{{W14_NS}}}paraId") == "33333334"

    def test_cross_boundary_and_comment_restrictions_on_textboxes(self, tmp_path):
        doc = Document()
        host = doc.add_paragraph("Body tail sentence.")
        host._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>First box sentence.</w:t></w:r></w:p>",
                    doc_pr_id=7,
                    shape_name="Box A",
                )
            )
        )
        host._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>Second box sentence.</w:t></w:r></w:p>",
                    doc_pr_id=8,
                    shape_name="Box B",
                )
            )
        )
        raw_bytes = _to_stream(doc).getvalue()

        # 1. Cross body -> textbox boundary refused
        with pytest.raises(BatchValidationError, match=r"spans a structural document-part boundary \(body → textbox\)"):
            RedlineEngine(io.BytesIO(raw_bytes), author="Counsel").process_batch(
                [ModifyText(target_text="Body tail sentence.\n\nFirst box sentence.", new_text="Merged.")],
                partial=False,
            )

        # 2. Cross textbox -> textbox boundary refused
        with pytest.raises(
            BatchValidationError, match=r"spans a structural document-part boundary \(textbox → textbox\)"
        ):
            RedlineEngine(io.BytesIO(raw_bytes), author="Counsel").process_batch(
                [ModifyText(target_text="First box sentence.\n\nSecond box sentence.", new_text="Merged.")],
                partial=False,
            )

        # 3. Comment-only edit inside textbox refused
        with pytest.raises(BatchValidationError, match=r"comments cannot be anchored inside a textbox part"):
            RedlineEngine(io.BytesIO(raw_bytes), author="Counsel").process_batch(
                [
                    ModifyText(
                        target_text="First box sentence.",
                        new_text="First box sentence.",
                        comment="Cannot anchor here.",
                    )
                ],
                partial=False,
            )

        # 4. Modify + comment inside textbox applies edit, drops comment with warning, keeps XML valid
        eng = RedlineEngine(io.BytesIO(raw_bytes), author="Counsel")
        stats = eng.process_batch(
            [
                ModifyText(
                    target_text="First box sentence.",
                    new_text="Revised box sentence.",
                    comment="Dropped bubble.",
                )
            ],
            partial=False,
        )
        assert stats["edits_applied"] == 1
        assert any("comments inside a textbox part" in msg for msg in eng.skipped_details)
        saved = eng.save_to_stream().getvalue()
        p = tmp_path / "mod_comment_box.docx"
        p.write_bytes(saved)
        assert ooxml_integrity.check(str(p)) == []
        with zipfile.ZipFile(io.BytesIO(saved)) as z:
            xml_txt = z.read("word/document.xml").decode("utf-8")
        assert "commentRangeStart" not in xml_txt

    def test_paragraph_split_delete_last_paragraph_floor_and_review_actions(self, tmp_path):
        doc = Document()
        host = doc.add_paragraph("Body paragraph.")
        host._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>Sole boxed paragraph.</w:t></w:r></w:p>",
                    doc_pr_id=7,
                    shape_name="Box Sole",
                )
            )
        )
        base_bytes = _to_stream(doc).getvalue()

        # Split inside textbox -> 2 paragraphs, then accept single Chg id syncs Fallback too
        eng_split = RedlineEngine(io.BytesIO(base_bytes), author="Counsel")
        eng_split.process_batch(
            [ModifyText(target_text="Sole boxed paragraph.", new_text="First line.\n\nSecond line.")],
            partial=False,
        )
        split_bytes = eng_split.save_to_stream().getvalue()
        assert find_duplicate_para_ids(split_bytes) == []

        eng_review = RedlineEngine(io.BytesIO(split_bytes), author="Reviewer")
        chg_ids = eng_review._existing_change_ids()
        assert chg_ids
        eng_review.process_batch([AcceptChange(target_id=f"Chg:{cid}") for cid in chg_ids], partial=False)
        reviewed_bytes = eng_review.save_to_stream().getvalue()
        p_rev = tmp_path / "reviewed.docx"
        p_rev.write_bytes(reviewed_bytes)
        assert ooxml_integrity.check(str(p_rev)) == []
        with zipfile.ZipFile(io.BytesIO(reviewed_bytes)) as z:
            rev_xml = etree.fromstring(z.read("word/document.xml"))
        assert rev_xml.findall(f".//{{{W_NS}}}ins") == []
        assert rev_xml.findall(f".//{{{W_NS}}}del") == []

        # Delete all paragraphs in textbox (first line + pilcrow, then second line) and accept_all,
        # plus verify a sole paragraph carrying a tracked pilcrow deletion <w:pPr><w:rPr><w:del/>:
        # <w:txbxContent> must retain >= 1 <w:p> so CT_TxbxContent stays schema-valid.
        sole_pilcrow_del_inner = (
            "<w:p>"
            '<w:pPr><w:rPr><w:del w:id="1" w:author="A" w:date="2026-11-17T09:00:00Z"/></w:rPr></w:pPr>'
            '<w:del w:id="2" w:author="A" w:date="2026-11-17T09:00:00Z"><w:r><w:delText>Gone</w:delText></w:r></w:del>'
            "</w:p>"
        )
        doc_pilcrow = Document()
        p_pilcrow = doc_pilcrow.add_paragraph("Anchor.")
        p_pilcrow._p.append(parse_xml(make_ac_textbox_run_xml(sole_pilcrow_del_inner)))
        eng_acc = RedlineEngine(_to_stream(doc_pilcrow), author="Counsel")
        counts = eng_acc.accept_all_revisions()
        assert counts["accepted_deletions"] == 2
        acc_bytes = eng_acc.save_to_stream().getvalue()
        p_acc = tmp_path / "accepted_empty_box.docx"
        p_acc.write_bytes(acc_bytes)
        assert ooxml_integrity.check(str(p_acc)) == []
        with zipfile.ZipFile(io.BytesIO(acc_bytes)) as z:
            acc_root = etree.fromstring(z.read("word/document.xml"))
        for tc in acc_root.findall(f".//{{{W_NS}}}txbxContent"):
            assert len(tc.findall(f"{{{W_NS}}}p")) >= 1, "CT_TxbxContent must keep at least one <w:p>"

        # RejectChange on a redlined textbox restores both Choice and Fallback cleanly
        eng_rej_setup = RedlineEngine(io.BytesIO(base_bytes), author="Counsel")
        eng_rej_setup.process_batch(
            [ModifyText(target_text="Sole boxed paragraph.", new_text="Changed boxed paragraph.")],
            partial=False,
        )
        rej_in = eng_rej_setup.save_to_stream().getvalue()
        eng_rej = RedlineEngine(io.BytesIO(rej_in), author="Reviewer")
        first_cid = eng_rej._existing_change_ids()[0]
        eng_rej.process_batch([RejectChange(target_id=f"Chg:{first_cid}")], partial=False)
        rej_out = eng_rej.save_to_stream().getvalue()
        assert "Sole boxed paragraph." in extract_text_from_stream(io.BytesIO(rej_out), include_appendix=False)
        with zipfile.ZipFile(io.BytesIO(rej_out)) as z:
            rej_root = etree.fromstring(z.read("word/document.xml"))
        assert rej_root.findall(f".//{{{W_NS}}}ins") == []
        assert rej_root.findall(f".//{{{W_NS}}}del") == []

    def test_accept_all_and_sanitize_do_not_double_count_fallback_revisions(self, tmp_path):
        doc = Document()
        host = doc.add_paragraph("Body clause.")
        host._p.append(parse_xml(make_ac_textbox_run_xml("<w:p><w:r><w:t>10 Old Road</w:t></w:r></w:p>")))
        eng = RedlineEngine(_to_stream(doc), author="Counsel")
        eng.process_batch([ModifyText(target_text="10 Old Road", new_text="25 New Road")], partial=False)
        redlined_bytes = eng.save_to_stream().getvalue()

        # 1. RedlineEngine.accept_all_revisions counts 1 ins + 1 del (not 2 + 2)
        eng_acc = RedlineEngine(io.BytesIO(redlined_bytes), author="Counsel")
        res = eng_acc.accept_all_revisions()
        assert res["accepted_insertions"] == 1
        assert res["accepted_deletions"] == 1

        # 2. sanitize_docx(accept_all=True) counts 2 tracked changes (not 4) and leaves zero revisions
        in_file = tmp_path / "in_redlined.docx"
        out_file = tmp_path / "out_sanitized.docx"
        in_file.write_bytes(redlined_bytes)
        san_res = sanitize_docx(str(in_file), str(out_file), accept_all=True)
        assert san_res.status in ("clean", "clean_with_warnings")
        assert san_res.tracked_changes_found == 2
        assert san_res.tracked_changes_accepted == 2
        san_doc = Document(str(out_file))
        assert count_tracked_changes(san_doc) == (0, 0, 0)
        with zipfile.ZipFile(out_file) as z:
            san_root = etree.fromstring(z.read("word/document.xml"))
        assert san_root.findall(f".//{{{W_NS}}}ins") == []
        assert san_root.findall(f".//{{{W_NS}}}del") == []

    def test_header_textbox_table_ops_set_field_and_text_revision(self, tmp_path):
        doc = Document()
        sec = doc.sections[0]
        hdr_p = sec.header.paragraphs[0]
        hdr_p.text = "Header Banner"
        hdr_p._p.append(
            parse_xml(
                make_ac_textbox_run_xml(
                    "<w:p><w:r><w:t>Suite 100, Old Tower</w:t></w:r></w:p>",
                    doc_pr_id=31,
                    shape_name="HeaderAddr",
                )
            )
        )
        body_p = doc.add_paragraph("Main body text.")
        box_inner = (
            "<w:sdt>"
            '<w:sdtPr><w:alias w:val="SignerName"/><w:text/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p></w:sdtContent>"
            "</w:sdt>"
            "<w:tbl>"
            "<w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
            "<w:tr>"
            "<w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Fee</w:t></w:r></w:p></w:tc>"
            "</w:tr>"
            "<w:tr>"
            "<w:tc><w:p><w:r><w:t>Setup</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>$100</w:t></w:r></w:p></w:tc>"
            "</w:tr>"
            "</w:tbl>"
        )
        body_p._p.append(parse_xml(make_ac_textbox_run_xml(box_inner, doc_pr_id=32, shape_name="BodyTableBox")))
        base_bytes = _to_stream(doc).getvalue()

        eng = RedlineEngine(io.BytesIO(base_bytes), author="Counsel")
        stats = eng.process_batch(
            [
                ModifyText(target_text="Suite 100, Old Tower", new_text="Suite 500, New Tower"),
                SetField(field="CC:1", value="John Smith"),
                InsertTableRow(target_text="Setup", position="below", cells=["Support", "$250"]),
            ],
            partial=False,
        )
        assert stats["edits_applied"] == 3
        out_bytes = eng.save_to_stream().getvalue()
        p_out = tmp_path / "header_table_field.docx"
        p_out.write_bytes(out_bytes)
        assert ooxml_integrity.check(str(p_out)) == []

        clean = extract_text_from_stream(io.BytesIO(out_bytes), clean_view=True, include_appendix=False)
        assert "Suite 500, New Tower" in clean
        assert "John Smith" in clean
        assert "Support | $250" in clean

        # Whole-document apply_text_revision_core editing inside a textbox
        rev_src = tmp_path / "rev_src.docx"
        rev_dst = tmp_path / "rev_dst.docx"
        rev_src.write_bytes(base_bytes)
        orig_clean = extract_text_from_stream(io.BytesIO(base_bytes), clean_view=True, include_appendix=False)
        revised_clean = orig_clean.replace("Suite 100, Old Tower", "Suite 900, Final Tower")
        rev_stats, _ = apply_text_revision_core(str(rev_src), revised_clean, str(rev_dst), author="Counsel")
        assert rev_stats["edits_applied"] >= 1
        assert ooxml_integrity.check(str(rev_dst)) == []
