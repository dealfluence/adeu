# FILE: tests/test_repro_live_run_fragmentation.py
"""
Regression test for the bug where live Word emits fragmented runs
(e.g. "N" + "ew Item Setup Fee." with identical rPr), which the
coalescing logic rendered as "**N****ew Item Setup Fee.**".

Fix C (parity): both ingest.py and mapper.py must coalesce adjacent runs
with matching wrappers AND matching style markers.
"""

import io

from adeu.redline.mapper import DocumentMapper

RPR_BOLD = "<w:rPr><w:b/><w:bCs/></w:rPr>"
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'


def _build_docx_with_fragmented_runs() -> io.BytesIO:
    """Builds a minimal .docx where 'New' is split as two adjacent bold runs."""
    import zipfile

    doc_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {W_NS}>
  <w:body>
    <w:p>
      <w:r>{RPR_BOLD}<w:t>3.4 N</w:t></w:r>
      <w:r>{RPR_BOLD}<w:t xml:space="preserve">ew Item Setup Fee.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>""".encode("utf-8")

    rels = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1"'
        b' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        b' Target="word/document.xml"/>'
        b"</Relationships>"
    )

    ct = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels"'
        b' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Override PartName="/word/document.xml"'
        b' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc_xml)
    buf.seek(0)
    return buf


def test_ingest_coalesces_adjacent_same_style_runs():
    from adeu.ingest import extract_text_from_stream

    stream = _build_docx_with_fragmented_runs()
    text = extract_text_from_stream(stream, clean_view=False)
    assert "**3.4 New Item Setup Fee.**" in text, text
    assert "****" not in text, f"Double-marker leak detected — coalescing failed:\n{text!r}"


def test_mapper_coalesces_adjacent_same_style_runs():
    from docx import Document

    stream = _build_docx_with_fragmented_runs()
    doc = Document(stream)
    # Skip normalize_docx here to prove mapper.py coalesces on its own —
    # this asserts parity even when normalization hasn't run.
    mapper = DocumentMapper(doc)
    assert "**3.4 New Item Setup Fee.**" in mapper.full_text, mapper.full_text
    assert "****" not in mapper.full_text, mapper.full_text


def test_mapper_find_match_across_fragmented_run():
    """The match offset must land inside the real span, not inside virtual markers."""
    from docx import Document

    stream = _build_docx_with_fragmented_runs()
    doc = Document(stream)
    mapper = DocumentMapper(doc)
    start, length = mapper.find_match_index("New Item Setup Fee")
    assert start != -1, f"couldn't find target in {mapper.full_text!r}"
    matched = mapper.full_text[start : start + length]
    assert matched == "New Item Setup Fee", matched
