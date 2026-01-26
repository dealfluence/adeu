import io

import pytest
from docx import Document
from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import XmlPart
from docx.oxml import parse_xml

from adeu.redline.engine import RedlineEngine


def test_repro_comments_namespace_xml_syntax_error():
    """
    Regression test for a bug where _ensure_namespaces constructed a malformed
    <w:comments> tag (missing closing '>') when patching namespaces.
    """
    doc = Document()
    doc.add_paragraph("Test content")

    # 1. Manually create a 'defective' comments part (missing w14/w15/Ignorable)
    # This forces _ensure_namespaces to trigger its regex replacement logic.
    # We use a minimal XML that lacks the modern namespaces Adeu requires.
    comments_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        b'  <w:comment w:id="1" w:author="Tester" w:date="2026-01-26T10:00:00Z">\n'
        b"    <w:p><w:r><w:t>Comment</w:t></w:r></w:p>\n"
        b"  </w:comment>\n"
        b"</w:comments>"
    )

    # Inject into package manually to bypass python-docx defaults
    partname = doc.part.package.next_partname("/word/comments%d.xml")
    part = XmlPart(partname, CT.WML_COMMENTS, parse_xml(comments_xml), doc.part.package)
    doc.part.package.parts.append(part)
    doc.part.relate_to(part, RT.COMMENTS)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # 2. Initialize Engine/Manager
    # This calls CommentsManager.__init__ -> _ensure_namespaces
    # Before the fix, this raised:
    # XMLSyntaxError: Couldn't find end of Start Tag comments line 1, line 2, column 1
    try:
        engine = RedlineEngine(stream)
    except Exception as e:
        pytest.fail(f"CommentsManager crashed on init during namespace patching: {e}")

    # 3. Verify the patch was applied correctly
    # The root element should now have the namespaces and valid syntax
    comments_part = engine.comments_manager.comments_part
    xml_str = comments_part.blob.decode("utf-8")

    # Check for presence of injected attributes
    assert 'xmlns:w15="' in xml_str
    assert 'mc:Ignorable="w14 w15 w16cid w16cex"' in xml_str

    # Ensure the tag was closed correctly (no syntax error implies this, but good to check string)
    # We expect the start tag to end with >
    # A rough check:
    assert 'mc:Ignorable="w14 w15 w16cid w16cex">' in xml_str or 'mc:Ignorable="w14 w15 w16cid w16cex" >' in xml_str
