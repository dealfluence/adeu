# FILE: langchain/tests/integration_tests/test_apply_changes.py
"""Integration tests for AdeuApplyChanges — actually runs the engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_adeu import AdeuApplyChanges, AdeuReadDocx

_UNIQUE_TARGET = "document"
_REPLACEMENT = "agreement"


class TestAdeuApplyChangesStandard(ToolsIntegrationTests):
    """LangChain-tests integration suite for AdeuApplyChanges."""

    _tmp_output: Path | None = None

    @pytest.fixture(autouse=True)
    def _setup_tmp_output(self, tmp_path: Path) -> None:
        type(self)._tmp_output = tmp_path / "applied_output.docx"

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuApplyChanges

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        fixture = str(repo_root / "shared" / "fixtures" / "golden.docx")
        assert self._tmp_output is not None
        return {
            "reasoning": "Applying an integration-test edit to the golden fixture.",
            "file_path": fixture,
            "author_name": "Integration Test",
            "changes": [
                {
                    "type": "modify",
                    "target_text": _UNIQUE_TARGET,
                    "new_text": _REPLACEMENT,
                    "comment": "Integration test edit.",
                }
            ],
            "output_path": str(self._tmp_output),
        }


def _make_args(
    file_path: Path,
    changes: list,
    output_path: Path | None = None,
    reasoning: str = "test",
    author: str = "AI Reviewer",
) -> dict:
    args = {
        "reasoning": reasoning,
        "file_path": str(file_path),
        "author_name": author,
        "changes": changes,
    }
    if output_path is not None:
        args["output_path"] = str(output_path)
    return args


def _make_call(
    file_path: Path,
    changes: list,
    output_path: Path | None = None,
    call_id: str = "test-call",
) -> dict:
    return {
        "name": "adeu_apply_changes",
        "args": _make_args(file_path, changes, output_path),
        "id": call_id,
        "type": "tool_call",
    }


class TestAdeuApplyChangesBehavior:
    def test_successful_modify_writes_file_and_artifact(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        tool_call = _make_call(
            working_docx,
            [{"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT}],
            output_path,
            "test-apply-1",
        )
        msg = tool.invoke(tool_call)
        assert msg.artifact["success"] is True, (
            f"apply_changes rejected the edit. Validation errors: "
            f"{msg.artifact.get('validation_errors')!r}. Content head: "
            f"{msg.content[:300]}"
        )
        assert msg.artifact["edits_applied"] == 1
        assert msg.artifact["edits_skipped"] == 0
        assert msg.artifact["output_path"] == str(output_path)
        assert output_path.exists()

    def test_modification_is_visible_in_output(self, working_docx: Path, output_path: Path) -> None:
        apply_tool = AdeuApplyChanges()
        apply_msg_content = apply_tool.invoke(
            _make_args(
                working_docx,
                [{"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT}],
                output_path,
            )
        )
        assert output_path.exists(), (
            f"apply_changes did not write an output file. Content head: {apply_msg_content[:300]}"
        )

        read_tool = AdeuReadDocx()
        raw = read_tool.invoke({"reasoning": "test", "file_path": str(output_path), "clean_view": False})
        assert _REPLACEMENT in raw, f"New text {_REPLACEMENT!r} not found in read-back of edited file."

    def test_input_file_is_not_modified(self, working_docx: Path, output_path: Path) -> None:
        original_bytes = working_docx.read_bytes()
        tool = AdeuApplyChanges()
        tool.invoke(
            _make_args(
                working_docx,
                [{"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT}],
                output_path,
            )
        )
        assert working_docx.read_bytes() == original_bytes

    def test_batch_validation_error_returns_failure_artifact(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        tool_call = _make_call(
            working_docx,
            [{"type": "modify", "target_text": "PHRASE_NOT_IN_DOCUMENT_xyz123", "new_text": "anything"}],
            output_path,
            "test-apply-fail",
        )
        msg = tool.invoke(tool_call)
        assert msg.artifact["success"] is False
        assert msg.artifact["output_path"] is None
        assert msg.artifact["validation_errors"]
        assert "Batch rejected" in msg.content
        assert not output_path.exists()

    def test_schema_validation_failure_returns_failure_artifact(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        tool_call = _make_call(
            working_docx,
            [{"type": "invalid_change_type_xyz"}],
            output_path,
            "test-apply-schema-fail",
        )
        msg = tool.invoke(tool_call)
        assert msg.artifact["success"] is False
        assert msg.artifact["output_path"] is None
        assert "Batch rejected during schema validation" in msg.content
        assert not output_path.exists()

    @pytest.mark.asyncio
    async def test_ainvoke_applies_edit(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        result = await tool.ainvoke(
            _make_args(
                working_docx,
                [{"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT}],
                output_path,
            )
        )
        assert output_path.exists(), f"ainvoke did not write an output file. Result head: {result[:300]}"
        assert "Batch complete" in result

    def test_successful_batch_carries_per_edit_preview_reports(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        tool_call = _make_call(
            working_docx,
            [{"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT}],
            output_path,
            "test-wet-report-1",
        )
        msg = tool.invoke(tool_call)

        assert msg.artifact["success"] is True
        assert msg.artifact["output_path"] == str(output_path)
        assert output_path.exists()
        assert msg.artifact["edits_applied"] == 1
        assert msg.artifact["edits_skipped"] == 0

        # Per-edit reports must be present in the artifact with preview
        # payload the LLM can inspect.
        edits = msg.artifact.get("edits") or []
        assert edits, "artifact missing per-edit reports — engine output was dropped on the floor."
        first = edits[0]
        assert first["status"] == "applied"
        assert first["target_text"] == _UNIQUE_TARGET
        assert first["new_text"] == _REPLACEMENT
        assert first.get("critic_markup") or first.get("clean_text"), (
            "per-edit report has neither critic_markup nor clean_text preview; "
            "the LLM has nothing actionable to inspect."
        )

        # Content should announce the committed write explicitly.
        assert "Batch complete" in msg.content

    def test_stale_id_error_points_at_the_langchain_read_tool(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        msg = tool.invoke(
            {
                "name": "adeu_apply_changes",
                "args": {
                    "reasoning": "test",
                    "file_path": str(working_docx),
                    "author_name": "AI Reviewer",
                    "changes": [{"type": "reply", "target_id": "Com:9999", "text": "hello"}],
                    "output_path": str(output_path),
                },
                "id": "test-stale-id",
                "type": "tool_call",
            }
        )
        assert msg.artifact["success"] is False
        blob = msg.content + repr(msg.artifact.get("validation_errors"))
        assert "adeu_read_docx" in blob, "engine error still advertises the CLI; id_discovery_hint was not passed"
        assert "adeu extract" not in blob

    def test_partial_success_salvages_valid_edits(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        msg = tool.invoke(
            {
                "name": "adeu_apply_changes",
                "args": {
                    "reasoning": "test",
                    "file_path": str(working_docx),
                    "author_name": "AI Reviewer",
                    "changes": [
                        {"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT},
                        {"type": "modify", "target_text": "PHRASE_NOT_IN_DOCUMENT_xyz123", "new_text": "x"},
                    ],
                    "output_path": str(output_path),
                },
                "id": "test-partial",
                "type": "tool_call",
            }
        )
        assert msg.artifact["success"] is True
        assert msg.artifact["status"] == "partial"
        assert msg.artifact["edits_applied"] == 1
        assert len(msg.artifact["failed"]) == 1
        assert msg.artifact["failed"][0]["index"] == 1
        assert output_path.exists()
        assert msg.content.startswith("PARTIAL:")

    def test_partial_false_is_transactional(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        msg = tool.invoke(
            {
                "name": "adeu_apply_changes",
                "args": {
                    "reasoning": "test",
                    "file_path": str(working_docx),
                    "author_name": "AI Reviewer",
                    "changes": [
                        {"type": "modify", "target_text": _UNIQUE_TARGET, "new_text": _REPLACEMENT},
                        {"type": "modify", "target_text": "PHRASE_NOT_IN_DOCUMENT_xyz123", "new_text": "x"},
                    ],
                    "output_path": str(output_path),
                    "partial": False,
                },
                "id": "test-transactional",
                "type": "tool_call",
            }
        )
        assert msg.artifact["success"] is False
        assert not output_path.exists()
        assert "Batch rejected" in msg.content

    def test_report_carries_page_and_heading_context(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuApplyChanges()
        msg = tool.invoke(
            {
                "name": "adeu_apply_changes",
                "args": {
                    "reasoning": "test",
                    "file_path": str(working_docx),
                    "author_name": "AI Reviewer",
                    "changes": [
                        {
                            "type": "modify",
                            "target_text": _UNIQUE_TARGET,
                            "new_text": _REPLACEMENT,
                            "comment": "Why this changed.",
                        }
                    ],
                    "output_path": str(output_path),
                },
                "id": "test-rich-report",
                "type": "tool_call",
            }
        )
        first = msg.artifact["edits"][0]
        for key in ("pages", "heading_path", "match_mode", "occurrences_modified", "comment"):
            assert key in first, f"per-edit report dropped engine field {key!r}"
        assert first["comment"] == "Why this changed."
        assert "Mode: `strict`" in msg.content
        assert 'Comment: "Why this changed."' in msg.content
        assert msg.artifact["occurrences_modified"] >= 1
        assert "actions_already_resolved" in msg.artifact
