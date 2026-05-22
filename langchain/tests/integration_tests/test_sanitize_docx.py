# FILE: langchain/tests/integration_tests/test_sanitize_docx.py
"""Integration tests for AdeuSanitizeDocx — all three modes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool, ToolException
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
            "file_path": fixture,
            "output_path": str(self._tmp_output),
            "accept_all": True,
        }


class TestAdeuSanitizeDocxBehavior:
    def test_full_sanitize_with_accept_all_writes_clean_file(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuSanitizeDocx()
        tool_call = {
            "name": "adeu_sanitize_docx",
            "args": {
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

    def test_full_sanitize_without_accept_all_blocks_with_tool_exception(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # golden.docx has unresolved tracked changes. Without
        # accept_all=True, SanitizeError fires → ToolException.
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match="SanitizeError"):
            tool.invoke(
                {
                    "file_path": str(working_docx),
                    "output_path": str(output_path),
                }
            )
        assert not output_path.exists(), (
            "Output file was written despite SanitizeError — sanitize "
            "engine failed but artifact still leaked."
        )

    def test_keep_markup_mode_writes_redline(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # keep_markup=True doesn't require accept_all because tracked
        # changes are explicitly preserved.
        tool = AdeuSanitizeDocx()
        tool_call = {
            "name": "adeu_sanitize_docx",
            "args": {
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
        # field — `tracked_changes_found` is the fie
