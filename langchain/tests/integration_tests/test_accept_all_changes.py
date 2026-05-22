# FILE: langchain/tests/integration_tests/test_accept_all_changes.py
"""Integration tests for AdeuAcceptAllChanges."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_adeu import AdeuAcceptAllChanges, AdeuReadDocx


class TestAdeuAcceptAllChangesStandard(ToolsIntegrationTests):
    """LangChain-tests integration suite for AdeuAcceptAllChanges.

    The standard suite invokes once with the example dict. We point at
    golden.docx (which has tracked changes) and let the tool write to
    its default output path next to the source. Because the default
    output is `<stem>_clean.docx`, repeated test runs will overwrite
    the same file — that's fine in test temp dirs but would pollute the
    shared fixture dir. To avoid polluting `shared/fixtures/`, the
    example uses an explicit output_path inside the test's tmp dir.
    """

    # Class-level tmp_path holder. The standard suite reads
    # tool_invoke_params_example as a property without fixtures, so we
    # populate the path from a pytest fixture at session scope and
    # reference it from the property.
    _tmp_output: Path | None = None

    @pytest.fixture(autouse=True)
    def _setup_tmp_output(self, tmp_path: Path) -> None:
        # Capture per-test tmp_path so tool_invoke_params_example can
        # point at it. The standard suite calls the property once per
        # test, so this gets refreshed correctly.
        type(self)._tmp_output = tmp_path / "accepted_output.docx"

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuAcceptAllChanges

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        fixture = str(repo_root / "shared" / "fixtures" / "golden.docx")
        assert self._tmp_output is not None, (
            "Output path tmp dir not initialized — _setup_tmp_output "
            "fixture did not run."
        )
        return {
            "file_path": fixture,
            "output_path": str(self._tmp_output),
        }


class TestAdeuAcceptAllChangesBehavior:
    def test_default_output_path(self, working_docx: Path) -> None:
        # Default output_path is `<stem>_clean.docx` in the same directory.
        tool = AdeuAcceptAllChanges()
        result = tool.invoke({"file_path": str(working_docx)})
        expected = working_docx.with_name(f"{working_docx.stem}_clean.docx")
        assert (
            expected.exists()
        ), f"Expected default output at {expected}, but it was not created."
        assert str(expected) in result

    def test_explicit_output_path_is_written(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuAcceptAllChanges()
        tool_call = {
            "name": "adeu_accept_all_changes",
            "args": {
                "file_path": str(working_docx),
                "output_path": str(output_path),
            },
            "id": "test-accept-1",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert output_path.exists()
        assert msg.artifact["output_path"] == str(output_path)
        assert msg.artifact["input_path"] == str(working_docx)

    def test_input_file_is_not_modified(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # The source file must be untouched — accept_all_changes is
        # "produce a clean copy", not "modify in place".
        original_bytes = working_docx.read_bytes()
        tool = AdeuAcceptAllChanges()
        tool.invoke({"file_path": str(working_docx), "output_path": str(output_path)})
        assert working_docx.read_bytes() == original_bytes, (
            "Source file was modified by accept_all_changes — should be " "read-only."
        )

    def test_output_has_no_tracked_changes(
        self, working_docx: Path, output_path: Path
    ) -> None:
        # The whole point of accept_all_changes: the output should have
        # no remaining CriticMarkup when read in raw mode.
        accept_tool = AdeuAcceptAllChanges()
        accept_tool.invoke(
            {"file_path": str(working_docx), "output_path": str(output_path)}
        )

        read_tool = AdeuReadDocx()
        raw_after = read_tool.invoke(
            {"file_path": str(output_path), "clean_view": False}
        )

        # No insertion/deletion markup should remain. Comments may or may
        # not survive depending on the engine's final-pass logic, so we
        # only assert on the tracked-change tokens here.
        for token in ("{++", "++}", "{--", "--}"):
            assert token not in raw_after, (
                f"Output still contains {token!r} after accept_all_changes. "
                "Tracked changes were not fully accepted."
            )

    @pytest.mark.asyncio
    async def test_ainvoke_writes_file(
        self, working_docx: Path, output_path: Path
    ) -> None:
        tool = AdeuAcceptAllChanges()
        result = await tool.ainvoke(
            {"file_path": str(working_docx), "output_path": str(output_path)}
        )
        assert output_path.exists()
        assert str(output_path) in result
