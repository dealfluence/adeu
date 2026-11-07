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


class TestAdeuDiffDocxRedlineOutput:
    """`output_path` writes the comparison as a tracked-changes redline (same contract as `adeu diff -o`)."""

    @staticmethod
    def _pair(tmp_path: Path) -> tuple[Path, Path]:
        from docx import Document

        v1, v2 = tmp_path / "v1.docx", tmp_path / "v2.docx"
        for path, word in ((v1, "thirty (30)"), (v2, "sixty (60)")):
            d = Document()
            d.add_paragraph(f"Invoices are payable within {word} days.")
            d.save(str(path))
        return v1, v2

    def test_writes_redline_matching_modified(self, tmp_path: Path) -> None:
        from io import BytesIO

        from adeu.ingest import extract_text_from_stream

        v1, v2 = self._pair(tmp_path)
        out = tmp_path / "redline.docx"
        result = AdeuDiffDocx().invoke(
            {
                "reasoning": "test",
                "original_path": str(v1),
                "modified_path": str(v2),
                "output_path": str(out),
                "author_name": "Deal Team",
            }
        )
        assert "Redline saved to" in result
        assert extract_text_from_stream(BytesIO(out.read_bytes()), clean_view=True) == extract_text_from_stream(
            BytesIO(v2.read_bytes()), clean_view=True
        )

    def test_refuses_to_overwrite_original(self, tmp_path: Path) -> None:
        v1, v2 = self._pair(tmp_path)
        before = v1.read_bytes()
        with pytest.raises(ToolException, match="same file as the original"):
            AdeuDiffDocx().invoke(
                {"reasoning": "test", "original_path": str(v1), "modified_path": str(v2), "output_path": str(v1)}
            )
        assert v1.read_bytes() == before


def test_dead_helper_is_gone() -> None:
    import langchain_adeu.diff_docx as mod

    assert not hasattr(mod, "_read_text"), "unused helper still present"
