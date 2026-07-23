# FILE: langchain/tests/unit_tests/test_sanitize_docx.py
"""Unit tests for `AdeuSanitizeDocx` — schema and input validation only.

End-to-end sanitization against a real DOCX is covered by integration
tests, because the sanitize engine touches enough subsystems (OPC parts,
relationships, comments, tracked changes) that mocking it doesn't catch
the regressions that matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu import AdeuSanitizeDocx, AdeuSanitizeDocxInput


class TestAdeuSanitizeDocxSchema:
    def test_name_is_snake_case(self) -> None:
        tool = AdeuSanitizeDocx()
        assert tool.name == "adeu_sanitize_docx"

    def test_description_explains_three_modes(self) -> None:
        # Description must surface all three modes so the LLM can pick
        # the right one without trial and error.
        tool = AdeuSanitizeDocx()
        desc = tool.description
        assert "keep_markup" in desc
        assert "baseline" in desc
        assert "accept_all" in desc

    def test_args_schema_required_fields(self) -> None:
        # Only file_path is required; everything else has a default.
        schema = AdeuSanitizeDocxInput.model_json_schema()
        assert schema["required"] == ["reasoning", "file_path"]

    def test_args_schema_rejects_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            AdeuSanitizeDocxInput.model_validate({"reasoning": "test", "file_path": "/a.docx", "preset": "loose"})

    def test_response_format_is_content_and_artifact(self) -> None:
        tool = AdeuSanitizeDocx()
        assert tool.response_format == "content_and_artifact"


class TestAdeuSanitizeDocxValidation:
    def test_rejects_nonexistent_input(self) -> None:
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match="does not exist"):
            tool.invoke({"reasoning": "test", "file_path": "/nonexistent/file.docx"})

    def test_rejects_non_docx_input(self, tmp_path: Path) -> None:
        bad = tmp_path / "doc.txt"
        bad.write_text("nope")
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke({"reasoning": "test", "file_path": str(bad)})

    def test_rejects_overwrite_of_input(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match="must differ from input path"):
            tool.invoke({"reasoning": "test", "file_path": str(src), "output_path": str(src)})

    def test_rejects_nonexistent_baseline(self, tmp_path: Path) -> None:

        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match="baseline document"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "baseline_path": "/nonexistent/baseline.docx",
                }
            )

    def test_rejects_non_docx_baseline(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        bad_baseline = tmp_path / "baseline.txt"
        bad_baseline.write_text("plain text")
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "baseline_path": str(bad_baseline),
                }
            )

    def test_rejects_non_docx_output_path(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK")
        bad_output = tmp_path / "out.pdf"
        tool = AdeuSanitizeDocx()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": str(src),
                    "output_path": str(bad_output),
                }
            )

    def test_allow_low_similarity_baseline_option(self) -> None:
        input_data = AdeuSanitizeDocxInput(
            reasoning="test",
            file_path="/tmp/x.docx",
            allow_low_similarity_baseline=True,
        )
        assert input_data.allow_low_similarity_baseline is True
