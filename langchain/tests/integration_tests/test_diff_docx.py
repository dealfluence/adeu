# FILE: langchain/tests/integration_tests/test_diff_docx.py
"""Integration tests for AdeuDiffDocx — exercises the diff engine end-to-end.

The standard suite uses the same DOCX for both sides (identity diff
short-circuit). Behavioral tests use an apply_changes-generated modified
copy to verify a real diff round-trips correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_adeu import AdeuApplyChanges, AdeuDiffDocx


class TestAdeuDiffDocxStandard(ToolsIntegrationTests):
    """LangChain-tests' integration suite for AdeuDiffDocx.

    The standard suite passes both paths through the example dict. We
    point both at the same golden.docx, which triggers the diff tool's
    identity short-circuit and returns the "no differences" message.
    The standard suite only validates that the response is a valid tool
    output shape, not its content — so the short-circuit path is fine
    for this purpose.
    """

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuDiffDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        fixture = str(repo_root / "shared" / "fixtures" / "golden.docx")
        return {
            "reasoning": "Diffing the golden fixture against itself for the suite.",
            "original_path": fixture,
            "modified_path": fixture,
            "compare_clean": True,
        }


class TestAdeuDiffDocxBehavior:
    def test_identical_files_returns_no_differences(self, working_docx: Path, golden_docx_path: Path) -> None:
        tool = AdeuDiffDocx()
        result = tool.invoke(
            {
                "reasoning": "test",
                "original_path": str(golden_docx_path),
                "modified_path": str(working_docx),
            }
        )
        assert "No text differences found" in result

    def test_identity_short_circuit(self, golden_docx_path: Path) -> None:
        tool = AdeuDiffDocx()
        result = tool.invoke(
            {
                "reasoning": "test",
                "original_path": str(golden_docx_path),
                "modified_path": str(golden_docx_path),
            }
        )
        assert result == "No text differences found between the documents."

    def test_real_diff_produces_word_patch_format(
        self,
        working_docx: Path,
        golden_docx_path: Path,
        tmp_path: Path,
    ) -> None:
        modified = tmp_path / "modified.docx"
        apply_tool = AdeuApplyChanges()

        apply_result = apply_tool.invoke(
            {
                "reasoning": "test",
                "file_path": str(working_docx),
                "author_name": "Integration Test",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": "document",
                        "new_text": "agreement",
                    }
                ],
                "output_path": str(modified),
            }
        )

        # If the apply itself fails the precondition for this test isn't
        # met. The test is asserting diff behavior, not apply behavior,
        # so we skip rather than fail in that case.
        if "Batch rejected" in apply_result:
            pytest.skip(f"apply_changes precondition failed: {apply_result[:200]} — cannot run diff behavior test.")

        diff_tool = AdeuDiffDocx()
        diff_result = diff_tool.invoke(
            {
                "reasoning": "test",
                "original_path": str(golden_docx_path),
                "modified_path": str(modified),
            }
        )

        # The diff format is documented as "@@ Word Patch @@" — verify
        # the header appears at least once.
        assert "@@ Word Patch @@" in diff_result, (
            "Expected at least one Word Patch hunk header. Got:\n" + diff_result[:500]
        )
        # The original word should appear as a deletion line.
        assert "- document" in diff_result or "-document" in diff_result
        # The replacement should appear as an addition line.
        assert "+ agreement" in diff_result or "+agreement" in diff_result

    def test_structured_changes_returns_appliable_json(
        self,
        working_docx: Path,
        golden_docx_path: Path,
        tmp_path: Path,
    ) -> None:
        # diff_format='structured_changes' is documented (README.md:145) as
        # returning JSON suitable for feeding straight into adeu_apply_changes.
        # The branch had no coverage and raised a TypeError at runtime.
        modified = tmp_path / "modified.docx"
        apply_result = AdeuApplyChanges().invoke(
            {
                "reasoning": "test",
                "file_path": str(working_docx),
                "author_name": "Integration Test",
                "changes": [{"type": "modify", "target_text": "document", "new_text": "agreement"}],
                "output_path": str(modified),
            }
        )
        if "Batch rejected" in apply_result:
            pytest.skip(f"apply_changes precondition failed: {apply_result[:200]} — cannot run diff behavior test.")

        diff_result = AdeuDiffDocx().invoke(
            {
                "reasoning": "test",
                "original_path": str(golden_docx_path),
                "modified_path": str(modified),
                "diff_format": "structured_changes",
            }
        )

        payload = json.loads(diff_result)
        assert payload["changes"], f"expected at least one structured change. Got:\n{diff_result[:500]}"
        assert all("type" in c for c in payload["changes"])
        assert isinstance(payload["warnings"], list)

        # "suitable for feeding directly into adeu_apply_changes": prove it.
        replayed = tmp_path / "replayed.docx"
        replay_result = AdeuApplyChanges().invoke(
            {
                "reasoning": "test",
                "file_path": str(working_docx),
                "author_name": "Integration Test",
                "changes": payload["changes"],
                "output_path": str(replayed),
            }
        )
        assert "Batch rejected" not in replay_result, replay_result[:500]
        assert "Edits: 0 applied" not in replay_result, replay_result[:500]

    def test_raw_diff_never_emits_orphaned_criticmarkup(self, working_docx: Path, output_path: Path) -> None:
        # Build a second version whose tracked-change markup differs, then diff
        # the RAW projections. Every CriticMarkup delimiter in a hunk payload
        # line must be paired (document.py:881-883).
        from langchain_adeu import AdeuApplyChanges

        AdeuApplyChanges().invoke(
            {
                "reasoning": "test",
                "file_path": str(working_docx),
                "author_name": "AI Reviewer",
                "changes": [{"type": "modify", "target_text": "document", "new_text": "agreement"}],
                "output_path": str(output_path),
            }
        )
        diff = AdeuDiffDocx().invoke(
            {
                "reasoning": "test",
                "original_path": str(working_docx),
                "modified_path": str(output_path),
                "compare_clean": False,
            }
        )
        for line in diff.splitlines():
            if not line.startswith(("+", "-")):
                continue
            payload = line[1:]
            openers = payload.count("{++") + payload.count("{--") + payload.count("{==") + payload.count("{>>")
            closers = payload.count("++}") + payload.count("--}") + payload.count("==}") + payload.count("<<}")
            assert openers == closers, f"unbalanced CriticMarkup in hunk line: {line!r}"

    @pytest.mark.asyncio
    async def test_ainvoke_matches_invoke(self, working_docx: Path, golden_docx_path: Path) -> None:
        tool = AdeuDiffDocx()
        args = {
            "reasoning": "test",
            "original_path": str(golden_docx_path),
            "modified_path": str(working_docx),
        }
        sync_result = tool.invoke(args)
        async_result = await tool.ainvoke(args)
        assert sync_result == async_result
