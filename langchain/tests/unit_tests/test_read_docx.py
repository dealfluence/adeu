# FILE: langchain/tests/unit_tests/test_read_docx.py
"""Unit tests for `AdeuReadDocx`.

These tests cover input validation and error paths without requiring a
real DOCX file. End-to-end happy-path tests live in `integration_tests/`
because they exercise the full Adeu engine and need a fixture file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException
from pydantic import ValidationError

from langchain_adeu import AdeuReadDocx, AdeuReadDocxInput


class TestAdeuReadDocxSchema:
    def test_name_is_snake_case(self) -> None:
        # LangChain docs warn that some model providers reject names with
        # spaces or special characters. Enforce the convention.
        tool = AdeuReadDocx()
        assert tool.name == "adeu_read_docx"
        assert " " not in tool.name

    def test_description_is_non_trivial(self) -> None:
        tool = AdeuReadDocx()
        assert len(tool.description) > 100  # Sanity floor — must actually explain itself.
        assert "CriticMarkup" in tool.description  # The critical concept must be named.

    def test_args_schema_is_pydantic_model(self) -> None:
        tool = AdeuReadDocx()
        assert tool.args_schema is AdeuReadDocxInput

    def test_args_schema_required_fields(self) -> None:
        # file_path is the only required field; the rest have defaults.
        schema = AdeuReadDocxInput.model_json_schema()
        assert schema["required"] == ["reasoning", "file_path"]

    def test_args_schema_rejects_extra_fields(self) -> None:

        with pytest.raises(ValueError):
            AdeuReadDocxInput.model_validate({"reasoning": "test", "file_path": "/tmp/x.docx", "bogus_param": True})

    def test_response_format_is_content_and_artifact(self) -> None:
        # If this assertion breaks, our _run signature also needs to change
        # (it currently returns a 2-tuple, which only `content_and_artifact`
        # consumes correctly).
        tool = AdeuReadDocx()
        assert tool.response_format == "content_and_artifact"


class TestAdeuReadDocxValidation:
    def test_rejects_nonexistent_file(self) -> None:
        tool = AdeuReadDocx()
        with pytest.raises(ToolException, match="does not exist"):
            tool.invoke({"reasoning": "test", "file_path": "/nonexistent/path/file.docx"})

    def test_rejects_non_docx_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "doc.txt"
        bad.write_text("not a docx")
        tool = AdeuReadDocx()
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            tool.invoke({"reasoning": "test", "file_path": str(bad)})

    def test_rejects_empty_file_path(self) -> None:
        tool = AdeuReadDocx()
        with pytest.raises(ToolException, match="cannot be empty"):
            tool.invoke({"reasoning": "test", "file_path": ""})

    def test_page_must_be_positive(self) -> None:
        # ge=1 on the field schema; Pydantic raises ValidationError on invoke.
        tool = AdeuReadDocx()
        with pytest.raises(ValidationError):
            tool.invoke({"reasoning": "test", "file_path": "/tmp/x.docx", "page": 0})

    def test_outline_max_level_bounds(self) -> None:
        tool = AdeuReadDocx()
        with pytest.raises(ValidationError):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": "/tmp/x.docx",
                    "outline_max_level": 0,
                }
            )
        with pytest.raises(ValidationError):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": "/tmp/x.docx",
                    "outline_max_level": 7,
                }
            )

    def test_mode_must_be_valid_literal(self) -> None:
        tool = AdeuReadDocx()
        with pytest.raises(ValidationError):
            tool.invoke(
                {
                    "reasoning": "test",
                    "file_path": "/tmp/x.docx",
                    "mode": "unsupported_mode",
                }
            )

    def test_page_all_is_allowed(self) -> None:
        input_data = AdeuReadDocxInput(
            reasoning="test",
            file_path="/tmp/x.docx",
            page="all",
        )
        assert input_data.page == "all"
