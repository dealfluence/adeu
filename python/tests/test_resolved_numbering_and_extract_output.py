import json
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

from adeu.compare import build_redline_comparison
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.models import ModifyText
from adeu.outline import extract_outline
from adeu.pagination import paginate
from adeu.redline.engine import RedlineEngine
from adeu.redline.mapper import DocumentMapper
from adeu.text_revision import apply_text_revision_core
from tests.utils import run_cli


def _attach_custom_numbering_xml(doc: Document, numbering_inner_xml: str) -> None:
    """Replaces or creates word/numbering.xml on an in-memory python-docx Document."""
    # Force python-docx to materialize numbering_part by touching a list style first if needed
    numbering_part = doc.part.numbering_part
    root = parse_xml(f"<w:numbering {nsdecls('w')}>{numbering_inner_xml}</w:numbering>")
    numbering_part._element.clear()
    for child in list(root):
        numbering_part._element.append(child)


def _set_num_pr(paragraph, num_id: str | int, ilvl: int | None = 0) -> None:
    pPr = paragraph._element.get_or_add_pPr()
    existing = pPr.find(qn("w:numPr"))
    if existing is not None:
        pPr.remove(existing)
    numPr = OxmlElement("w:numPr")
    if ilvl is not None:
        ilvl_el = OxmlElement("w:ilvl")
        ilvl_el.set(qn("w:val"), str(ilvl))
        numPr.append(ilvl_el)
    numId_el = OxmlElement("w:numId")
    numId_el.set(qn("w:val"), str(num_id))
    numPr.append(numId_el)
    pPr.append(numPr)


