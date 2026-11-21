import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
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
_fixtures = runpy.run_path(str(_SHARED / "cases.py"))
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


@pytest.mark.parametrize("case", _CASES["safe_targets"], ids=lambda case: case["id"])
def test_shared_tool_path_policy(tmp_path, case, monkeypatch):
    if case.get("windows_only") and os.name != "nt":
        pytest.skip("Windows filename rule")
    target, inputs = _fixtures["prepare_case"](tmp_path, case)
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


@pytest.mark.parametrize("case", _CASES["confinement"], ids=lambda case: case["id"])
def test_shared_confinement(tmp_path, case):
    _fixtures["check_confinement_case"](tmp_path, case)


@pytest.mark.parametrize("case", _CASES["startup"], ids=lambda case: case["id"])
def test_shared_startup(tmp_path, case):
    _fixtures["check_startup_case"](tmp_path, case)


@pytest.mark.parametrize("case", _CASES["startup"], ids=lambda case: case["id"])
def test_server_starts_only_with_valid_allowed_directories(tmp_path, case):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "second").mkdir()
    _fixtures["prepare_directory_link"](tmp_path, case)
    (tmp_path / "file.docx").touch()
    env = os.environ.copy()
    env.pop("ADEU_ALLOWED_DIRS", None)
    if case["entries"] is not None:
        entries = [str(tmp_path / e[6:]) if e.startswith("$root/") else e for e in case["entries"]]
        env["ADEU_ALLOWED_DIRS"] = os.pathsep.join(entries)
    result = subprocess.run(
        [sys.executable, "-m", "adeu.server", "--version"], env=env, capture_output=True, encoding="utf-8", timeout=60
    )
    if "error" not in case:
        assert result.returncode == 0, result.stderr
        if case["entries"] is None:
            assert "ADEU_ALLOWED_DIRS" not in result.stderr
        else:
            expected = os.pathsep.join(str(Path(e).resolve()) for e in entries if e.strip())
            if "resolved" in case:
                expected = os.pathsep.join(str((tmp_path / e[6:]).resolve()) for e in case["resolved"])
            assert result.stderr.count(f"ADEU_ALLOWED_DIRS allowed folders: {expected}") == 1
    else:
        assert result.returncode != 0
        assert not result.stdout
        with pytest.raises(path_policy.PathPolicyError) as exc:
            path_policy.AllowedDirectories.from_value(env["ADEU_ALLOWED_DIRS"])
        assert result.stderr.strip() == str(exc.value)


