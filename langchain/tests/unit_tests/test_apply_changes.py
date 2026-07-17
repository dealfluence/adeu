# FILE: langchain/tests/unit_tests/test_apply_changes.py
"""Unit tests for `AdeuApplyChanges` — schema and input validation only.

End-to-end batch application against a real DOCX is covered by
integration tests. These tests verify the tool's contract surface:
schema correctness, path validation, empty-input handling, and the
schema-level validation failure path (which short-circuits before the
engine is touched).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu import AdeuApplyChanges, AdeuApplyChangesInput
from langchain_adeu.apply_changes import _resolve_output_path


class TestAdeuApplyChangesSchema:
    def test_name_is_snake_case(self) -> None:
        tool = AdeuApplyChanges()
        assert tool.name == "adeu_apply_changes"

    def test_description_explains_change_types(self) -> None:
        # Description must enumerate the supported change types so the
        # LLM doesn't have to guess. If we add a new type, this assertion
        # serves as a reminder to update the description.
        tool = AdeuApplyChanges()
        desc = tool.description
        for change_type in (
            "modify",
            "accept",
            "reject",
            "reply",
            "insert_row",
            "delete_row",
        ):
            assert change_type in desc, f"description missing change type '{change_type}'"

    def test_description_warns_about_batch_semantics(self) -> None:
        # Batches apply sequentially (chaining supported); if the contract is
        # missing from the description, LLMs won't know a later edit must
        # target the text as it reads AFTER the preceding edits.
        tool = AdeuApplyChanges()
        assert "SEQUENTIALLY" in tool.description or "sequential" in tool.description.lower()

    def test_description_warns_about_id_volatility(self) -> None:
        # IDs shift between document states. If the warning is missing,
        # agents will cache Chg:N from one turn and try to accept/reject
        # in the next, hitting unrelated changes.
        tool = AdeuApplyChanges()
        assert "ID VOLATILITY" in tool.description or "shift between" in tool.description

    def test_args_schema_required_fields(self) -> None:
        # file_path, author_name, and changes are required. output_path
        # has a default of None.
        schema = AdeuApplyChangesInput.model_json_schema()
        required = set(schema["required"])
        assert required == {"reasoning", "file_path", "author_name", "changes"}

    def test_args_schema_rejects_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            AdeuApplyChangesInput.model_validate(
                {
                    "reasoning": "test",
                    "file_path": "/a.docx",
                    "author_name": "AI",
                    "changes": [],
                    "policy": "strict",
                }
            )

    def test_response_format_is_content_and_artifact(self) -> None:
        tool = AdeuApplyChanges()
        assert tool.response_format == "content_and_artifact"

    def test_args_schema_has_dry_run_field_with_default_false(self) -> None:
        # dry_run is optional with default False — existing call sites
        # that never pass it must continue to perform real writes.
        schema = AdeuApplyChangesInput.model_json_schema()
        properties = schema["properties"]
        assert "dry_run" in properties
        assert properties["dry_run"].get("default") is False
        assert "dry_run" not in schema["required"]

    def test_dry_run_field_rejects_non_bool(self) -> None:
        # Pydantic should refuse strings / ints / None for a bool field.
        # (Strings like "true" technically coerce; we don't want LLM
        # ambiguity so confirm the strict-typed path raises.)
        with pytest.raises(ValueError):
            AdeuApplyChangesInput.model_validate(
                {
                    "reasoning": "test",
                    "file_path": "/a.docx",
                    "author_name": "AI",
                    "changes": [{"type": "modify", "target_text": "x", "new_text": "y"}],
                    "dry_run": "not_a_bool_xyz",
                }
            )


class TestAdeuApplyChangesValidation:
    def test_rejects_nonexistent_input(self) -> None:
        tool = AdeuApplyChanges()
        with pytest.raises(ToolException, match="does not exist"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": "/nonexistent/file.docx",
                    "author_name": "AI",
                    "changes": [{"type": "modify", "target_text": "x", "new_text": "y"}],
                }
            )

    def test_rejects_non_docx_input(self, tmp_path: Path) -> None:
        bad = tmp_path / "doc.txt"
        bad.write_text("nope")
        tool = AdeuApplyChanges()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(bad),
                    "author_name": "AI",
                    "changes": [{"type": "modify", "target_text": "x", "new_text": "y"}],
                }
            )

    def test_rejects_blank_author(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuApplyChanges()
        with pytest.raises(ToolException, match="author_name cannot be empty"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "author_name": "   ",
                    "changes": [{"type": "modify", "target_text": "x", "new_text": "y"}],
                }
            )

    def test_rejects_empty_changes_list(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuApplyChanges()
        with pytest.raises(ToolException, match="changes list cannot be empty"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "author_name": "AI",
                    "changes": [],
                }
            )

    def test_rejects_overwrite_of_unprocessed_input(self, tmp_path: Path) -> None:
        # Source stem is plain "draft" (no _processed/_redlined suffix),
        # so the overwrite guard should reject same-path output.
        src = tmp_path / "draft.docx"
        src.write_bytes(b"PK")
        tool = AdeuApplyChanges()
        with pytest.raises(ToolException, match="must differ from input path"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "author_name": "AI",
                    "changes": [{"type": "modify", "target_text": "x", "new_text": "y"}],
                    "output_path": str(src),
                }
            )

    def test_schema_validation_failure_returns_content_not_exception(self, tmp_path: Path) -> None:
        # A change with an invalid type should be caught by the
        # TypeAdapter and returned as content (success=False), NOT
        # raised as a ToolException. This is the recoverable-validation
        # contract the LLM relies on.
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuApplyChanges()

        tool_call = {
            "name": "adeu_apply_changes",
            "args": {
                "reasoning": "test",
                "file_path": str(src),
                "author_name": "AI",
                "changes": [{"type": "this_is_not_a_real_type", "garbage": "ignored"}],
            },
            "id": "test-validation-failure",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)

        # The ToolMessage should carry our failure artifact, NOT throw.
        assert msg.artifact["success"] is False
        assert msg.artifact["output_path"] is None
        assert msg.artifact["validation_errors"] is not None
        assert len(msg.artifact["validation_errors"]) > 0
        assert "Batch rejected" in msg.content


class TestAdeuApplyChangesOutputPathLogic:
    def test_default_output_for_plain_stem(self, tmp_path: Path) -> None:

        src = tmp_path / "draft.docx"
        src.write_bytes(b"PK")
        target = _resolve_output_path(src, None)
        assert target.name == "draft_processed.docx"
        assert target.parent == src.parent

    def test_default_output_for_processed_stem_overwrites(self, tmp_path: Path) -> None:
        # Iterating on already-processed work: overwrite in place.

        src = tmp_path / "draft_processed.docx"
        src.write_bytes(b"PK")
        target = _resolve_output_path(src, None)
        assert target == src

    def test_default_output_for_redlined_stem_overwrites(self, tmp_path: Path) -> None:

        src = tmp_path / "contract_redlined.docx"
        src.write_bytes(b"PK")
        target = _resolve_output_path(src, None)
        assert target == src

    def test_explicit_same_path_allowed_for_processed_stem(self, tmp_path: Path) -> None:
        # Explicit overwrite of a processed file is allowed (iteration).

        src = tmp_path / "draft_processed.docx"
        src.write_bytes(b"PK")
        target = _resolve_output_path(src, str(src))
        assert target == src
