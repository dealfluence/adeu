from pathlib import Path

import docx

from adeu.ingest import _extract_text_from_doc
from adeu.outline import extract_outline
from adeu.pagination import paginate


def create_manual_break_doc(path: Path):
    doc = docx.Document()
    doc.add_heading("Pagination Test Document", level=1)
    for page_num in range(1, 6):
        doc.add_heading(f"Heading on Page {page_num}", level=2)
        doc.add_paragraph(f"This is paragraph content belonging strictly to page {page_num}. " * 5)
        if page_num < 5:
            doc.add_page_break()
    doc.save(str(path))


def test_manual_page_breaks_pagination(tmp_path):
    doc_path = tmp_path / "manual_breaks.docx"
    create_manual_break_doc(doc_path)

    doc = docx.Document(str(doc_path))
    projected_body = _extract_text_from_doc(doc, include_appendix=False)

    # Run paginate
    pag_res = paginate(projected_body)

    # The document must have 5 pages
    assert pag_res.total_pages == 5, f"Expected 5 pages, got {pag_res.total_pages}"

    # Assert each page has the correct heading
    for page_num in range(1, 6):
        page_content = pag_res.pages[page_num - 1].page_content
        assert f"Heading on Page {page_num}" in page_content
        assert f"This is paragraph content belonging strictly to page {page_num}." in page_content
        # Ensure other page content is NOT leaked to this page
        for other_num in range(1, 6):
            if other_num != page_num:
                assert f"Heading on Page {other_num}" not in page_content


def test_manual_page_breaks_outline(tmp_path):
    doc_path = tmp_path / "manual_breaks.docx"
    create_manual_break_doc(doc_path)

    doc = docx.Document(str(doc_path))
    projected_body = _extract_text_from_doc(doc, include_appendix=False)

    pag_res = paginate(projected_body)

    # Get outline
    # extract_outline expects (doc, projected_body, body_pages, body_page_offsets, paragraph_offsets)
    body_pages = [p.page_content for p in pag_res.pages]
    body_page_offsets = pag_res.body_page_offsets

    nodes = extract_outline(doc, projected_body, body_pages, body_page_offsets)

    # We should have headings on pages 1 to 5
    headings = [node for node in nodes if node.level == 2]
    assert len(headings) == 5
    for i, node in enumerate(headings):
        expected_page = i + 1
        assert node.text == f"Heading on Page {expected_page}"
        assert node.page == expected_page, f"Expected {node.text} to be on page {expected_page}, got page {node.page}"
