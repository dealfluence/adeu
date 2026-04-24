import io

import pytest
from docx import Document
from docx.oxml.ns import qn

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def create_doc_with_bold_run():
    doc = Document()
    p = doc.add_paragraph()
    r = p.add_run("This is bold text.")
    r.bold = True
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


class TestQaReportDefects:
    def test_h1_insertion_inherits_bold(self):
        """
        H1: Plain text insertion into a bold run explicitly emits <w:b w:val="0"/>
        instead of inheriting the bold context.
        """
        stream = create_doc_with_bold_run()
        engine = RedlineEngine(stream)
        # new_text starts with target_text to bypass trim_common_context string diffs
        edit = ModifyText(target_text="bold text.", new_text="bold text. Plain suffix.")
        engine.process_batch([edit])

        ins_elements = engine.doc.element.xpath("//w:ins")
        assert len(ins_elements) >= 1

        for ins in ins_elements:
            for run in ins.findall(qn("w:r")):
                rPr = run.find(qn("w:rPr"))
                if rPr is not None:
                    b_tag = rPr.find(qn("w:b"))
                    if b_tag is not None:
                        val = b_tag.get(qn("w:val"))
                        assert val != "0", "Insertion explicitly disabled bold inside a bold run (H1)"

    def test_h2_markdown_bold_inside_bold(self):
        """
        H2: Markdown **bold** emits bare <w:b/> inside already-bold context,
        which Word interprets as a toggle (turning bold OFF).
        """
        stream = create_doc_with_bold_run()
        engine = RedlineEngine(stream)
        edit = ModifyText(target_text="bold text.", new_text="bold text. **Spicy suffix**.")
        engine.process_batch([edit])

        ins_elements = engine.doc.element.xpath("//w:ins")
        for ins in ins_elements:
            for run in ins.findall(qn("w:r")):
                t_tag = run.find(qn("w:t"))
                if t_tag is not None and "Spicy suffix" in t_tag.text:
                    rPr = run.find(qn("w:rPr"))
                    if rPr is not None:
                        b_tag = rPr.find(qn("w:b"))
                        assert b_tag is not None, "Expected <w:b> tag for markdown bold"
                        val = b_tag.get(qn("w:val"))
                        assert val == "1", (
                            "Inside a bold context, **bold** must emit <w:b w:val='1'> to avoid toggle semantics (H2)"
                        )

    def test_h3_accept_all_removes_orphan_paragraphs(self):
        """
        H3: accept_all_changes leaves orphan blank paragraphs after full-paragraph deletions.
        """
        doc = Document()
        doc.add_heading("5. Exclusions", level=1)
        doc.add_paragraph("Following text.")
        stream = io.BytesIO()
        doc.save(stream)
        stream.seek(0)

        engine = RedlineEngine(stream)

        edit = ModifyText(target_text="# 5. Exclusions", new_text="## 5. Exclusions (Revised)\n\nNew paragraph.")
        engine.process_batch([edit])
        engine.accept_all_revisions()

        paragraphs = [p.text for p in engine.doc.paragraphs]
        # 1: Empty orphan (BUG), 2: Revised heading, 3: New paragraph, 4: Following text
        assert len(paragraphs) == 3, (
            f"Expected 3 paragraphs, but got {len(paragraphs)}. Orphan paragraph was left behind: {paragraphs}"
        )

    def test_h7_skipped_edits_report(self):
        """
        H7: Skipped-edit reporting is aggregate-only; callers cannot identify which edits were skipped.
        """
        doc = Document()
        doc.add_paragraph("Overlapping target.")
        stream = io.BytesIO()
        doc.save(stream)
        stream.seek(0)

        engine = RedlineEngine(stream)
        # Both edits target the same text. Edit 1 applies, Edit 2 is skipped due to overlap.
        # This passes validation but tests the post-validation skip path.
        edit1 = ModifyText(target_text="Overlapping target.", new_text="First edit.")
        edit2 = ModifyText(target_text="Overlapping target.", new_text="Second edit.")

        stats = engine.process_batch([edit1, edit2])

        assert stats["edits_applied"] == 1
        assert stats["edits_skipped"] == 1

        assert "skipped_details" in stats, "Engine must return detailed information about skipped edits (H7)"

    def test_h8_noop_modify_drops_comment(self):
        """
        H8: Silent comment drop. Comments attached to no-op modify edits are lost.
        """
        doc = Document()
        doc.add_paragraph("This is some text.")
        stream = io.BytesIO()
        doc.save(stream)
        stream.seek(0)

        engine = RedlineEngine(stream)
        edit = ModifyText(target_text="some text.", new_text="some text.", comment="This is a QA comment.")
        engine.process_batch([edit])

        comments = engine.comments_manager.extract_comments_data()
        assert len(comments) == 1, "Comment attached to no-op modify was silently dropped (H8)"

    def test_h9_table_row_multi_paragraph(self):
        """
        H9: Silent multi-paragraph-insert drop in table row contexts.
        """
        doc = Document()
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "Cell content."
        stream = io.BytesIO()
        doc.save(stream)
        stream.seek(0)

        engine = RedlineEngine(stream)
        edit = ModifyText(target_text="Cell content.", new_text="Cell content.\n\nNew paragraph.")

        stats = engine.process_batch([edit])

        if stats["edits_skipped"] > 0:
            pytest.fail("Edit was silently skipped. Must either apply or raise a validation error. (H9)")

        engine.accept_all_revisions()

        table_text = [p.text for row in engine.doc.tables[0].rows for cell in row.cells for p in cell.paragraphs]
        assert "New paragraph." in table_text, "Multi-paragraph insert into table silently failed (H9)"

    def test_m3_empty_comment_parts_not_created(self):
        """
        M3: Empty comment XML parts created on every modify edit, even when no comments are attached.
        """
        doc = Document()
        doc.add_paragraph("No comments here.")
        stream = io.BytesIO()
        doc.save(stream)
        stream.seek(0)

        engine = RedlineEngine(stream)
        edit = ModifyText(target_text="here.", new_text="here in this document.")
        engine.process_batch([edit])

        parts = engine.doc.part.package.parts
        comment_parts = [p for p in parts if "comments" in p.partname]
        assert len(comment_parts) == 0, "Comment XML parts should not be created if no comments are attached (M3)"
