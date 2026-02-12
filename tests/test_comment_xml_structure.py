"""
Unit tests for Word XML structure compliance in comments.

These tests verify that Adeu generates XML structures that match Word's expected format,
isolated from fixture dependencies.

"""

import io
from xml.etree import ElementTree as ET

from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine


def create_minimal_docx():
    """Creates a minimal DOCX in memory for testing."""
    from docx import Document

    doc = Document()
    doc.add_paragraph("This is the initial document")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def get_comments_xml(engine: RedlineEngine) -> ET.Element:
    """Extract and parse the comments.xml from the engine."""
    return engine.comments_manager.comments_part.element


def get_document_xml(engine: RedlineEngine) -> ET.Element:
    """Extract and parse the main document.xml from the engine."""
    return engine.doc.element


class TestCommentAnnotationRefFormatting:
    """Test Problem 1: Missing <w:rPr> wrapper with CommentReference style for w:annotationRef"""

    def test_annotation_ref_has_comment_reference_style(self):
        """
        Word wraps <w:annotationRef/> inside a run with CommentReference style.

        Expected structure in comments.xml:
        <w:r>
          <w:rPr>
            <w:rStyle w:val="CommentReference"/>
          </w:rPr>
          <w:annotationRef/>
        </w:r>

        Current bug: Missing <w:rPr> wrapper entirely.
        Root cause: comments.py:327-333 creates annotationRef run without styling.
        """
        stream = create_minimal_docx()
        engine = RedlineEngine(stream, author="Test Author")

        edit = DocumentEdit(
            target_text="initial",
            new_text="modified",
            comment="Test comment",
        )
        engine.apply_edits([edit])

        comments_xml = get_comments_xml(engine)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        # Find all runs containing annotationRef
        annotation_runs = []
        for comment in comments_xml.findall(".//w:comment", ns):
            for run in comment.findall(".//w:r", ns):
                if run.find("w:annotationRef", ns) is not None:
                    annotation_runs.append(run)

        assert len(annotation_runs) > 0, "No runs with annotationRef found"

        # Check each annotationRef run has proper styling
        for run in annotation_runs:
            rPr = run.find("w:rPr", ns)
            assert rPr is not None, "annotationRef run missing <w:rPr>"

            rStyle = rPr.find("w:rStyle", ns)
            assert rStyle is not None, "annotationRef run missing <w:rStyle>"
            assert (
                rStyle.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") == "CommentReference"
            ), "annotationRef rStyle should be 'CommentReference'"


class TestRevisionDateAttributes:
    """Test Problem 4: Missing w16du:dateUtc attribute on revision tags"""

    def test_insertion_has_date_utc_attribute(self):
        """
        Word adds both w:date and w16du:dateUtc to <w:ins> elements.

        Expected: <w:ins w:id="X" w:author="..." w:date="..." w16du:dateUtc="...">
        Current bug: Missing w16du:dateUtc attribute.

        Root cause: Revision insertion code only sets w:date, doesn't add w16du:dateUtc namespace/attribute.
        Impact: Modern Word versions expect both date formats; missing dateUtc may cause compatibility issues.
        """
        stream = create_minimal_docx()
        engine = RedlineEngine(stream, author="Test Author")

        edit = DocumentEdit(
            target_text="initial",
            new_text="modified",
        )
        engine.apply_edits([edit])

        doc_xml = get_document_xml(engine)
        ns = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "w16du": "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
        }

        insertions = doc_xml.findall(".//w:ins", ns)
        assert len(insertions) > 0, "No insertions found"

        for ins in insertions:
            w_date = ins.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}date")
            assert w_date is not None, "w:ins missing w:date attribute"

            # Check for w16du:dateUtc (Word modern versions add this)
            date_utc = ins.get("{http://schemas.microsoft.com/office/word/2023/wordml/word16du}dateUtc")
            assert date_utc is not None, "w:ins missing w16du:dateUtc attribute"

    def test_deletion_has_date_utc_attribute(self):
        """
        Word adds both w:date and w16du:dateUtc to <w:del> elements.

        Expected: <w:del w:id="X" w:author="..." w:date="..." w16du:dateUtc="...">
        Current bug: Missing w16du:dateUtc attribute.
        """
        stream = create_minimal_docx()
        engine = RedlineEngine(stream, author="Test Author")

        edit = DocumentEdit(
            target_text="initial",
            new_text="modified",
        )
        engine.apply_edits([edit])

        doc_xml = get_document_xml(engine)
        ns = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "w16du": "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
        }

        deletions = doc_xml.findall(".//w:del", ns)
        assert len(deletions) > 0, "No deletions found"

        for dels in deletions:
            w_date = dels.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}date")
            assert w_date is not None, "w:del missing w:date attribute"

            date_utc = dels.get("{http://schemas.microsoft.com/office/word/2023/wordml/word16du}dateUtc")
            assert date_utc is not None, "w:del missing w16du:dateUtc attribute"
