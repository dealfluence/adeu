"""Exercise the shared path policy through the LangChain tool boundary."""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from adeu import path_policy
from docx import Document
from langchain_core.tools import ToolException

from langchain_adeu import AdeuAcceptAllChanges, AdeuApplyTextRevision, AdeuReadDocx, AdeuToolkit, get_tools
from langchain_adeu._shared import validate_docx_path, validate_output_path

_SHARED = Path(__file__).resolve().parents[3] / "shared" / "path_policy"
_CASES = json.loads((_SHARED / "cases.json").read_text(encoding="utf-8"))
_fixtures = runpy.run_path(str(_SHARED / "cases.py"))


@pytest.mark.parametrize(
    "case", [c for c in _CASES["safe_targets"] if c["operation"] == "write"], ids=lambda c: c["id"]
)
def test_shared_output_path_policy(tmp_path: Path, case: dict[str, Any]) -> None:
    if case.get("windows_only") and os.name != "nt":
        pytest.skip("Windows filename rule")
    target, inputs = _fixtures["prepare_case"](tmp_path, case)

    def validate() -> Path:
        sources = [validate_docx_path(str(source)) for source in inputs]
        return validate_output_path(target, *sources)

    # This integration only accepts .docx inputs and does not offer PDF export.
    if case["allow"] and not case.get("pdf") and all(source.suffix.lower() == ".docx" for source in inputs):
        assert validate() == target.resolve()
    else:
        with pytest.raises(ToolException):
            validate()


@pytest.mark.parametrize("case", _CASES["confinement"], ids=lambda c: c["id"])
def test_shared_confinement(tmp_path: Path, case: dict[str, Any]) -> None:
    _fixtures["check_confinement_case"](tmp_path, case)


@pytest.mark.parametrize("case", _CASES["startup"], ids=lambda c: c["id"])
def test_shared_startup(tmp_path: Path, case: dict[str, Any]) -> None:
    _fixtures["check_startup_case"](tmp_path, case)


@pytest.mark.parametrize("constructor", [AdeuReadDocx, AdeuToolkit])
def test_first_construction_freezes_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constructor: type) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(path_policy, "_folders", None)
    monkeypatch.setenv("ADEU_ALLOWED_DIRS", str(allowed))
    first = constructor()
    monkeypatch.setenv("ADEU_ALLOWED_DIRS", str(tmp_path))
    tools = first.get_tools() if isinstance(first, AdeuToolkit) else [first]
    for read_tool in (tools[0], AdeuReadDocx()):
        with pytest.raises(ToolException) as error:
            read_tool.invoke({"reasoning": "Read", "file_path": str(tmp_path / "outside.docx")})
        assert str(error.value) == f"File access refused. Allowed folders: {allowed.resolve()}"


@pytest.mark.parametrize("constructor", [AdeuReadDocx, AdeuToolkit])
def test_invalid_configuration_fails_construction(monkeypatch: pytest.MonkeyPatch, constructor: type) -> None:
    monkeypatch.setattr(path_policy, "_folders", None)
    monkeypatch.setenv("ADEU_ALLOWED_DIRS", "relative-entry")
    with pytest.raises(ValueError, match="relative-entry"):
        constructor()
    monkeypatch.delenv("ADEU_ALLOWED_DIRS")
    with pytest.raises(ValueError, match="relative-entry"):
        constructor()


