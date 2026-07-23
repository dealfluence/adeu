# FILE: tests/test_repro_qa_mcp_2026_07_23_mcp.py
"""
Failing repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box QA of the
Node MCP server, v1.29.0+4bb70f9) — Python MCP-surface mirror.

Each finding from the Node QA report was re-verified against the PYTHON MCP
tool surface (adeu.mcp_components.tools.document) by calling the real tool
coroutines. Findings covered here:

- F11: Error messages recommend CLI commands inside MCP responses.
  Reproduces. Stale accept/reject/reply ids surface the engine's
  "Run `adeu markup <file> -i` or `adeu extract <file>`" hint
  (redline/engine.py, _action_not_found_error) verbatim through the
  process_document_batch "Batch rejected" response. MCP callers cannot run
  the CLI; the advice at this boundary must name `read_docx`. (The same
  engine string is correct when surfaced through the CLI — these tests
  assert only at the MCP tool boundary.)

- F16: Missing-file error for a RELATIVE path never says absolute paths are
  required. Partially reproduces. Unlike Node, Python echoes the path
  exactly as given (directory included) — that half is fine — but the
  read_docx parameter contract says "Absolute path to the DOCX file." and
  a relative path only resolves against the arbitrary server cwd. The
  error (shared.py read_file_bytes -> ToolError) must tell the caller that
  absolute paths are required. (The CLI-mapping escape hatch for sandboxed
  hosts in the same message is deliberate and NOT under test.)

- F17: Silent overwrites. Reproduces. process_document_batch overwrites an
  existing <stem>_processed.docx from a previous run, overwrites the INPUT
  file in place when its stem already ends in _processed, and overwrites
  the source when output_path == input path — in every case the response
  is a plain "Batch complete. Saved to: ..." with no overwrite disclosure
  (tools/document.py default-output logic + shared.py save_stream).

- F18: accept_all_changes on a change-free document. Reproduces. The
  response is "Accepted all changes. Saved to: <path>" — it claims action
  (with no counts at all, unlike Node) when nothing was accepted. A no-op
  must be reported as one.

- F19: Invalid .docx bytes error quality. Does NOT reproduce in Python —
  no test. read_docx on plain-text bytes, a truncated zip, and an empty
  file all raise ToolError("Error reading file: not a valid DOCX file
  (got bad zip signature)") via strip_bom_from_docx_bytes
  (utils/docx.py), which already satisfies the desired
  /not a valid .docx/ hint.

Every test is written test-first: it fails on current main and passes once
the finding is fixed.
"""

import asyncio
import io
import re

import pytest
from docx import Document
from fastmcp.exceptions import ToolError

from adeu.mcp_components.tools.document import (
    accept_all_changes,
    process_document_batch,
    read_docx,
)
from adeu.models import AcceptChange, ModifyText, ReplyComment
from adeu.redline.engine import RedlineEngine


class MockContext:
    """Mock FastMCP Context to absorb async logging calls during tests."""

    async def info(self, msg, **kwargs):
        pass

    async def debug(self, msg, **kwargs):
        pass

    async def warning(self, msg, **kwargs):
        pass

    async def error(self, msg, **kwargs):
        pass


def _make_plain_doc(path, text="The quick brown fox jumps over the lazy dog."):
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)
    return path


def _make_doc_with_tracked_change(tmp_path):
    """A document containing exactly one pending tracked replacement."""
    plain = tmp_path / "plain_src.docx"
    _make_plain_doc(plain)
    with open(plain, "rb") as f:
        engine = RedlineEngine(io.BytesIO(f.read()), author="Round1")
    engine.apply_edits([ModifyText(target_text="quick", new_text="swift")])
    tracked = tmp_path / "tracked.docx"
    with open(tracked, "wb") as f:
        f.write(engine.save_to_stream().getvalue())
    return tracked


# ---------------------------------------------------------------------------
# F11 — CLI commands recommended inside MCP responses
# ---------------------------------------------------------------------------


def test_f11_stale_change_id_error_advises_read_docx_not_cli(tmp_path):
    """
    F11: accept on a nonexistent change id through the REAL
    process_document_batch tool. Current response ends with
    "Run `adeu markup <file> -i` or `adeu extract <file>` to list the
    current change (Chg:) and comment (Com:) ids." — CLI commands an MCP
    caller cannot run. The advice at the MCP boundary must name read_docx.
    """
    tracked = _make_doc_with_tracked_change(tmp_path)

    res = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(tracked),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[AcceptChange(target_id="99")],
        )
    )

    # Sanity (passes today): we hit the stale-id diagnostic path.
    assert "Batch rejected" in res
    assert "no tracked change with that id exists" in res

    # The finding (fails today): CLI-isms inside an MCP response.
    assert "adeu markup" not in res, f"MCP response recommends the CLI:\n{res}"
    assert "adeu extract" not in res, f"MCP response recommends the CLI:\n{res}"
    assert "read_docx" in res, f"MCP response must point at the read_docx tool to re-list ids:\n{res}"


