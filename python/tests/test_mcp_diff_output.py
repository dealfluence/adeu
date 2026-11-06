import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.mcp_components.tools.document import diff_docx_files
from adeu.redline.engine import RedlineEngine
from tests.utils import run_cli


@pytest.mark.anyio
async def test_mcp_diff_docx_files_with_output_path_saves_valid_redline(tmp_path: Path):
    v1_path = tmp_path / "v1.docx"
    v2_path = tmp_path / "v2.docx"
    out_path = tmp_path / "redline.docx"

    d1 = Document()
    d1.add_paragraph("1. Governing Law. This Agreement is governed by Delaware law.")
    d1.add_paragraph("2. Payment Terms. Invoices are payable within thirty (30) days.")
    d1.save(v1_path)

    d2 = Document()
    d2.add_paragraph("1. Governing Law. This Agreement is governed by New York law.")
    d2.add_paragraph("2. Payment Terms. Invoices are payable within sixty (60) days.")
    d2.add_paragraph("3. Confidentiality. Both parties shall keep terms confidential.")
    d2.save(v2_path)

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()

    fn = getattr(diff_docx_files, "fn", diff_docx_files)
    res = await fn(
        original_path=str(v1_path),
        modified_path=str(v2_path),
        ctx=ctx,
        compare_clean=True,
        output_path=str(out_path),
        author_name="Counterparty Counsel",
    )

    assert out_path.exists()
    assert "Redline saved to:" in res
    out_bytes = out_path.read_bytes()

    # Accepted view matches v2
    assert extract_text_from_stream(io.BytesIO(out_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(v2_path.read_bytes()), clean_view=True
    )

    # Rejected view matches v1
    eng_rej = RedlineEngine(io.BytesIO(out_bytes))
    eng_rej.reject_all_revisions()
    rej_bytes = eng_rej.save_to_stream().getvalue()
    assert extract_text_from_stream(io.BytesIO(rej_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(v1_path.read_bytes()), clean_view=True
    )


def test_cli_diff_with_docx_output_writes_redlined_docx(tmp_path: Path):
    v1_path = tmp_path / "orig.docx"
    v2_path = tmp_path / "rev.docx"
    out_path = tmp_path / "comparison.docx"

    d1 = Document()
    d1.add_paragraph("Alpha clause with original wording.")
    d1.save(v1_path)

    d2 = Document()
    d2.add_paragraph("Alpha clause with revised wording.")
    d2.save(v2_path)

    res = run_cli("diff", str(v1_path), str(v2_path), "-o", str(out_path), "--author", "Deal Team")
    assert res.returncode == 0
    assert out_path.exists()
    out_bytes = out_path.read_bytes()
    assert extract_text_from_stream(io.BytesIO(out_bytes), clean_view=True) == extract_text_from_stream(
        io.BytesIO(v2_path.read_bytes()), clean_view=True
    )