@pytest.mark.parametrize("confined", [False, True], ids=["unrestricted", "confined"])
def test_registered_tool_schemas_route_every_path_to_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confined: bool
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(path_policy, "_folders", None)
    if confined:
        monkeypatch.setenv("ADEU_ALLOWED_DIRS", str(allowed))
    else:
        monkeypatch.delenv("ADEU_ALLOWED_DIRS", raising=False)
    path_words = {
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
    sample = {
        "reasoning": "Verify paths.",
        "author_name": "Reviewer",
        "changes": [{"type": "modify", "target_text": "draft", "new_text": "final"}],
        "revised_text": "final",
    }
    for tool in get_tools():
        properties = tool.get_input_schema().model_json_schema()["properties"]
        args = {name: value for name, value in sample.items() if name in properties}
        path_fields = [name for name in properties if path_words.intersection(name.split("_"))]
        for name in path_fields:
            path = allowed / f"{tool.name}_{name}.docx"
            if name != "output_path":
                doc = Document()
                doc.add_paragraph("final" if name == "modified_path" else "draft")
                doc.save(path)
            args[name] = str(path)

        with (
            patch.object(path_policy, "resolved_input_path", wraps=path_policy.resolved_input_path) as input_check,
            patch.object(path_policy, "checked_output_path", wraps=path_policy.checked_output_path) as output_check,
        ):
            tool.invoke(args)
        for name in path_fields:
            check = output_check if name == "output_path" else input_check
            checked_paths = [str(call.args[0]) for call in check.call_args_list]
            assert args[name] in checked_paths, f"{tool.name}.{name} bypassed the path policy"

        if confined:
            outside = tmp_path / "outside.docx"
            outside.touch()
            for name in path_fields:
                for denied in (outside, tmp_path / "absent.docx"):
                    with (
                        patch.object(
                            path_policy, "_is_word_package", side_effect=AssertionError("early ZIP inspection")
                        ),
                        patch.object(Path, "read_bytes", side_effect=AssertionError("early read")),
                        pytest.raises(ToolException) as error,
                    ):
                        tool.invoke({**args, name: str(denied)})
                    assert str(error.value) == f"File access refused. Allowed folders: {allowed.resolve()}", (
                        f"{tool.name}.{name}"
                    )


@pytest.mark.parametrize(
    ("name", "derived", "extra"),
    [
        ("adeu_accept_all_changes", "draft_clean.docx", {}),
        ("adeu_sanitize_docx", "draft_sanitized.docx", {}),
        ("adeu_apply_changes", "draft_processed.docx", {"author_name": "Editor", "changes": [{"type": "modify"}]}),
        ("adeu_apply_text_revision", "draft_redlined.docx", {"revised_text": "Final"}),
        ("adeu_apply_text_revision", "draft_redlined.unverified.docx", {"revised_text": "Final"}),
    ],
)
def test_confines_derived_paths_before_inspecting_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, derived: str, extra: dict[str, Any]
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    link = allowed / derived
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(path_policy, "_folders", None)
    monkeypatch.setenv("ADEU_ALLOWED_DIRS", str(allowed))
    tool = next(tool for tool in get_tools() if tool.name == name)
    with pytest.raises(ToolException) as error:
        tool.invoke({"reasoning": "Edit", "file_path": str(allowed / "draft.docx"), **extra})
    assert str(error.value) == f"File access refused. Allowed folders: {allowed.resolve()}"


def test_revision_refuses_unsafe_diagnostic_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "draft.docx"
    source.touch()
    output = tmp_path / "other" / "revised.docx"
    output.parent.mkdir()
    diagnostic = output.with_suffix(".unverified.docx")
    diagnostic.write_text("keep this", encoding="utf-8")

    with pytest.raises(ToolException, match="unverified"):
        AdeuApplyTextRevision().invoke(
            {
                "reasoning": "Revise draft",
                "file_path": str(source),
                "revised_text": "revised",
                "output_path": str(output),
            }
        )
    assert diagnostic.read_text(encoding="utf-8") == "keep this"
    assert not output.exists()


def test_writes_to_resolved_directory_link(tmp_path: Path) -> None:
    source = tmp_path / "input.docx"
    Document().save(source)
    folder = tmp_path / "actual"
    folder.mkdir()
    link = tmp_path / "linked"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(folder), str(link))
    else:
        link.symlink_to(folder, target_is_directory=True)

    result = AdeuAcceptAllChanges().invoke(
        {"reasoning": "Finalize", "file_path": str(source), "output_path": str(link / "new" / "output.docx")}
    )
    output = folder / "new" / "output.docx"
    assert str(output) in result
    assert output.is_file()


def test_replacing_interrupted_output_preserves_other_hard_links(tmp_path: Path) -> None:
    source = tmp_path / "input.docx"
    Document().save(source)
    existing = tmp_path / "notes.txt"
    existing.write_bytes(b"keep these notes")
    output = tmp_path / "output.docx"
    output.hardlink_to(existing)

    AdeuAcceptAllChanges().invoke({"reasoning": "Finalize", "file_path": str(source), "output_path": str(output)})
    assert existing.read_bytes() == b"keep these notes"
    Document(output)
