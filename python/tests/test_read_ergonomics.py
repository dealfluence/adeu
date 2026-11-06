import io
from pathlib import Path

import docx

from adeu.ingest import extract_text_from_stream
from adeu.mcp_components._response_builders import (
    build_outline_response,
    build_page_range_response,
)
from adeu.mcp_components.tools.document import read_docx
from adeu.pagination import parse_page_arg
from tests.fixtures_synth import build_long_docx
from tests.utils import extract_content, get_mock_ctx, run_async


def test_parse_page_arg_strips_surrounding_quotes():
    assert parse_page_arg("'all'") == ("all", None)
    assert parse_page_arg('"all"') == ("all", None)
    assert parse_page_arg("'2-6'") == ("range", (2, 6))
    assert parse_page_arg('"3"') == ("single", 3)


def test_read_docx_search_accepts_page_range_and_quoted_page(tmp_path: Path):
    doc_path = tmp_path / "multi.docx"
    build_long_docx(doc_path, pages=6)

    # Search with page="2-4" in MCP read_docx filters hits to pages 2..4
    res = run_async(
        read_docx(
            reasoning="test search page range",
            ctx=get_mock_ctx(),
            file_path=str(doc_path),
            search_query="Section",
            page="2-4",
        )
    )
    content = extract_content(res)
    assert "Search Results" in content
    assert "pages 2-4" in content
    assert "(p2)" in content or "(p3)" in content or "(p4)" in content
    assert "(p1)" not in content
    assert "(p5)" not in content

    # Quoted "'all'" in search mode
    res_all = run_async(
        read_docx(
            reasoning="test search quoted all",
            ctx=get_mock_ctx(),
            file_path=str(doc_path),
            search_query="Section",
            page="'all'",
        )
    )
    content_all = extract_content(res_all)
    assert "Search Results" in content_all
    assert "(p1)" in content_all


def test_page_range_full_window_emits_continuation_footer(tmp_path: Path):
    doc_path = tmp_path / "doc14.docx"
    build_long_docx(doc_path, pages=14)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    res_mcp = build_page_range_response(text, 1, 8, str(doc_path), is_cli=False)
    content_mcp = str(res_mcp.content)
    assert "Continues on page 9 of 14" in content_mcp
    assert 'page="9-14"' in content_mcp

    res_cli = build_page_range_response(text, 1, 8, str(doc_path), is_cli=True)
    content_cli = str(res_cli.content)
    assert "Continues on page 9 of 14" in content_cli
    assert "--page 9-14" in content_cli


def test_outline_detects_standalone_bold_section_headers(tmp_path: Path):
    doc = docx.Document()
    p_title = doc.add_paragraph()
    r_title = p_title.add_run("MASTER SERVICES AGREEMENT")
    r_title.bold = True

    p_art = doc.add_paragraph()
    r_art = p_art.add_run("Article I — Definitions")
    r_art.bold = True

    p_sec1 = doc.add_paragraph()
    r_sec1 = p_sec1.add_run("Paragraph 1 — Services and Exclusivity")
    r_sec1.bold = True

    doc.add_paragraph("The Service Provider shall provide the services described herein.")

    p_sec2 = doc.add_paragraph()
    r_sec2 = p_sec2.add_run("Section 2.01 — Purchase Orders")
    r_sec2.bold = True

    doc.add_paragraph("Each purchase order shall be in writing.")

    p_sec3 = doc.add_paragraph()
    r_sec3 = p_sec3.add_run("3. Term and Termination")
    r_sec3.bold = True

    # A bold run-in paragraph ending in a period must NOT be treated as a heading
    p_runin = doc.add_paragraph()
    r_runin_b = p_runin.add_run("3.1 Initial Term. ")
    r_runin_b.bold = True
    p_runin.add_run("This Agreement commences on the Effective Date.")

    doc_path = tmp_path / "bold_sections.docx"
    doc.save(str(doc_path))

    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))
    res = build_outline_response(doc, text, str(doc_path))
    content = str(res.content)

    assert "# Article I — Definitions" in content
    assert "## Paragraph 1 — Services and Exclusivity" in content
    assert "## Section 2.01 — Purchase Orders" in content
    assert "## 3. Term and Termination" in content
    assert "Initial Term" not in content