class TestResolvedOoxmlNumbering:
    def test_sequential_list_number_resolves_clause_numbers(self):
        doc = Document()
        doc.add_paragraph("First obligation of Seller.", style="List Number")
        doc.add_paragraph("Second obligation of Seller.", style="List Number")
        doc.add_paragraph("Liability cap clause.", style="List Number")
        doc.add_paragraph("Cross-reference: see Clause 3 above.")

        buf = BytesIO()
        doc.save(buf)
        buf.seek(0)
        text = extract_text_from_stream(buf, include_appendix=False)

        assert "1. First obligation of Seller." in text
        assert "2. Second obligation of Seller." in text
        assert "3. Liability cap clause." in text

        reloaded = Document(BytesIO(buf.getvalue()))
        mapper = DocumentMapper(reloaded)
        assert mapper.full_text == text

    def test_multilevel_polish_legal_alpha_roman_and_overrides(self):
        doc = Document()
        _attach_custom_numbering_xml(
            doc,
            """
            <w:abstractNum w:abstractNumId="10">
              <w:lvl w:ilvl="0">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:lvlText w:val="§ %1."/>
              </w:lvl>
              <w:lvl w:ilvl="1">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:lvlText w:val="%1.%2."/>
              </w:lvl>
              <w:lvl w:ilvl="2">
                <w:start w:val="1"/>
                <w:numFmt w:val="lowerLetter"/>
                <w:lvlText w:val="(%3)"/>
              </w:lvl>
            </w:abstractNum>
            <w:abstractNum w:abstractNumId="20">
              <w:lvl w:ilvl="0">
                <w:start w:val="1"/>
                <w:numFmt w:val="upperRoman"/>
                <w:lvlText w:val="ARTICLE %1"/>
              </w:lvl>
              <w:lvl w:ilvl="1">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:isLgl/>
                <w:lvlText w:val="%1.%2"/>
              </w:lvl>
            </w:abstractNum>
            <w:abstractNum w:abstractNumId="30">
              <w:lvl w:ilvl="0">
                <w:start w:val="1"/>
                <w:numFmt w:val="upperLetter"/>
                <w:lvlText w:val="Part %1"/>
              </w:lvl>
              <w:lvl w:ilvl="1">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:lvlRestart w:val="0"/>
                <w:lvlText w:val="Clause %2."/>
              </w:lvl>
            </w:abstractNum>
            <w:num w:numId="10"><w:abstractNumId w:val="10"/></w:num>
            <w:num w:numId="11">
              <w:abstractNumId w:val="10"/>
              <w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride>
            </w:num>
            <w:num w:numId="20"><w:abstractNumId w:val="20"/></w:num>
            <w:num w:numId="30"><w:abstractNumId w:val="30"/></w:num>
            """,
        )

        # Polish hierarchical list (numId 10)
        p = doc.add_paragraph("Przedmiot umowy")
        _set_num_pr(p, 10, 0)
        p = doc.add_paragraph("Zakres usług")
        _set_num_pr(p, 10, 1)
        p = doc.add_paragraph("Konsultacje")
        _set_num_pr(p, 10, 2)
        p = doc.add_paragraph("Wdrożenie")
        _set_num_pr(p, 10, 2)
        p = doc.add_paragraph("Terminy")
        _set_num_pr(p, 10, 1)
        p = doc.add_paragraph("Wynagrodzenie")
        _set_num_pr(p, 10, 0)
        p = doc.add_paragraph("Fakturowanie")
        _set_num_pr(p, 10, 1)

        # Start override list (numId 11 starts at 5)
        p = doc.add_paragraph("Kary umowne")
        _set_num_pr(p, 11, 0)
        p = doc.add_paragraph("Odstąpienie")
        _set_num_pr(p, 11, 0)

        # Roman + isLgl sublevel (numId 20: ARTICLE I -> 1.1, ARTICLE II -> 2.1)
        p = doc.add_paragraph("Definitions")
        _set_num_pr(p, 20, 0)
        p = doc.add_paragraph("Defined Terms")
        _set_num_pr(p, 20, 1)
        p = doc.add_paragraph("Obligations")
        _set_num_pr(p, 20, 0)
        p = doc.add_paragraph("Performance")
        _set_num_pr(p, 20, 1)

        # lvlRestart=0 continuous clause numbering across Parts (numId 30)
        p = doc.add_paragraph("General")
        _set_num_pr(p, 30, 0)
        p = doc.add_paragraph("First continuous")
        _set_num_pr(p, 30, 1)
        p = doc.add_paragraph("Special")
        _set_num_pr(p, 30, 0)
        p = doc.add_paragraph("Second continuous")
        _set_num_pr(p, 30, 1)

        buf = BytesIO()
        doc.save(buf)
        reloaded = Document(BytesIO(buf.getvalue()))
        text = _extract_text_from_doc(reloaded, include_appendix=False)
        mapper = DocumentMapper(reloaded)
        assert mapper.full_text == text

        expected_lines = [
            "§ 1. Przedmiot umowy",
            "    1.1. Zakres usług",
            "        (a) Konsultacje",
            "        (b) Wdrożenie",
            "    1.2. Terminy",
            "§ 2. Wynagrodzenie",
            "    2.1. Fakturowanie",
            "§ 5. Kary umowne",
            "§ 6. Odstąpienie",
            "ARTICLE I Definitions",
            "    1.1 Defined Terms",
            "ARTICLE II Obligations",
            "    2.1 Performance",
            "Part A General",
            "    Clause 1. First continuous",
            "Part B Special",
            "    Clause 2. Second continuous",
        ]
        assert text == "\n\n".join(expected_lines)

    def test_auto_numbered_headings_project_in_body_and_outline(self):
        doc = Document()
        _attach_custom_numbering_xml(
            doc,
            """
            <w:abstractNum w:abstractNumId="40">
              <w:lvl w:ilvl="0">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:pStyle w:val="Heading1"/>
                <w:lvlText w:val="§ %1."/>
              </w:lvl>
              <w:lvl w:ilvl="1">
                <w:start w:val="1"/>
                <w:numFmt w:val="decimal"/>
                <w:pStyle w:val="Heading2"/>
                <w:lvlText w:val="%1.%2."/>
              </w:lvl>
            </w:abstractNum>
            <w:num w:numId="40"><w:abstractNumId w:val="40"/></w:num>
            """,
        )
        # Attach numPr without ilvl on Heading 2 to verify <w:pStyle> level inference
        h1 = doc.add_heading("Postanowienia ogólne", level=1)
        _set_num_pr(h1, 40, None)
        h2 = doc.add_heading("Definicje umowne", level=2)
        _set_num_pr(h2, 40, None)
        h1b = doc.add_heading("Odpowiedzialność stron", level=1)
        _set_num_pr(h1b, 40, None)
        h_unnumbered = doc.add_heading("Załączniki", level=1)
        _set_num_pr(h_unnumbered, 0, 0)

        buf = BytesIO()
        doc.save(buf)
        reloaded = Document(BytesIO(buf.getvalue()))
        text, offsets = _extract_text_from_doc(reloaded, include_appendix=False, return_paragraph_offsets=True)
        mapper = DocumentMapper(reloaded)
        assert mapper.full_text == text

        assert "# § 1. Postanowienia ogólne" in text
        assert "## 1.1. Definicje umowne" in text
        assert "# § 2. Odpowiedzialność stron" in text
        assert "# Załączniki" in text

        pag = paginate(text, "")
        page_bodies = [p.page_content for p in pag.pages]
        nodes_fast = extract_outline(reloaded, text, page_bodies, pag.body_page_offsets, paragraph_offsets=offsets)
        nodes_slow = extract_outline(reloaded, text, page_bodies, pag.body_page_offsets, paragraph_offsets=None)
        assert [n.text for n in nodes_fast] == [
            "§ 1. Postanowienia ogólne",
            "1.1. Definicje umowne",
            "§ 2. Odpowiedzialność stron",
            "Załączniki",
        ]
        assert [n.text for n in nodes_slow] == [n.text for n in nodes_fast]

    def test_clean_view_renumbers_after_deleted_list_item(self):
        doc = Document()
        doc.add_paragraph("Clause Alpha.", style="List Number")
        doc.add_paragraph("Clause Beta.", style="List Number")
        doc.add_paragraph("Clause Gamma.", style="List Number")
        buf = BytesIO()
        doc.save(buf)

        engine = RedlineEngine(BytesIO(buf.getvalue()), author="Reviewer")
        stats = engine.process_batch([ModifyText(type="modify", target_text="2. Clause Beta.\n\n", new_text="")])
        assert stats["edits_applied"] == 1

        out_bytes = engine.save_to_stream().getvalue()
        reloaded = Document(BytesIO(out_bytes))

        clean_ingest = _extract_text_from_doc(reloaded, clean_view=True, include_appendix=False)
        clean_mapper = DocumentMapper(reloaded, clean_view=True)
        assert clean_mapper.full_text == clean_ingest
        assert clean_ingest == "1. Clause Alpha.\n\n2. Clause Gamma."

        raw_ingest = _extract_text_from_doc(reloaded, clean_view=False, include_appendix=False)
        raw_mapper = DocumentMapper(reloaded, clean_view=False)
        assert raw_mapper.full_text == raw_ingest

    def test_sequential_batch_and_edit_without_duplicating_number(self):
        doc = Document()
        doc.add_paragraph("First obligation.", style="List Number")
        doc.add_paragraph("Second obligation.", style="List Number")
        doc.add_paragraph("Liability cap is 100 PLN.", style="List Number")
        buf = BytesIO()
        doc.save(buf)

        engine = RedlineEngine(BytesIO(buf.getvalue()), author="Reviewer")
        stats = engine.process_batch(
            [
                ModifyText(
                    type="modify",
                    target_text="1. First obligation.",
                    new_text="1. First obligation.\n\n1. Inserted obligation.",
                ),
                ModifyText(
                    type="modify",
                    target_text="3. Liability cap is 100 PLN.",
                    new_text="3. Liability cap is 500 PLN.",
                ),
            ]
        )
        assert stats["edits_applied"] == 2
        assert stats["edits_skipped"] == 0

        accepted = _extract_text_from_doc(Document(engine.save_to_stream()), clean_view=True, include_appendix=False)
        assert accepted == (
            "1. First obligation.\n\n2. Inserted obligation.\n\n3. Second obligation.\n\n4. Liability cap is 500 PLN."
        )

    def test_compare_ignores_downstream_auto_renumbering(self, tmp_path: Path):
        v1 = Document()
        v1.add_paragraph("Clause Alpha.", style="List Number")
        v1.add_paragraph("Clause Beta.", style="List Number")
        v1.add_paragraph("Clause Gamma.", style="List Number")
        v1.add_paragraph("Clause Delta.", style="List Number")
        v1_path = tmp_path / "v1.docx"
        v1.save(str(v1_path))

        v2 = Document()
        v2.add_paragraph("Clause Alpha.", style="List Number")
        v2.add_paragraph("Clause Inserted.", style="List Number")
        v2.add_paragraph("Clause Beta.", style="List Number")
        v2.add_paragraph("Clause Gamma.", style="List Number")
        v2.add_paragraph("Clause Delta.", style="List Number")
        v2_path = tmp_path / "v2.docx"
        v2.save(str(v2_path))

        cmp_res = build_redline_comparison(v1_path.read_bytes(), v2_path.read_bytes(), author="CompareBot")
        assert cmp_res.applied == 1
        redlined_doc = Document(BytesIO(cmp_res.data))
        raw = _extract_text_from_doc(redlined_doc, clean_view=False, include_appendix=False)
        clean = _extract_text_from_doc(redlined_doc, clean_view=True, include_appendix=False)
        assert "Clause Inserted." in raw
        # Clauses Beta, Gamma, Delta were NOT modified — no tracked changes inside them
        assert "{--" not in raw
        assert clean == (
            "1. Clause Alpha.\n\n2. Clause Inserted.\n\n3. Clause Beta.\n\n4. Clause Gamma.\n\n5. Clause Delta."
        )

        # Also verify `adeu diff --json` -> `adeu apply` when deleting item 3
        v3 = Document()
        v3.add_paragraph("Clause Alpha.", style="List Number")
        v3.add_paragraph("Clause Beta.", style="List Number")
        v3.add_paragraph("Clause Delta.", style="List Number")
        v3_path = tmp_path / "v3.docx"
        v3.save(str(v3_path))

        diff_proc = run_cli("diff", v1_path, v3_path, "--json")
        assert diff_proc.returncode == 0, diff_proc.stderr
        edits_path = tmp_path / "edits.json"
        edits_path.write_text(diff_proc.stdout, encoding="utf-8")
        applied_path = tmp_path / "v1_applied.docx"
        apply_proc = run_cli("apply", v1_path, edits_path, "-o", applied_path, "--json")
        assert apply_proc.returncode == 0, apply_proc.stderr
        stats = json.loads(apply_proc.stdout.strip().splitlines()[-1])
        assert stats["edits_applied"] == 1
        assert _extract_text_from_doc(Document(str(applied_path)), clean_view=True, include_appendix=False) == (
            "1. Clause Alpha.\n\n2. Clause Beta.\n\n3. Clause Delta."
        )

    def test_apply_text_revision_handles_numbered_list_deletion(self, tmp_path: Path):
        doc = Document()
        doc.add_paragraph("Clause Alpha with sufficient body prose to avoid major deletion guard.", style="List Number")
        doc.add_paragraph("Clause Beta with sufficient body prose to keep length well above half.", style="List Number")
        doc.add_paragraph(
            "Clause Gamma with sufficient body prose to keep length well above half.", style="List Number"
        )
        src_path = tmp_path / "numbered.docx"
        doc.save(str(src_path))

        # User extracts clean text ("1. ...\n\n2. ...\n\n3. ...") and deletes Clause 1
        # without manually renumbering lines 2 and 3.
        extracted = _extract_text_from_doc(Document(str(src_path)), clean_view=True, include_appendix=False)
        revised = extracted.split("\n\n", 1)[1]
        assert revised.startswith("2. Clause Beta")

        out_path = tmp_path / "numbered_revised.docx"
        stats, saved_path = apply_text_revision_core(src_path, revised, output_path=out_path, author="Editor")
        assert saved_path == out_path
        assert stats["edits_applied"] == 1
        final_clean = _extract_text_from_doc(Document(str(out_path)), clean_view=True, include_appendix=False)
        assert final_clean.startswith("1. Clause Beta")
        assert "2. Clause Gamma" in final_clean


