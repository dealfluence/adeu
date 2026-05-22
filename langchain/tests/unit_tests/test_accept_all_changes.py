# FILE: langchain/tests/unit_tests/test_accept_all_changes.py
"""Unit tests for `AdeuAcceptAllChanges` — input validation and schema.

End-to-end acceptance against a real tracked-changes DOCX is covered by
integration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu import AdeuAcceptAllChanges, AdeuAcceptAllChangesInput


class TestAdeuAcceptAllChangesSchema:
    def test_name_is_snake_case(self) -> None:
        tool = AdeuAcceptAllChanges()
        assert tool.name == "adeu_accept_all_changes"

    def test_description_mentions_destructive_nature(self) -> None:
        # Make sure the description tells the model that the output has
        # no tracked-change history — this is the most important property
        # for an agent to understand before invoking.
        tool = AdeuAcceptAllChanges()
        assert (
            "history" in tool.description.lower()
            or "finalized" in tool.description.lower()
        )

    def test_args_schema_required_fields(self) -> None:
        # Only file_path is required; output_path has a default of None.
        schema = AdeuAcceptAllChangesInput.model_json_schema()
        assert schema["required"] == ["file_path"]

    def test_args_schema_rejects_extra_fields(self) -> None:

        with pytest.raises(ValueError):
            AdeuAcceptAllChangesInput.model_validate(
                {"file_path": "/a.docx", "policy": "auto"}
            )

    def test_response_format_is_content_and_artifact(self) -> None:
        tool = AdeuAcceptAllChanges()
        assert tool.response_format == "content_and_artifact"


class TestAdeuAcceptAllChangesValidation:
    def test_rejects_nonexistent_input(self) -> None:
        tool = AdeuAcceptAllChanges()
        with pytest.raises(ToolException, match="does not exist"):
            tool.invoke({"file_path": "/nonexistent/file.docx"})

    def test_rejects_non_docx_input(self, tmp_path: Path) -> None:
        bad = tmp_path / "doc.txt"
        bad.write_text("nope")
        tool = AdeuAcceptAllChanges()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke({"file_path": str(bad)})

    def test_rejects_overwrite_of_input(self, tmp_path: Path) -> None:
        # Same physical path for input and output is rejected even if the
        # file is empty — we never reach the engine, the path-resolution
        # guard catches it.
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuAcceptAllChanges()
        with pytest.raises(ToolException, match="must differ from input path"):
            tool.invoke({"file_path": str(src), "output_path": str(src)})

    def test_rejects_non_docx_output_path(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        target = tmp_path / "out.txt"
        tool = AdeuAcceptAllChanges()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke({"file_path": str(src), "output_path": str(target)})

    def test_resolves_relative_input_path(self, tmp_path: Path, monkeypatch) -> None:
        # validate_docx_path expands and resolves, so a relative path
        # against an actual file should be accepted (it just won't be the
        # path that ends up in the artifact — that's always absolute).
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        monkeypatch.chdir(tmp_path)
        tool = AdeuAcceptAllChanges()
        # The PK header isn't a valid DOCX so RedlineEngine will fail —
        # but we expect it to fail INSIDE the engine, not at the path
        # validator. Use the wrap to assert that.
        with pytest.raises(ToolException) as excinfo:
            tool.invoke({"file_path": "doc.docx"})
        # Failure should NOT be about path resolution.
        assert "does not exist" not in str(excinfo.value)
        assert "must be a .docx file" not in str(excinfo.value)
