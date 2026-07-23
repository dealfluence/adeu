# FILE: langchain/tests/unit_tests/test_diff_docx.py
"""Unit tests for `AdeuDiffDocx` — input validation and schema shape only.

End-to-end diff behavior against real DOCX content is covered by the
integration tests (Step 7). These tests verify the tool refuses bad input
cleanly without touching the diff engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu import AdeuDiffDocx, AdeuDiffDocxInput


class TestAdeuDiffDocxSchema:
    def test_name_is_snake_case(self) -> None:
        tool = AdeuDiffDocx()
        assert tool.name == "adeu_diff_docx"
        assert " " not in tool.name

    def test_description_mentions_word_patch_format(self) -> None:
        # The custom diff format is the tool's most distinctive trait;
        # if the description loses that name, models will be confused
        # about what it outputs.
        tool = AdeuDiffDocx()
        assert "Word Patch" in tool.description

    def test_args_schema_required_fields(self) -> None:
        # Both paths are required; compare_clean has a default.
        schema = AdeuDiffDocxInput.model_json_schema()
        assert set(schema["required"]) == {
            "reasoning",
            "original_path",
            "modified_path",
        }

    def test_args_schema_rejects_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            AdeuDiffDocxInput.model_validate(
                {
                    "reasoning": "test",
                    "original_path": "/a.docx",
                    "modified_path": "/b.docx",
                    "language": "en",
                }
            )

    def test_response_format_is_content(self) -> None:
        # Diff returns a string only — no artifact.
        tool = AdeuDiffDocx()
        assert tool.response_format == "content"


class TestAdeuDiffDocxValidation:
    def test_rejects_nonexistent_original(self, tmp_path: Path) -> None:
        # Make modified valid so we know the failure is about original.
        mod = tmp_path / "mod.docx"
        mod.write_bytes(b"PK")
        tool = AdeuDiffDocx()
        with pytest.raises(ToolException, match="original document"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "original_path": "/nonexistent/orig.docx",
                    "modified_path": str(mod),
                }
            )

    def test_rejects_nonexistent_modified(self, tmp_path: Path) -> None:
        orig = tmp_path / "orig.docx"
        orig.write_bytes(b"PK")
        tool = AdeuDiffDocx()
        with pytest.raises(ToolException, match="modified document"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "original_path": str(orig),
                    "modified_path": "/nonexistent/mod.docx",
                }
            )

    def test_rejects_non_docx_extension(self, tmp_path: Path) -> None:
        orig = tmp_path / "orig.txt"
        orig.write_text("plaintext")
        mod = tmp_path / "mod.docx"
        mod.write_bytes(b"PK")
        tool = AdeuDiffDocx()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "original_path": str(orig),
                    "modified_path": str(mod),
                }
            )

    def test_identical_paths_short_circuit(self, tmp_path: Path) -> None:

        same = tmp_path / "same.docx"
        same.write_bytes(b"PK")
        tool = AdeuDiffDocx()
        result = tool.invoke(
            {
                "reasoning": "test",
                "original_path": str(same),
                "modified_path": str(same),
            }
        )
        assert "No text differences found" in result

    def test_diff_format_options(self) -> None:
        input_data = AdeuDiffDocxInput(
            reasoning="test",
            original_path="/tmp/orig.docx",
            modified_path="/tmp/mod.docx",
            diff_format="unified",
        )
        assert input_data.diff_format == "unified"