class TestCliExtractMultiPageOutput:
    def _make_multipage_docx(self, path: Path) -> Path:
        doc = Document()
        for i in range(65):
            doc.add_paragraph(f"Clause {i:03d}: " + ("lorem ipsum dolor sit amet " * 28))
        doc.save(str(path))
        return path

    def test_extract_dash_o_defaults_to_full_document_without_page_truncation(self, tmp_path: Path):
        doc_path = self._make_multipage_docx(tmp_path / "multi.docx")
        out_md = tmp_path / "multi.md"

        proc = run_cli("extract", doc_path, "--clean-view", "-o", out_md)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        content = out_md.read_text(encoding="utf-8")
        assert "Clause 000:" in content
        assert "Clause 064:" in content
        assert "Page 1 of" not in content
        assert "Continues on page" not in content

        # Round-trips cleanly into `adeu apply` with zero edits
        applied_docx = tmp_path / "roundtrip.docx"
        apply_proc = run_cli("apply", doc_path, out_md, "-o", applied_docx, "--json")
        assert apply_proc.returncode == 0, apply_proc.stderr
        stats = json.loads(apply_proc.stdout.strip().splitlines()[-1])
        assert stats["edits_applied"] == 0

    def test_extract_stdout_and_dash_o_dash_still_paginate_to_page_1(self, tmp_path: Path):
        doc_path = self._make_multipage_docx(tmp_path / "multi.docx")

        proc_stdout = run_cli("extract", doc_path)
        assert proc_stdout.returncode == 0
        assert "Page 1 of" in proc_stdout.stdout
        assert "Clause 064:" not in proc_stdout.stdout

        proc_dash = run_cli("extract", doc_path, "-o", "-")
        assert proc_dash.returncode == 0
        assert "Page 1 of" in proc_dash.stdout
        assert "Clause 064:" not in proc_dash.stdout

    def test_extract_explicit_partial_page_to_file_warns_on_stderr(self, tmp_path: Path):
        doc_path = self._make_multipage_docx(tmp_path / "multi.docx")
        p1_md = tmp_path / "page1.md"

        proc = run_cli("extract", doc_path, "--page", "1", "-o", p1_md)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "page 1 of" in proc.stderr.lower()
        assert "--page all" in proc.stderr
        assert "Clause 064:" not in p1_md.read_text(encoding="utf-8")

        range_md = tmp_path / "page1_2.md"
        proc_range = run_cli("extract", doc_path, "--page", "1-2", "-o", range_md)
        assert proc_range.returncode == 0
        assert "1-2 of" in proc_range.stderr.lower()
        assert "--page all" in proc_range.stderr
