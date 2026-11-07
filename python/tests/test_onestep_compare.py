"""One-step compare: structured diff edits applied in-process (pins kept) must equal the JSON round trip.

`generate_structured_edits` widens ambiguous hunks with surrounding context (make_edits_self_contained) and keeps
their positional pins. In-process callers (`adeu diff -o`, MCP `diff_docx_files(output_path=...)`, sanitize) apply
those pinned edits directly. A widened target that crossed a bold run and a paragraph break was applied raw: the
shared context was deleted and re-inserted with literal `**`, and rejecting every revision merged two headings.
"""

import io
import zipfile
from pathlib import Path

import pytest
from docx import Document
from fastmcp.exceptions import ToolError

from adeu.diff import generate_structured_edits
from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.mcp_components.tools.document import diff_docx_files
from adeu.redline.engine import RedlineEngine
from tests.utils import get_mock_ctx, run_cli


def _confirmation(suffix: str) -> bytes:
    d = Document()
    d.add_paragraph().add_run("5. Close-out and Early Termination").bold = True
    d.add_paragraph().add_run("5.1 Close-out Amount" + suffix).bold = True
    d.add_paragraph("The Close-out Amount shall be determined by the Determining Party.")
    # A second "5.1 Close-out Amount" makes the bare hunk ambiguous, so the diff widens it across the heading
    # break: 'Termination**\n\n**5.1 Close-out Amount'.
    d.add_paragraph("References to 5.1 Close-out Amount include any successor provision.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _structured_edits(v1: bytes, v2: bytes):
    t1, s1 = _extract_text_from_doc(
        Document(io.BytesIO(v1)), clean_view=True, include_appendix=False, return_structure=True
    )
    t2, s2 = _extract_text_from_doc(
        Document(io.BytesIO(v2)), clean_view=True, include_appendix=False, return_structure=True
    )
    edits, _ = generate_structured_edits(t1, s1, t2, s2)  # type: ignore[arg-type]
    return edits


def _clean(b: bytes) -> str:
    return extract_text_from_stream(io.BytesIO(b), clean_view=True)


def _rejected(b: bytes) -> str:
    eng = RedlineEngine(io.BytesIO(b))
    eng.reject_all_revisions()
    return _clean(eng.save_to_stream().getvalue())


def test_widened_pinned_edit_across_bold_heading_break_applies_as_the_minimal_insertion():
    v1, v2 = _confirmation(""), _confirmation(" — Mandatory Dealer Poll")
    edits = _structured_edits(v1, v2)
    assert any("\n\n" in (e.target_text or "") for e in edits), "fixture no longer produces a widened target"

    eng = RedlineEngine(io.BytesIO(v1), author="Reviewer")
    applied, skipped = eng.apply_edits(edits)  # type: ignore[arg-type]
    assert (applied, skipped) == (len(edits), 0)
    out = eng.save_to_stream().getvalue()

    assert _clean(out) == _clean(v2)
    assert _rejected(out) == _clean(v1)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        body = z.read("word/document.xml").decode("utf-8")
    assert "**" not in body
    # Only the suffix is new: the shared heading text is never deleted and re-inserted.
    assert "<w:del " not in body and "<w:del>" not in body


# --- the one-step surfaces: transactional, never over an input ---------------------------------------------------


@pytest.fixture
def pair(tmp_path: Path) -> "tuple[Path, Path]":
    v1, v2 = tmp_path / "v1.docx", tmp_path / "v2.docx"
    v1.write_bytes(_confirmation(""))
    v2.write_bytes(_confirmation(" — Mandatory Dealer Poll"))
    return v1, v2


async def _mcp_diff(v1: Path, v2: Path, out: Path):
    fn = getattr(diff_docx_files, "fn", diff_docx_files)
    return await fn(
        original_path=str(v1), modified_path=str(v2), ctx=get_mock_ctx(), output_path=str(out), author_name="Rev"
    )


@pytest.mark.anyio
async def test_mcp_one_step_compare_matches_the_modified_document(pair, tmp_path: Path):
    v1, v2 = pair
    out = tmp_path / "redline.docx"
    res = await _mcp_diff(v1, v2, out)
    assert "Redline saved to:" in res
    assert _clean(out.read_bytes()) == _clean(v2.read_bytes())
    assert _rejected(out.read_bytes()) == _clean(v1.read_bytes())


@pytest.mark.anyio
@pytest.mark.parametrize("which", ["original", "modified"])
async def test_mcp_one_step_compare_refuses_to_overwrite_an_input(pair, which: str):
    v1, v2 = pair
    target = v1 if which == "original" else v2
    before = target.read_bytes()
    with pytest.raises(ToolError, match=f"same file as the {which} document"):
        await _mcp_diff(v1, v2, target)
    assert target.read_bytes() == before


@pytest.mark.anyio
async def test_mcp_one_step_compare_writes_nothing_when_a_change_is_skipped(pair, tmp_path: Path, monkeypatch):
    v1, v2 = pair
    out = tmp_path / "redline.docx"
    # Fault injection at the engine boundary: an apply-stage skip the diff could not foresee.
    monkeypatch.setattr(RedlineEngine, "apply_edits", lambda self, edits, *a, **k: (len(edits) - 1, 1))
    with pytest.raises(ToolError, match="could not be written as tracked changes"):
        await _mcp_diff(v1, v2, out)
    assert not out.exists()


def test_cli_one_step_compare_needs_no_author(pair, tmp_path: Path):
    v1, v2 = pair
    out = tmp_path / "redline.docx"
    res = run_cli("diff", v1, v2, "-o", out)
    assert res.returncode == 0, res.stderr
    assert _clean(out.read_bytes()) == _clean(v2.read_bytes())
    assert _rejected(out.read_bytes()) == _clean(v1.read_bytes())


@pytest.mark.parametrize("extra", [[], ["--author", "Deal Team"]])
def test_cli_one_step_compare_refuses_to_overwrite_the_original(pair, extra):
    v1, v2 = pair
    before = v1.read_bytes()
    res = run_cli("diff", v1, v2, "-o", v1, *extra)
    assert res.returncode == 1
    assert "same file as the original" in res.stderr
    assert v1.read_bytes() == before
