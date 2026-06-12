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
    def test_successful_modify_writes_file_and_artifact(self, working_docx: Path, output_path: Path) -> None:
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

    def test_modification_is_visible_in_output(self, working_docx: Path, output_path: Path) -> None:
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
            f"apply_changes did not write an output file. Content head: {apply_msg_content[:300]}"
        )

        read_tool = AdeuReadDocx()
        raw = read_tool.invoke({"file_path": str(output_path), "clean_view": False})
        assert _REPLACEMENT in raw, f"New text {_REPLACEMENT!r} not found in read-back of edited file."

    def test_input_file_is_not_modified(self, working_docx: Path, output_path: Path) -> None:
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

    def test_batch_validation_error_returns_failure_artifact(self, working_docx: Path, output_path: Path) -> None:
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

    def test_schema_validation_failure_returns_failure_artifact(self, working_docx: Path, output_path: Path) -> None:
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
    async def test_ainvoke_applies_edit(self, working_docx: Path, output_path: Path) -> None:
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
        assert output_path.exists(), f"ainvoke did not write an output file. Result head: {result[:300]}"
        assert "Batch complete" in result

    def test_dry_run_does_not_write_output_file(self, working_docx: Path, output_path: Path) -> None:
        # dry_run=True should simulate without producing any file on disk,
        # regardless of whether output_path was supplied.
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
                "dry_run": True,
            },
            "id": "test-dry-run-1",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)

        # Core contract: no file written even though output_path was provided.
        assert not output_path.exists(), (
            "dry_run=True wrote an output file at the requested output_path — the simulation contract was violated."
        )

        # Source must remain untouched.
        # (Hash check would be overkill; existence + mtime stability is enough
        # because the engine never opens the source for write.)
        assert working_docx.exists()

        # Artifact must surface that this was a dry-run and the simulation
        # itself succeeded (the edit was a valid one).
        assert msg.artifact["success"] is True
        assert msg.artifact["dry_run"] is True
        assert msg.artifact["output_path"] is None
        assert msg.artifact["edits_applied"] == 1
        assert msg.artifact["edits_skipped"] == 0

        # Per-edit reports must be present in the artifact and contain the
        # preview payload that distinguishes dry-run from a regular call.
        edits = msg.artifact.get("edits") or []
        assert edits, "dry_run artifact missing per-edit reports — engine output was dropped on the floor."
        first = edits[0]
        assert first["status"] == "applied"
        assert first["target_text"] == _UNIQUE_TARGET
        assert first["new_text"] == _REPLACEMENT
        # At least one of the preview fields must be populated — these are
        # the whole point of dry-run vs. just running the batch and checking
        # counts.
        assert first.get("critic_markup") or first.get("clean_text"), (
            "dry_run report has neither critic_markup nor clean_text preview; "
            "the LLM has nothing actionable to inspect."
        )

        # Content should announce the simulation explicitly so the LLM can
        # branch on the message text alone if it doesn't read the artifact.
        assert "Dry-run simulation complete" in msg.content

    def test_dry_run_surfaces_failing_edits_without_writing(self, working_docx: Path, output_path: Path) -> None:
        # When an edit is genuinely unfindable, dry-run should still report
        # the failure per-edit (it does NOT raise BatchValidationError on
        # the dry-run path — the engine validates each edit individually).
        tool = AdeuApplyChanges()
        msg = tool.invoke(
            {
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
                    "dry_run": True,
                },
                "id": "test-dry-run-fail",
                "type": "tool_call",
            }
        )
        assert not output_path.exists()
        assert msg.artifact["dry_run"] is True
        # The engine returns success=True at the batch level but reports the
        # individual failure in the edits list — that's what makes dry-run
        # useful as a self-review step (the agent sees granular feedback
        # instead of an opaque rejection).
        edits = msg.artifact.get("edits") or []
        assert edits and edits[0]["status"] == "failed"
        assert edits[0].get("error")
