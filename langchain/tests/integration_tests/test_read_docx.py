# FILE: langchain/tests/integration_tests/test_read_docx.py
"""Integration tests for AdeuReadDocx — actually loads golden.docx."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_adeu import AdeuReadDocx


class TestAdeuReadDocxStandard(ToolsIntegrationTests):
    """LangChain-tests' canonical integration suite for AdeuReadDocx.

    Verifies that:
      - `invoke` with a ToolCall returns valid ToolMessage content.
      - `ainvoke` does the same async.
      - `invoke` with raw kwargs does not raise.

    We point `tool_invoke_params_example` at the real `golden.docx`
    so the suite actually exercises the read pipeline, not just path
    validation.
    """

    @pytest.fixture
    def golden_docx_path(self, request: pytest.FixtureRequest) -> Path:

        repo_root = Path(__file__).resolve().parents[3]
        p = repo_root / "shared" / "fixtures" / "golden.docx"
        if not p.exists():
            pytest.skip(f"Golden fixture not found at {p}")
        return p

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuReadDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:

        repo_root = Path(__file__).resolve().parents[3]
        return {
            "file_path": str(repo_root / "shared" / "fixtures" / "golden.docx"),
            "clean_view": False,
            "mode": "full",
            "page": 1,
        }


class TestAdeuReadDocxBehavior:
    """Behavioral assertions specific to AdeuReadDocx."""

    def test_returns_clean_markdown_string(self, golden_docx_path: Path) -> None:
        # Regular invoke should return a plain string, not a repr of
        # content blocks. This is the same bug we caught manually in
        # Step 2 — having a test guarantees it can't regress.
        tool = AdeuReadDocx()
        result = tool.invoke({"file_path": str(golden_docx_path)})
        assert isinstance(result, str)
        assert "TextContent(" not in result, (
            "Output contains FastMCP block repr — content extraction "
            "regressed. Expected plain Markdown string."
        )
        assert "File Path:" in result, "Expected the file-path hint prefix."

    def test_tool_call_returns_artifact_with_expected_keys(
        self, golden_docx_path: Path
    ) -> None:
        # ToolCall invoke is the path agents actually take. Verify the
        # ToolMessage carries the artifact dict with the contract keys.
        tool = AdeuReadDocx()
        tool_call = {
            "name": "adeu_read_docx",
            "args": {"file_path": str(golden_docx_path)},
            "id": "test-call-1",
            "type": "tool_call",
        }
        msg = tool.invoke(tool_call)
        assert msg.artifact is not None
        assert set(msg.artifact.keys()) >= {"markdown", "title", "file_path"}

    def test_clean_view_strips_critic_markup(self, golden_docx_path: Path) -> None:
        # clean_view=True simulates "Accept All Changes". The output
        # should not contain CriticMarkup tokens.
        tool = AdeuReadDocx()
        clean = tool.invoke({"file_path": str(golden_docx_path), "clean_view": True})
        # Strip the file-path prefix line which doesn't count as content.
        body = clean.split("\n\n", 1)[1] if "\n\n" in clean else clean
        for token in ("{++", "++}", "{--", "--}", "{>>", "<<}"):
            assert token not in body, (
                f"clean_view=True returned content containing CriticMarkup "
                f"token {token!r}; expected fully accepted text."
            )

    def test_raw_view_includes_critic_markup_when_present(
        self, golden_docx_path: Path
    ) -> None:

        tool = AdeuReadDocx()
        raw = tool.invoke({"file_path": str(golden_docx_path), "clean_view": False})

        has_any_markup = any(token in raw for token in ("{++", "{--", "{==", "{>>"))
        assert has_any_markup, (
            "Expected CriticMarkup in raw view of golden.docx, but found "
            "none. Either the fixture changed or the projection broke."
        )

    def test_outline_mode_returns_outline_view_banner(
        self, golden_docx_path: Path
    ) -> None:
        tool = AdeuReadDocx()
        result = tool.invoke({"file_path": str(golden_docx_path), "mode": "outline"})
        assert (
            "Outline view" in result
        ), "Expected the outline banner to appear in mode='outline' output."

    @pytest.mark.asyncio
    async def test_ainvoke_matches_invoke(self, golden_docx_path: Path) -> None:
        # The async path must produce the same content as sync (it just
        # offloads to a thread). Drift between the two is a leak risk.
        tool = AdeuReadDocx()
        sync_result = tool.invoke({"file_path": str(golden_docx_path)})
        async_result = await tool.ainvoke({"file_path": str(golden_docx_path)})
        assert sync_result == async_result