def test_embedded_server_keeps_its_startup_snapshot(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    env = dict(os.environ, ADEU_ALLOWED_DIRS=str(other))
    probe = (
        "import os, sys\n"
        "from adeu.server import mcp\n"
        "os.environ['ADEU_ALLOWED_DIRS'] = sys.argv[1]\n"
        "from adeu.path_policy import get_allowed_directories\n"
        "print(get_allowed_directories().display)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)], env=env, capture_output=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(other.resolve())
    assert result.stderr.count("ADEU_ALLOWED_DIRS allowed folders:") == 1


def test_registered_tool_schemas_route_every_path_to_policy(tmp_path, monkeypatch):
    from fastmcp.server.providers import FileSystemProvider

    from adeu.server import mcp_dir

    # Discover optional tools too, regardless of when other tests imported the server.
    monkeypatch.setenv("ADEU_ENABLE_TEST_TOOLS", "1")
    tools = asyncio.run(FileSystemProvider(root=mcp_dir).list_tools())
    assert tools
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    if sys.platform == "win32":
        from adeu.mcp_components.tools import live_word

        app = Mock()
        app.Documents.Count = 0
        app.ActiveDocument.Path = str(allowed)
        app.ActiveDocument.FullName = str(allowed / "active.docx")
        app.ActiveDocument.SaveFormat = 16
        monkeypatch.setattr(live_word.win32com.client, "GetActiveObject", lambda _: app)
        monkeypatch.setattr(live_word.win32com.client, "Dispatch", lambda _: app)
        monkeypatch.setattr(os, "startfile", Mock())
        assert "debug_xml_diff" in {tool.name for tool in tools}
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
            path = allowed / f"{tool.name}_{name}.docx"
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

        # An independently discovered path must be refused when it alone is outside.
        # Keep the other inputs/outputs valid to catch tools checking only the first path.
        with monkeypatch.context() as mp:
            folders = path_policy.AllowedDirectories.from_value(str(allowed))
            mp.setattr(path_policy, "get_allowed_directories", Mock(return_value=folders))
            for name in paths:
                outside = tmp_path / f"forbidden_{tool.name}_{name}.docx"
                for exists in (False, True):
                    if exists:
                        Document().save(outside)
                    with pytest.raises(ToolError) as refusal:
                        asyncio.run(tool.fn(ctx=get_mock_ctx(), **(args | {name: str(outside)})))
                    assert str(refusal.value) == folders.refusal, f"{tool.name}.{name}: {refusal.value}"


def test_outside_read_refusals_are_identical_before_caches_and_suggestions(tmp_path, monkeypatch):
    from adeu.mcp_components.doc_cache import doc_cache

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    folders = path_policy.AllowedDirectories.from_value(str(allowed))
    monkeypatch.setattr(path_policy, "get_allowed_directories", lambda: folders)
    touched = Mock(side_effect=AssertionError("Confinement must precede I/O"))
    monkeypatch.setattr(doc_cache, "stat_key", touched)
    monkeypatch.setattr(document, "_not_found_error", touched)
    if sys.platform == "win32":
        monkeypatch.setattr(document, "is_document_open_in_word", touched)
    existing = tmp_path / "outside.docx"
    Document().save(existing)
    messages = []
    for target in (existing, tmp_path / "missing.docx"):
        with pytest.raises(ToolError) as refusal:
            asyncio.run(document.read_docx(ctx=get_mock_ctx(), file_path=str(target)))
        messages.append(str(refusal.value))
    assert messages[0] == messages[1]
    assert messages[0] == folders.refusal
    touched.assert_not_called()


def test_confined_missing_file_hints_check_each_candidate(tmp_path, monkeypatch):
    from adeu.mcp_components.shared import read_file_bytes

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    inside_file = allowed / "contract.docx"
    inside_file.touch()
    candidate = allowed / "forbidden.docx"
    alias = outside / "into-allowed"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(candidate))
        _winapi.CreateJunction(str(allowed), str(alias))
    else:
        external_file = outside / "private.docx"
        external_file.touch()
        candidate.symlink_to(external_file)
        alias.symlink_to(allowed, target_is_directory=True)
    folders = path_policy.AllowedDirectories.from_value(str(allowed))
    monkeypatch.setattr(path_policy, "get_allowed_directories", lambda: folders)
    is_file = Path.is_file

    def allowed_file_metadata(path):
        assert path != candidate
        assert path.is_relative_to(allowed)
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", allowed_file_metadata)
    for parent in (allowed, alias):
        with pytest.raises(FileNotFoundError) as missing:
            read_file_bytes(str(parent / "conract.docx"))
        assert "available files: [contract.docx]" in str(missing.value)
        assert candidate.name not in str(missing.value)
    with pytest.raises(path_policy.FolderAccessError) as refused:
        read_file_bytes(str(candidate / "missing.docx"))
    assert str(refused.value) == folders.refusal


@pytest.mark.parametrize("operation", ["batch", "accept", "revision", "diagnostic", "sanitize"])
def test_refuses_derived_sibling_leading_outside(tmp_path, monkeypatch, operation):
    from adeu.mcp_components.tools.sanitize import sanitize_docx
    from adeu.text_revision import default_revision_output, unverified_revision_output

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "draft.docx"
    Document().save(source)
    outside = tmp_path / "outside.docx"
    revision = default_revision_output(source)
    outputs = {
        "accept": allowed / "draft_clean.docx",
        "batch": allowed / "draft_processed.docx",
        "sanitize": allowed / "draft_sanitized.docx",
        "revision": revision,
        "diagnostic": unverified_revision_output(revision),
    }
    if os.name == "nt":
        # Junction creation needs no file-symlink privilege. This must get the
        # folder refusal before the output check could report "not a regular file".
        import _winapi

        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(outputs[operation]))
    else:
        outputs[operation].symlink_to(outside)
    folders = path_policy.AllowedDirectories.from_value(str(allowed))
    monkeypatch.setattr(path_policy, "get_allowed_directories", lambda: folders)
    if sys.platform == "win32":
        monkeypatch.setattr(document, "is_document_open_in_word", lambda _: False)
    ctx = get_mock_ctx()
    if operation == "batch":
        action = document.process_document_batch(ctx=ctx, original_docx_path=str(source), changes=[])
    elif operation == "accept":
        action = document.accept_all_changes(ctx=ctx, docx_path=str(source))
    elif operation == "sanitize":
        action = sanitize_docx(ctx=ctx, file_path=str(source))
    else:
        action = document.apply_text_revision(ctx=ctx, file_path=str(source), revised_text="changed")
    with pytest.raises(ToolError) as refusal:
        asyncio.run(action)
    assert str(refusal.value) == folders.refusal
    if os.name == "nt":
        assert list(outside.iterdir()) == []
    else:
        assert not outside.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Word automation")
