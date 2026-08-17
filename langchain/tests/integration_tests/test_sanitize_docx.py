# FILE: langchain/tests/integration_tests/test_sanitize_docx.py
"""Integration tests for AdeuSanitizeDocx — all three modes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_adeu import AdeuSanitizeDocx


class TestAdeuSanitizeDocxStandard(ToolsIntegrationTests):
    """LangChain-tests integration suite for AdeuSanitizeDocx."""

    _tmp_output: Path | None = None

    @pytest.fixture(autouse=True)
    def _setup_tmp_output(self, tmp_path: Path) -> None:
        type(self)._tmp_output = tmp_path / "sanitized_output.docx"

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuSanitizeDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        fixture = str(repo_root / "shared" / "fixtures" / "golden.docx")
        assert self._tmp_output is not None
        return {
            "reasoning": "Sanitizing the golden fixture for the suite.",
            "file_path": fixture,
            "output_path": str(self._tmp_output),
            "accept_all": True,
        }


class TestAdeuSanitizeDocxBehavior:
    def test_full_sanitize_with_accept_all_writes_clean_file(self, working_docx: Path, output_path: Path) -> None:
        tool = AdeuSanitizeDocx()
        tool_call = {
            "name": "adeu_sanitize_docx",
            "args": {
                "reasoning": "test",
                "file_path": str(working_docx),
                "output_path": str(output_path),
                "accept_all": True,
            },
            "id": "test-sanitize-1",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert output_path.exists()
        assert msg.artifact["status"] in {"clean", "clean_with_warnings"}
        assert msg.artifact["output_path"] == str(output_path)

    def test_full_sanitize_without_accept_all_returns_blocked_payload(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # golden.docx has unresolved tracked changes. A refusal is a normal
        # payload with status="blocked" (sanitize.py:125-137), not a tool
        # failure — the agent must be able to read the reason and retry with
        # accept_all=True or keep_markup=True.
        tool = AdeuSanitizeDocx()
        msg = tool.invoke(
            {
                "name": "adeu_sanitize_docx",
                "args": {
                    "reasoning": "test",
                    "file_path": str(working_docx),
                    "output_path": str(output_path),
                },
                "id": "test-sanitize-blocked",
                "type": "tool_call",
            }
        )
        assert msg.artifact["status"] == "blocked"
        assert msg.artifact["output_path"] is None
        assert "unresolved tracked changes" in msg.content
        assert not output_path.exists()

    def test_keep_markup_mode_writes_redline(self, working_docx: Path, output_path: Path) -> None:
        # keep_markup=True doesn't require accept_all because tracked
        # changes are explicitly preserved.
        tool = AdeuSanitizeDocx()
        tool_call = {
            "name": "adeu_sanitize_docx",
            "args": {
                "reasoning": "test",
                "file_path": str(working_docx),
                "output_path": str(output_path),
                "keep_markup": True,
                "author": "AI Reviewer",
            },
            "id": "test-sanitize-keep",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert output_path.exists()
        assert msg.artifact["status"] in {"clean", "clean_with_warnings"}
        # We assert on `tracked_changes_found` rather than a "kept"
        # field — `tracked_changes_found` is the count of changes preserved.
        assert msg.artifact.get("tracked_changes_found", 0) > 0
