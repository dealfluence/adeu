# FILE: tests/test_live_word_dispatch.py
"""
Dispatch-layer tests for the COM-unavailable disk fallback in document.py.

Unlike test_live_word.py, these require NO running Word instance: they mock the
COM layer entirely and assert the *routing* decisions read_docx /
process_document_batch make when the live path raises. They remain Windows-only
because the probe-gated dispatch and the LiveWordUnavailableError fallback only
exist in the win32 branch of document.py — the non-Windows branch reads/edits
disk unconditionally and has nothing to route.

What the fix guarantees, and what each test locks down:
  - LiveWordUnavailableError (Word/COM dead or zombie) -> silent disk fallback,
    because an explicit file_path makes the disk copy authoritative. This is the
    product-robustness path the headless benchmark never hits.
  - A post-read ToolError (page out of range, etc.) means the live doc WAS read
    fine but the request was bad — it must propagate, NOT trigger a fallback that
    would mask the real error.
  - Probe False -> the live path is never touched; disk is used directly.
  - The active-document mode (no file_path) has no disk fallback by design:
    LiveWordUnavailableError there is surfaced, not swallowed.
"""

import sys

import pytest

from tests.utils import get_mock_ctx, run_async

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Probe-gated dispatch and LiveWordUnavailableError fallback only exist in the win32 branch of document.py",
)

if sys.platform == "win32":
    from fastmcp.exceptions import ToolError
    from fastmcp.tools.tool import ToolResult

    from adeu.mcp_components.tools import document as doc_mod
    from adeu.mcp_components.tools.live_word import LiveWordUnavailableError
    from adeu.models import ModifyText


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disk_sentinel():
    """Stand-in return for the disk path. Identity via substring is all we assert."""
    return ToolResult(content="DISK_FALLBACK_RESULT")


def _as_text(res):
    """Flatten a tool return (possibly add_timing_if_debug-wrapped) to a string."""
    content = getattr(res, "content", res)
    if isinstance(content, list):
        return "".join(getattr(c, "text", str(c)) for c in content)
    return str(content)


# ---------------------------------------------------------------------------
# read_docx dispatch
# ---------------------------------------------------------------------------


def test_read_docx_falls_back_to_disk_on_com_unavailable(monkeypatch):
    """
    Probe reports the file open, but the live read raises LiveWordUnavailableError
    (dead/zombie COM). read_docx must fall back to _read_docx_disk with the same
    path, rather than surfacing -2147221021 to the model.
    """
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    async def _boom(*args, **kwargs):
        raise LiveWordUnavailableError("Could not connect to active Word document. (-2147221021)")

    monkeypatch.setattr(doc_mod, "read_active_word_document", _boom)

    called = {}

    async def _disk(file_path, ctx, clean_view, mode, page, **kwargs):
        called["file_path"] = file_path
        called["mode"] = mode
        return _disk_sentinel()

    monkeypatch.setattr(doc_mod, "_read_docx_disk", _disk)

    async def run_test():
        res = await doc_mod.read_docx(
            ctx=ctx,
            file_path=r"C:\deal\contract.docx",
            mode="full",
        )
        assert called["file_path"] == r"C:\deal\contract.docx"
        assert called["mode"] == "full"
        assert "DISK_FALLBACK_RESULT" in _as_text(res)

    run_async(run_test())


def test_read_docx_does_not_swallow_toolerror(monkeypatch):
    """
    A post-read ToolError (e.g. page out of range) means the live doc read
    succeeded but the request was invalid. It must propagate, NOT trigger a disk
    fallback that would mask the real error.
    """
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    async def _page_oor(*args, **kwargs):
        raise ToolError("Page 999 out of range.")

    monkeypatch.setattr(doc_mod, "read_active_word_document", _page_oor)

    disk = {"hit": False}

    async def _disk(*args, **kwargs):
        disk["hit"] = True
        return _disk_sentinel()

    monkeypatch.setattr(doc_mod, "_read_docx_disk", _disk)

    async def run_test():
        with pytest.raises(ToolError, match="out of range"):
            await doc_mod.read_docx(
                ctx=ctx,
                file_path=r"C:\deal\contract.docx",
                mode="full",
                page=999,
            )
        assert disk["hit"] is False, "ToolError must NOT trigger disk fallback"

    run_async(run_test())


def test_read_docx_uses_disk_directly_when_not_open(monkeypatch):
    """Probe reports not open -> live read is never called; disk is used directly."""
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: False)

    live = {"hit": False}

    async def _live(*args, **kwargs):
        live["hit"] = True
        raise AssertionError("live read must not be called when the probe returns False")

    monkeypatch.setattr(doc_mod, "read_active_word_document", _live)

    async def _disk(file_path, ctx, clean_view, mode, page, **kwargs):
        return _disk_sentinel()

    monkeypatch.setattr(doc_mod, "_read_docx_disk", _disk)

    async def run_test():
        res = await doc_mod.read_docx(
            ctx=ctx,
            file_path=r"C:\deal\contract.docx",
            mode="full",
        )
        assert live["hit"] is False
        assert "DISK_FALLBACK_RESULT" in _as_text(res)

    run_async(run_test())


def test_read_docx_uses_live_when_open_and_com_healthy(monkeypatch):
    """Probe open + healthy COM -> the live read is used and its result returned (no fallback)."""
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    async def _live(*args, **kwargs):
        return ToolResult(content="LIVE_CANVAS_RESULT")

    monkeypatch.setattr(doc_mod, "read_active_word_document", _live)

    disk = {"hit": False}

    async def _disk(*args, **kwargs):
        disk["hit"] = True
        return _disk_sentinel()

    monkeypatch.setattr(doc_mod, "_read_docx_disk", _disk)

    async def run_test():
        res = await doc_mod.read_docx(
            ctx=ctx,
            file_path=r"C:\deal\contract.docx",
            mode="full",
        )
        assert disk["hit"] is False
        assert "LIVE_CANVAS_RESULT" in _as_text(res)

    run_async(run_test())


