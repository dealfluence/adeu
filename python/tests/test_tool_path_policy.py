import asyncio
import json
import os
import runpy
import socket
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from docx import Document
from fastmcp.exceptions import ToolError

from adeu import path_policy
from adeu.mcp_components.tools import document
from tests.utils import get_mock_ctx

_SHARED = Path(__file__).resolve().parents[2] / "shared" / "path_policy"
_CASES = json.loads((_SHARED / "cases.json").read_text(encoding="utf-8"))
_prepare_case = runpy.run_path(str(_SHARED / "cases.py"))["prepare_case"]
_PATH_WORDS = {
    "path",
    "paths",
    "file",
    "files",
    "filename",
    "filenames",
    "directory",
    "directories",
    "folder",
    "folders",
}


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_shared_tool_path_policy(tmp_path, case, monkeypatch):
    if case.get("windows_only") and os.name != "nt":
        pytest.skip("Windows filename rule")
    target, inputs = _prepare_case(tmp_path, case)
    actual = tmp_path / case.get("symlink", case["target"])
    parent_existed = actual.parent.is_dir()
    if case["operation"] == "open":
        launcher = Mock()
        if sys.platform == "win32":
            monkeypatch.setattr(os, "startfile", launcher)
        else:
            monkeypatch.setattr(document.subprocess, "run", launcher)
        open_file = document.open_local_file(file_path=str(target), ctx=get_mock_ctx())
        if case["allow"]:
            asyncio.run(open_file)
            if sys.platform == "win32":
                launcher.assert_called_once_with(actual.resolve())
            else:
                command = "open" if sys.platform == "darwin" else "xdg-open"
                launcher.assert_called_once_with([command, str(actual.resolve())], check=True)
        else:
            with pytest.raises(ToolError, match="Refused"):
                asyncio.run(open_file)
            launcher.assert_not_called()
    elif case["allow"]:
        assert path_policy.checked_output_path(target, *inputs, pdf=case.get("pdf", False)) == actual.resolve()
    else:
        with pytest.raises(path_policy.PathPolicyError, match="Refused"):
            path_policy.checked_output_path(target, *inputs, pdf=case.get("pdf", False))
    assert actual.parent.is_dir() == parent_existed


def test_registered_tool_schemas_route_every_path_to_policy(tmp_path, monkeypatch):
    from adeu.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert tools
    if sys.platform == "win32":
        from adeu.mcp_components.tools import live_word

        app = Mock()
        app.Documents.Count = 0
        app.ActiveDocument.Path = str(tmp_path)
        app.ActiveDocument.FullName = str(tmp_path / "active.docx")
        app.ActiveDocument.SaveFormat = 16
        monkeypatch.setattr(live_word.win32com.client, "GetActiveObject", lambda _: app)
        monkeypatch.setattr(live_word.win32com.client, "Dispatch", lambda _: app)
        monkeypatch.setattr(os, "startfile", Mock())
    else:
        monkeypatch.setattr(document.subprocess, "run", Mock())

    examples = {
        "changes": [{"type": "modify", "target_text": "draft", "new_text": "final"}],
        "revised_text": "final",
    }
    for tool in tools:
        properties = tool.parameters["properties"]
        args = {name: value for name, value in examples.items() if name in properties}
        paths = [name for name in properties if _PATH_WORDS.intersection(name.split("_"))]
        for name in paths:
            path = tmp_path / f"{tool.name}_{name}.docx"
            if name != "output_path":
                doc = Document()
                doc.add_paragraph("draft")
                doc.save(path)
            args[name] = str(path)

        with (
            patch.object(path_policy, "resolved_input_path", wraps=path_policy.resolved_input_path) as read_check,
            patch.object(path_policy, "checked_output_path", wraps=path_policy.checked_output_path) as write_check,
            patch.object(path_policy, "checked_open_path", wraps=path_policy.checked_open_path) as open_check,
        ):
            asyncio.run(tool.fn(ctx=get_mock_ctx(), **args))
        for name in paths:
            check = read_check
            if name == "output_path":
                check = write_check
            elif tool.name in {"open_local_file", "open_word_document"}:
                check = open_check
            assert args[name] in {str(call.args[0]) for call in check.call_args_list}, f"{tool.name}.{name}"