def test_f11_reply_on_bad_comment_id_error_advises_read_docx_not_cli(tmp_path):
    """
    F11 (reply path): reply on a nonexistent comment id. Same CLI hint
    ("Run `adeu markup <file> -i` or `adeu extract <file>`") is surfaced
    through the MCP tool response instead of read_docx guidance.
    """
    tracked = _make_doc_with_tracked_change(tmp_path)

    res = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(tracked),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[ReplyComment(target_id="Com:99", text="Following up.")],
        )
    )

    # Sanity (passes today): we hit the reply-not-found diagnostic path.
    assert "Batch rejected" in res
    assert "no comment with that id exists" in res

    # The finding (fails today).
    assert "adeu markup" not in res, f"MCP response recommends the CLI:\n{res}"
    assert "adeu extract" not in res, f"MCP response recommends the CLI:\n{res}"
    assert "read_docx" in res, f"MCP response must point at the read_docx tool to re-list ids:\n{res}"


# ---------------------------------------------------------------------------
# F16 — missing RELATIVE path error never mentions the absolute-path contract
# ---------------------------------------------------------------------------


def test_f16_missing_relative_path_error_names_absolute_path_requirement():
    """
    F16 (Python half): read_docx with a nonexistent RELATIVE path. Python
    already echoes the path exactly as given (directory included — better
    than Node, which dropped it), but the read_docx contract says
    "Absolute path to the DOCX file." and a relative path only resolves
    against the arbitrary server cwd. The error must say absolute paths
    are required. Currently it never mentions them.
    """
    rel_path = "qa_sandbox_nonexistent_f16/alice_copy.docx"

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(
            read_docx(
                reasoning="test",
                file_path=rel_path,
                ctx=MockContext(),
            )
        )
    msg = str(excinfo.value)

    # Sanity (passes today): the path is echoed verbatim, directory included.
    assert rel_path in msg

    # The finding (fails today): no hint that the tool requires absolute paths.
    assert re.search(r"absolute", msg, re.IGNORECASE), (
        f"Missing-file error for a RELATIVE path must state that absolute paths are required; got:\n{msg}"
    )


# ---------------------------------------------------------------------------
# F17 — silent overwrites
# ---------------------------------------------------------------------------

_OVERWRITE_RE = re.compile(r"overwrit|replaced existing", re.IGNORECASE)


def test_f17_second_default_named_run_discloses_overwrite(tmp_path):
    """
    F17(a): two default-named runs on the same input. The second run's
    default output path equals the first run's output, which is silently
    replaced. Current second response is a plain "Batch complete.
    Saved to: ..." with no overwrite disclosure.
    """
    src = tmp_path / "contract.docx"
    _make_plain_doc(src, "Alpha beta gamma delta.")

    first = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(src),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[ModifyText(target_text="Alpha", new_text="ALPHA")],
        )
    )
    assert "Batch complete" in first

    default_out = tmp_path / "contract_processed.docx"
    assert default_out.exists(), "Precondition: first run created the default output"

    second = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(src),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[ModifyText(target_text="beta", new_text="BETA")],
        )
    )
    assert "Batch complete" in second

    # The finding (fails today): the existing output was replaced silently.
    assert _OVERWRITE_RE.search(second), (
        f"Second run replaced the existing {default_out.name} without saying so; got:\n{second}"
    )


def test_f17_processed_stem_input_discloses_in_place_overwrite(tmp_path):
    """
    F17(a, in-place variant): when the input stem already ends in
    _processed, the default output path IS the input path
    (tools/document.py) — the source file is overwritten in place with no
    disclosure in the response.
    """
    src = tmp_path / "contract_processed.docx"
    _make_plain_doc(src, "Alpha beta gamma delta.")

    res = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(src),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[ModifyText(target_text="gamma", new_text="GAMMA")],
        )
    )
    assert "Batch complete" in res
    assert str(src) in res, "Precondition: the default output path is the input path itself"

    # The finding (fails today): the SOURCE was overwritten in place, silently.
    assert _OVERWRITE_RE.search(res), f"In-place overwrite of the source document must be disclosed; got:\n{res}"


def test_f17_explicit_output_path_equal_to_input_discloses_overwrite(tmp_path):
    """
    F17(b): explicit output_path equal to the input path silently
    overwrites the source document. The response must mention the
    in-place overwrite.
    """
    src = tmp_path / "source.docx"
    _make_plain_doc(src, "One two three.")

    res = asyncio.run(
        process_document_batch(
            reasoning="test",
            original_docx_path=str(src),
            author_name="Reviewer",
            ctx=MockContext(),
            changes=[ModifyText(target_text="two", new_text="2")],
            output_path=str(src),
        )
    )
    assert "Batch complete" in res

    # The finding (fails today).
    assert _OVERWRITE_RE.search(res), f"output_path == input path overwrote the source without saying so; got:\n{res}"


# ---------------------------------------------------------------------------
# F18 — accept_all_changes on a change-free document claims action
# ---------------------------------------------------------------------------


def test_f18_accept_all_on_change_free_document_reports_noop(tmp_path):
    """
    F18: accept_all_changes on a document with ZERO tracked changes and
    zero comments. Current response is "Accepted all changes. Saved to:
    <path>" — unlike the Node engine it reports no counts at all, so the
    message genuinely claims action on a no-op. It must indicate that
    there were no changes to accept.
    """
    src = tmp_path / "nochanges.docx"
    _make_plain_doc(src, "Nothing tracked here.")

    res = asyncio.run(
        accept_all_changes(
            reasoning="test",
            docx_path=str(src),
            ctx=MockContext(),
        )
    )

    # Sanity (passes today): the tool did not error.
    assert not res.startswith("Error"), res

    # The finding (fails today): a no-op must be reported as one.
    assert re.search(r"no (pending |tracked )?changes", res, re.IGNORECASE), (
        f"accept_all_changes on a change-free document must report a no-op; got:\n{res}"
    )
