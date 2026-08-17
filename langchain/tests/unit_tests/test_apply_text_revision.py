from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu import AdeuApplyTextRevision, AdeuApplyTextRevisionInput


class TestSchema:
    def test_name_and_response_format(self) -> None:
        tool = AdeuApplyTextRevision()
        assert tool.name == "adeu_apply_text_revision"
        assert tool.response_format == "content_and_artifact"

    def test_required_fields(self) -> None:
        schema = AdeuApplyTextRevisionInput.model_json_schema()
        assert set(schema["required"]) == {"reasoning", "file_path", "revised_text"}


class TestValidation:
    def test_rejects_nonexistent_file(self) -> None:
        with pytest.raises(ToolException, match="does not exist"):
            AdeuApplyTextRevision().invoke({"reasoning": "t", "file_path": "/nope/x.docx", "revised_text": "hello"})

    def test_rejects_criticmarkup_in_revised_text(self, tmp_path: Path) -> None:
        src = tmp_path / "d.docx"
        src.write_bytes(b"PK\x03\x04")
        with pytest.raises(ToolException, match="CriticMarkup"):
            AdeuApplyTextRevision().invoke({"reasoning": "t", "file_path": str(src), "revised_text": "a {++b++} c"})

    def test_rejects_empty_revised_text(self, tmp_path: Path) -> None:
        src = tmp_path / "d.docx"
        src.write_bytes(b"PK\x03\x04")
        with pytest.raises(ToolException, match="revised_text cannot be empty"):
            AdeuApplyTextRevision().invoke({"reasoning": "t", "file_path": str(src), "revised_text": "   "})