@pytest.mark.parametrize("operation", ["read", "batch", "empty_batch", "save", "save_as"])
@pytest.mark.parametrize("saved", [True, False], ids=["outside", "unsaved"])
def test_confined_active_word_tools_check_actual_document(tmp_path, monkeypatch, operation, saved):
    from adeu.mcp_components.tools import live_word

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    folders = path_policy.AllowedDirectories.from_value(str(allowed))
    monkeypatch.setattr(path_policy, "get_allowed_directories", lambda: folders)
    app = Mock()
    doc = app.ActiveDocument
    doc.Path = str(tmp_path) if saved else ""
    doc.FullName = str(tmp_path / "outside.docx") if saved else "Document1"
    doc.TrackRevisions = False
    monkeypatch.setattr(live_word.win32com.client, "GetActiveObject", lambda _: app)
    extracted = Mock(side_effect=AssertionError("Extracted a refused live document"))
    monkeypatch.setattr(live_word, "_build_mock_docx_stream", extracted)
    ctx = get_mock_ctx()
    if operation == "read":
        action = document.read_docx(ctx=ctx)
    elif operation in ("batch", "empty_batch"):
        changes = [] if operation == "empty_batch" else [{"type": "modify", "target_text": "old", "new_text": "new"}]
        action = document.process_document_batch(ctx=ctx, changes=changes)
    else:
        target = str(allowed / "save.docx") if operation == "save_as" else None
        action = live_word.save_active_word_document_impl(ctx=ctx, output_path=target, close=True)
    with pytest.raises(ToolError) as refusal:
        asyncio.run(action)
    assert str(refusal.value) == folders.refusal
    extracted.assert_not_called()
    assert doc.TrackRevisions is False
    doc.Save.assert_not_called()
    doc.SaveAs2.assert_not_called()
    doc.Close.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows diagnostic and live Word tools")
def test_confined_debug_tools_keep_data_in_memory(tmp_path, monkeypatch):
    from fastmcp.server.providers import FileSystemProvider

    from adeu.mcp_components.tools import live_word
    from adeu.server import mcp_dir
    from adeu.utils import xml_debug

    folders = path_policy.AllowedDirectories.from_value(str(tmp_path))
    monkeypatch.setattr(path_policy, "get_allowed_directories", lambda: folders)
    monkeypatch.setenv("ADEU_ENABLE_TEST_TOOLS", "1")
    monkeypatch.setenv("ADEU_DEBUG_FLATOPC", "1")
    tools = asyncio.run(FileSystemProvider(root=mcp_dir).list_tools())
    xml_diff = next(tool for tool in tools if tool.name == "debug_xml_diff")
    spill = Mock(side_effect=AssertionError("Confined diagnostics must not spill to the temp folder"))
    monkeypatch.setattr(tempfile, "mkstemp", spill)
    monkeypatch.setattr(tempfile, "gettempdir", spill)
    monkeypatch.setattr(xml_debug, "get_abstracted_xml_snapshot", Mock(side_effect=["draft", "z" * 160_000]))
    result = asyncio.run(
        xml_diff.fn(ctx=get_mock_ctx(), file_a=str(tmp_path / "a.docx"), file_b=str(tmp_path / "b.docx"))
    )
    assert "[Diff truncated to 150KB.]" in result

    app = Mock()
    app.ActiveDocument.Path = str(tmp_path)
    app.ActiveDocument.FullName = str(tmp_path / "active.docx")
    app.ActiveDocument.WordOpenXML = "<pkg:package/>"
    monkeypatch.setattr(live_word.win32com.client, "GetActiveObject", lambda _: app)
    # Stop after Flat OPC reconstruction, when the optional debug copy would have been written.
    finished = RuntimeError("reconstructed in memory")
    monkeypatch.setattr(live_word, "load_document", Mock(side_effect=finished))
    with pytest.raises(RuntimeError, match="reconstructed in memory"):
        asyncio.run(document.read_docx(ctx=get_mock_ctx()))
    spill.assert_not_called()


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