def test_revision_refuses_unsafe_diagnostic_before_writing(tmp_path):
    source = tmp_path / "draft.docx"
    Document().save(source)
    output = tmp_path / "other" / "result.docx"
    output.parent.mkdir()
    diagnostic = output.with_suffix(".unverified.docx")
    diagnostic.write_bytes(b"keep this file")
    with pytest.raises(ToolError, match="unverified"):
        asyncio.run(
            document.apply_text_revision(
                file_path=str(source), revised_text="", output_path=str(output), ctx=get_mock_ctx()
            )
        )
    assert not output.exists()
    assert diagnostic.read_bytes() == b"keep this file"


def test_revision_writes_to_resolved_diagnostic(tmp_path, monkeypatch):
    from adeu import text_revision

    source = tmp_path / "draft.docx"
    doc = Document()
    doc.add_paragraph("The draft.")
    doc.save(source)
    target = tmp_path / "result.docx"
    link = target.with_suffix(".unverified.docx")
    actual = tmp_path / "other" / "diagnostic.docx"
    try:
        link.symlink_to(actual)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows requires symlink privilege")
        raise
    monkeypatch.setattr(text_revision, "verify_clean_text", lambda *_: (False, "verification failed"))

    with pytest.raises(ToolError, match="verification failed"):
        asyncio.run(
            document.apply_text_revision(
                file_path=str(source), revised_text="The final.", output_path=str(target), ctx=get_mock_ctx()
            )
        )
    assert link.is_symlink()
    assert actual.is_file()
    assert not target.exists()


def test_replacing_interrupted_output_preserves_other_hardlinks(tmp_path):
    source = tmp_path / "draft.docx"
    Document().save(source)
    original = tmp_path / "notes.txt"
    original.write_text("Keep these notes", encoding="utf-8")
    output = tmp_path / "result.docx"
    output.hardlink_to(original)

    asyncio.run(document.accept_all_changes(docx_path=str(source), output_path=str(output), ctx=get_mock_ctx()))
    assert path_policy.checked_open_path(output) == output
    assert original.read_text(encoding="utf-8") == "Keep these notes"


@pytest.mark.skipif(os.name == "nt", reason="POSIX special files")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_special_targets_are_refused_without_open(tmp_path, monkeypatch, kind):
    path = tmp_path / f"{kind}.docx"
    with socket.socket(socket.AF_UNIX) as sock:
        if kind == "fifo":
            os.mkfifo(path)
        else:
            sock.bind(str(path))
        monkeypatch.setattr(path_policy, "ZipFile", Mock(side_effect=AssertionError("Opened a special file")))
        with pytest.raises(path_policy.PathPolicyError, match="regular file"):
            path_policy.checked_output_path(path, tmp_path / "source.docx")
        with pytest.raises(path_policy.PathPolicyError, match="regular file"):
            path_policy.checked_open_path(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Word automation")
def test_live_word_checks_open_and_save_before_com_action(tmp_path, monkeypatch):
    import _winapi

    from adeu.mcp_components.tools import live_word

    dispatch = Mock()
    monkeypatch.setattr(live_word.win32com.client, "Dispatch", dispatch)
    source = tmp_path / "draft.docx"
    source.write_bytes(b"invalid Word package")
    with pytest.raises(ToolError, match="Word package"):
        asyncio.run(live_word.open_word_document_impl(get_mock_ctx(), str(source)))
    dispatch.assert_not_called()

    app = Mock()
    app.ActiveDocument.Path = str(tmp_path)
    app.ActiveDocument.FullName = str(source)
    app.ActiveDocument.SaveFormat = 16
    monkeypatch.setattr(live_word.win32com.client, "GetActiveObject", lambda _: app)
    unsafe = tmp_path / "missing" / "out.txt"
    with pytest.raises(ToolError, match="ending"):
        asyncio.run(live_word.save_active_word_document_impl(get_mock_ctx(), str(unsafe)))
    app.ActiveDocument.SaveAs2.assert_not_called()
    assert not unsafe.parent.exists()

    directory = tmp_path / "actual"
    directory.mkdir()
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(directory), str(junction))
    actual = directory / "new" / "out.docx"
    link = junction / "new" / "out.docx"
    asyncio.run(live_word.save_active_word_document_impl(get_mock_ctx(), str(link)))
    app.ActiveDocument.SaveAs2.assert_called_once_with(str(actual.resolve()), 16)
    assert actual.parent.is_dir()