# ---------------------------------------------------------------------------
# process_document_batch dispatch
# ---------------------------------------------------------------------------


def test_process_batch_falls_back_to_disk_on_com_unavailable(monkeypatch):
    """
    Probe reports the file open, but the live batch raises LiveWordUnavailableError.
    process_document_batch must fall back to _process_document_batch_disk with the
    same path and changes, rather than erroring.
    """
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    async def _boom(*args, **kwargs):
        raise LiveWordUnavailableError("Could not connect to active Word document. (-2147221021)")

    monkeypatch.setattr(doc_mod, "process_active_word_batch", _boom)

    called = {}

    async def _disk(
        original_docx_path,
        author_name,
        ctx,
        changes,
        output_path,
        dry_run=False,
        rejected_notes=None,
    ):
        called["path"] = original_docx_path
        called["author"] = author_name
        called["n_changes"] = len(changes)
        called["dry_run"] = dry_run
        return "DISK_BATCH_RESULT"

    monkeypatch.setattr(doc_mod, "_process_document_batch_disk", _disk)

    changes = [ModifyText(type="modify", target_text="foo", new_text="bar")]

    async def run_test():
        res = await doc_mod.process_document_batch(
            author_name="Reviewer AI",
            ctx=ctx,
            changes=changes,
            original_docx_path=r"C:\deal\contract.docx",
        )
        assert called["path"] == r"C:\deal\contract.docx"
        assert called["author"] == "Reviewer AI"
        assert called["n_changes"] == 1
        assert called["dry_run"] is False
        assert "DISK_BATCH_RESULT" in _as_text(res)

    run_async(run_test())


def test_process_batch_uses_live_when_open_and_com_healthy(monkeypatch):
    """Probe open + healthy COM -> live batch is used; disk is not touched."""
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    async def _live(ctx, changes, author_name, path):
        return "LIVE_BATCH_RESULT"

    monkeypatch.setattr(doc_mod, "process_active_word_batch", _live)

    disk = {"hit": False}

    async def _disk(*args, **kwargs):
        disk["hit"] = True
        return "DISK_BATCH_RESULT"

    monkeypatch.setattr(doc_mod, "_process_document_batch_disk", _disk)

    changes = [ModifyText(type="modify", target_text="foo", new_text="bar")]

    async def run_test():
        res = await doc_mod.process_document_batch(
            author_name="Reviewer AI",
            ctx=ctx,
            changes=changes,
            original_docx_path=r"C:\deal\contract.docx",
        )
        assert disk["hit"] is False
        assert "LIVE_BATCH_RESULT" in _as_text(res)

    run_async(run_test())


def test_process_batch_uses_disk_directly_when_not_open(monkeypatch):
    """Probe not open -> live batch never called; disk edit used directly."""
    ctx = get_mock_ctx()
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: False)

    live = {"hit": False}

    async def _live(*args, **kwargs):
        live["hit"] = True
        raise AssertionError("live batch must not be called when the probe returns False")

    monkeypatch.setattr(doc_mod, "process_active_word_batch", _live)

    async def _disk(*args, **kwargs):
        return "DISK_BATCH_RESULT"

    monkeypatch.setattr(doc_mod, "_process_document_batch_disk", _disk)

    changes = [ModifyText(type="modify", target_text="foo", new_text="bar")]

    async def run_test():
        res = await doc_mod.process_document_batch(
            author_name="Reviewer AI",
            ctx=ctx,
            changes=changes,
            original_docx_path=r"C:\deal\contract.docx",
        )
        assert live["hit"] is False
        assert "DISK_BATCH_RESULT" in _as_text(res)

    run_async(run_test())


# ---------------------------------------------------------------------------
# Active-document mode (no file_path) has NO disk fallback — error surfaces
# ---------------------------------------------------------------------------


def test_process_batch_active_mode_surfaces_com_unavailable(monkeypatch):
    """
    With no original_docx_path, there is no disk copy to fall back to. A
    LiveWordUnavailableError from the active-document path must surface (as a
    ToolError from process_active_word_batch), not be silently swallowed.
    """
    ctx = get_mock_ctx()

    # Probe should not even be consulted in active-document mode, but stub it safe.
    monkeypatch.setattr(doc_mod, "is_document_open_in_word", lambda _p: True)

    disk = {"hit": False}

    async def _disk(*args, **kwargs):
        disk["hit"] = True
        return "DISK_BATCH_RESULT"

    monkeypatch.setattr(doc_mod, "_process_document_batch_disk", _disk)

    # In active mode the dispatcher calls process_active_word_batch(None). That
    # function wraps a LiveWordUnavailableError as ToolError (it re-raises the
    # unavailable error, which the tool boundary turns into a ToolError). We
    # simulate the surfaced form here.
    async def _active(ctx, changes, author_name, path):
        assert path is None
        raise ToolError("Could not connect to active Word document. (-2147221021)")

    monkeypatch.setattr(doc_mod, "process_active_word_batch", _active)

    changes = [ModifyText(type="modify", target_text="foo", new_text="bar")]

    async def run_test():
        with pytest.raises(ToolError, match="active Word document"):
            await doc_mod.process_document_batch(
                author_name="Reviewer AI",
                ctx=ctx,
                changes=changes,
                original_docx_path=None,
            )
        assert disk["hit"] is False

    run_async(run_test())
