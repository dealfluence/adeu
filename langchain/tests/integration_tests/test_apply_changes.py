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


class TestAdeuApplyChangesBehavior:
    def test_successful_modify_writes_file_and_artifact(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # _UNIQUE_TARGET appears once in golden.docx outside any
        # CriticMarkup wrapper, so the edit must succeed cleanly.
        tool = AdeuApplyChanges()
        tool_call = {
            "name": "adeu_apply_changes",
            "args": {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": _UNIQUE_TARGET,
                        "new_text": _REPLACEMENT,
                    }
                ],
                "output_path": str(output_path),
            },
            "id": "test-apply-1",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        # If this fails, surface the engine's complaint so the next
        # adjustment is informed by the real reason, not a guess.
        assert msg.artifact["success"] is True, (
            f"apply_changes rejected the edit. Validation errors: "
            f"{msg.artifact.get('validation_errors')!r}. Content head: "
            f"{msg.content[:300]}"
        )
        assert msg.artifact["edits_applied"] == 1
        assert msg.artifact["edits_skipped"] == 0
        assert msg.artifact["output_path"] == str(output_path)
        assert output_path.exists()

    def test_modification_is_visible_in_output(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # Round-trip: apply edit → read result → verify the new text
        # appears in the projected output (as a tracked insertion).
        apply_tool = AdeuApplyChanges()
        apply_msg_content = apply_tool.invoke(
            {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": _UNIQUE_TARGET,
                        "new_text": _REPLACEMENT,
                    }
                ],
                "output_path": str(output_path),
            }
        )
        # If apply_changes rejected the edit, the round-trip is
        # meaningless. Surface the rejection reason rather than letting
        # the next read fail with a confusing "file not found".
        assert output_path.exists(), (
            f"apply_changes did not write an output file. Content head: "
            f"{apply_msg_content[:300]}"
        )

        read_tool = AdeuReadDocx()
        raw = read_tool.invoke({"file_path": str(output_path), "clean_view": False})
        assert _REPLACEMENT in raw, (
            f"New text {_REPLACEMENT!r} not found in read-back of edited " "file."
        )

    def test_input_file_is_not_modified(
        self, working_docx: Path, output_path: Path
    ) -> None:
        original_bytes = working_docx.read_bytes()
        tool = AdeuApplyChanges()
        tool.invoke(
            {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": _UNIQUE_TARGET,
                        "new_text": _REPLACEMENT,
                    }
                ],
                "output_path": str(output_path),
            }
        )
        assert working_docx.read_bytes() == original_bytes

    def test_batch_validation_error_returns_failure_artifact(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuApplyChanges()
        tool_call = {
            "name": "adeu_apply_changes",
            "args": {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": "PHRASE_NOT_IN_DOCUMENT_xyz123",
                        "new_text": "anything",
                    }
                ],
                "output_path": str(output_path),
            },
            "id": "test-apply-fail",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert msg.artifact["success"] is False
        assert msg.artifact["output_path"] is None
        assert msg.artifact["validation_errors"]
        assert "Batch rejected" in msg.content
        assert not output_path.exists()

    def test_schema_validation_failure_returns_failure_artifact(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuApplyChanges()
        tool_call = {
            "name": "adeu_apply_changes",
            "args": {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [{"type": "invalid_change_type_xyz"}],
                "output_path": str(output_path),
            },
            "id": "test-apply-schema-fail",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert msg.artifact["success"] is False
        assert msg.artifact["output_path"] is None
        assert "Batch rejected during schema validation" in msg.content
        assert not output_path.exists()

    @pytest.mark.asyncio
    async def test_ainvoke_applies_edit(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuApplyChanges()
        result = await tool.ainvoke(
            {
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": _UNIQUE_TARGET,
                        "new_text": _REPLACEMENT,
                    }
                ],
                "output_path": str(output_path),
            }
        )
        assert output_path.exists(), (
            f"ainvoke did not write an output file. Result head: " f"{result[:300]}"
        )
        assert "Batch complete" in result
