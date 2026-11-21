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

from langchain_adeu import AdeuAcceptAllChanges, AdeuApplyTextRevision, get_tools
from langchain_adeu._shared import validate_docx_path, validate_output_path

_SHARED = Path(__file__).resolve().parents[3] / "shared" / "path_policy"
_CASES = json.loads((_SHARED / "cases.json").read_text(encoding="utf-8"))
_prepare_case = runpy.run_path(str(_SHARED / "cases.py"))["prepare_case"]


@pytest.mark.parametrize("case", [c for c in _CASES if c["operation"] == "write"], ids=lambda c: c["id"])
def test_shared_output_path_policy(tmp_path: Path, case: dict[str, Any]) -> None:
    if case.get("windows_only") and os.name != "nt":
        pytest.skip("Windows filename rule")
    target, inputs = _prepare_case(tmp_path, case)

    def validate() -> Path:
        sources = [validate_docx_path(str(source)) for source in inputs]
        return validate_output_path(target, *sources)

    # This integration only accepts .docx inputs and does not offer PDF export.
    if case["allow"] and not case.get("pdf") and all(source.suffix.lower() == ".docx" for source in inputs):
        assert validate() == target.resolve()
    else:
        with pytest.raises(ToolException):
            validate()


def test_registered_tool_schemas_route_every_path_to_policy(tmp_path: Path) -> None:
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
            path = tmp_path / f"{tool.name}_{name}.docx"
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
