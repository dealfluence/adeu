import io
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from adeu.ingest import extract_text_from_stream
from adeu.mcp_components._response_builders import BuilderError, build_page_range_response, build_paginated_response
from adeu.mcp_components.tools.document import read_docx
from adeu.pagination import parse_page_arg
from tests.fixtures_synth import build_long_docx
from tests.utils import approx_tokens, extract_content, get_mock_ctx, run_async


def test_mid_range_returns_all_pages_in_one_response(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    res = build_page_range_response(text, 2, 4, str(doc_path))
    content = str(res.content)

    assert "**Page 2 of 6**" in content
    assert "**Page 3 of 6**" in content
    assert "**Page 4 of 6**" in content
    assert "**Page 1 of 6**" not in content
    assert "**Page 5 of 6**" not in content


def test_cap_at_eight_pages_appends_continue_note(tmp_path: Path):
    doc_path = tmp_path / "doc12.docx"
    build_long_docx(doc_path, pages=12)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    res_cli = build_page_range_response(text, 1, 12, str(doc_path), is_cli=True)
    content_cli = str(res_cli.content)
    assert "**Range capped at 8 pages.**" in content_cli
    assert "Continue with `--page 9-12`." in content_cli

    res_mcp = build_page_range_response(text, 1, 12, str(doc_path), is_cli=False)
    content_mcp = str(res_mcp.content)
    assert "**Range capped at 8 pages.**" in content_mcp
    assert 'Continue with `page="9-12"`.' in content_mcp


def test_range_past_end_stops_and_reports_real_page_count(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    res = build_page_range_response(text, 5, 99, str(doc_path))
    content = str(res.content)
    assert "**Page 5 of 6**" in content
    assert "**Page 6 of 6**" in content
    assert "[range stopped at page 6: the document has 6 page(s)]" in content


def test_start_past_end_is_a_builder_error(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    with pytest.raises(BuilderError) as exc_info:
        build_page_range_response(text, 9, 10, str(doc_path))
    assert "out of range" in str(exc_info.value)


def test_cli_page_range(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)

    cmd = [sys.executable, "-m", "adeu.cli", "extract", str(doc_path), "--page", "2-3"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0
    assert "**Page 2 of 6**" in result.stdout
    assert "**Page 3 of 6**" in result.stdout
    assert "**Page 1 of 6**" not in result.stdout


def test_cli_range_rejected_with_search_query(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)

    cmd = [
        sys.executable,
        "-m",
        "adeu.cli",
        "extract",
        str(doc_path),
        "--page",
        "2-3",
        "--search-query",
        "Section",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 2
    assert "Page ranges (e.g. '2-3') are not supported with --search-query." in result.stderr


def test_range_token_parity(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    res_range = build_page_range_response(text, 2, 4, str(doc_path))
    tokens_range = approx_tokens(str(res_range.content))

    p2 = build_paginated_response(text, 2, str(doc_path))
    p3 = build_paginated_response(text, 3, str(doc_path))
    p4 = build_paginated_response(text, 4, str(doc_path))

    sum_tokens = approx_tokens(str(p2.content)) + approx_tokens(str(p3.content)) + approx_tokens(str(p4.content))
    assert tokens_range <= sum_tokens


def test_mcp_page_range_string(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)

    res = run_async(read_docx(reasoning="test", ctx=get_mock_ctx(), file_path=str(doc_path), page="2-3"))
    content = extract_content(res)
    assert "**Page 2 of 6**" in content
    assert "**Page 3 of 6**" in content


def test_start_greater_than_end_raises_builder_error(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)
    text = extract_text_from_stream(io.BytesIO(doc_path.read_bytes()))

    with pytest.raises(BuilderError) as exc_info:
        build_page_range_response(text, 5, 2, str(doc_path))
    assert "end page (2) cannot be less than start page (5)" in str(exc_info.value)


def test_mcp_invalid_page_string_raises_tool_error(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)

    with pytest.raises(ToolError) as exc_info:
        run_async(read_docx(reasoning="test", ctx=get_mock_ctx(), file_path=str(doc_path), page="banana"))
    assert "Invalid page parameter: 'banana'" in str(exc_info.value)


def test_appendix_mode_with_page_range_raises_error(tmp_path: Path):
    doc_path = tmp_path / "doc6.docx"
    build_long_docx(doc_path, pages=6)

    # MCP path
    with pytest.raises(ToolError) as exc_info:
        run_async(read_docx(reasoning="test", ctx=get_mock_ctx(), file_path=str(doc_path), mode="appendix", page="1-3"))
    assert "Page range pagination is only supported in 'full' mode, not 'appendix' mode." in str(exc_info.value)

    # CLI path
    cmd = [
        sys.executable,
        "-m",
        "adeu.cli",
        "extract",
        str(doc_path),
        "--mode",
        "appendix",
        "--page",
        "1-3",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 2
    assert "Page range pagination is only supported in 'full' mode, not 'appendix' mode." in result.stderr


def test_parse_page_arg_valid_and_invalid_inputs():
    # Valid inputs
    assert parse_page_arg(None) == ("single", 1)
    assert parse_page_arg(1) == ("single", 1)
    assert parse_page_arg(5) == ("single", 5)
    assert parse_page_arg("1") == ("single", 1)
    assert parse_page_arg(" 3 ") == ("single", 3)
    assert parse_page_arg("all") == ("all", None)
    assert parse_page_arg("ALL") == ("all", None)
    assert parse_page_arg("2-6") == ("range", (2, 6))
    assert parse_page_arg(" 2 - 6 ") == ("range", (2, 6))

    # Invalid inputs
    for invalid in [0, -1, "0", "-5", "0-5", "banana", ""]:
        with pytest.raises(ValueError) as exc_info:
            parse_page_arg(invalid)
        assert f"Invalid page parameter: '{invalid}'" in str(exc_info.value)
