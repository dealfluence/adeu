from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from adeu.text_revision import TextRevisionVerificationError
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


class TestVerificationFailureArtifact:
    """The artifact must relay the engine's post-gate stats, not a blank."""

    def test_artifact_carries_engine_stats(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "d.docx"
        src.write_bytes(b"PK\x03\x04")
        unverified = tmp_path / "d_redlined.unverified.docx"
        # Shape mirrors what the engine attaches to the exception
        # (python/src/adeu/text_revision.py:259-277).
        engine_stats: dict[str, Any] = {
            "status": "ok",
            "edits_applied": 0,
            "edits_skipped": 2,
            "actions_applied": 0,
            "actions_skipped": 0,
            "occurrences_modified": 0,
            "verified": False,
            "verification_error": "boom",
            "error": "verification_failed",
            "output_path": None,
            "unverified_output_path": str(unverified),
            "edits": [
                {"index": 0, "status": "failed", "error": "Not applied: post-apply verification failed."},
                {"index": 1, "status": "failed", "error": "Not applied: post-apply verification failed."},
            ],
            "engine": "python",
        }

        def fake_core(*args: Any, **kwargs: Any) -> Any:
            raise TextRevisionVerificationError(
                "boom",
                unverified_path=unverified,
                output_path=tmp_path / "d_redlined.docx",
                stats=engine_stats,
            )

        monkeypatch.setattr("langchain_adeu.apply_text_revision.apply_text_revision_core", fake_core)

        msg = AdeuApplyTextRevision().invoke(
            {
                "name": "adeu_apply_text_revision",
                "args": {"reasoning": "t", "file_path": str(src), "revised_text": "hello"},
                "id": "test-stats-relay",
                "type": "tool_call",
            }
        )
        artifact = msg.artifact
        # Engine-reported detail survives instead of being zeroed out.
        assert artifact["edits_skipped"] == 2
        assert [e["status"] for e in artifact["edits"]] == ["failed", "failed"]
        assert artifact["verification_error"] == "boom"
        assert artifact["occurrences_modified"] == 0
        # The tool's own verdict overlays the engine's batch-level status.
        assert artifact["success"] is False
        assert artifact["verified"] is False
        assert artifact["status"] == "verification_failed"
        assert artifact["output_path"] is None
        assert artifact["unverified_output_path"] == str(unverified)
        assert artifact["input_path"] == str(src)

    def test_missing_stats_still_reports_the_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "d.docx"
        src.write_bytes(b"PK\x03\x04")
        unverified = tmp_path / "d_redlined.unverified.docx"

        def fake_core(*args: Any, **kwargs: Any) -> Any:
            raise TextRevisionVerificationError(
                "boom", unverified_path=unverified, output_path=tmp_path / "d_redlined.docx"
            )

        monkeypatch.setattr("langchain_adeu.apply_text_revision.apply_text_revision_core", fake_core)

        msg = AdeuApplyTextRevision().invoke(
            {
                "name": "adeu_apply_text_revision",
                "args": {"reasoning": "t", "file_path": str(src), "revised_text": "hello"},
                "id": "test-stats-absent",
                "type": "tool_call",
            }
        )
        assert msg.artifact["success"] is False
        assert msg.artifact["status"] == "verification_failed"
        assert msg.artifact["error"] == "boom"
        assert msg.artifact["unverified_output_path"] == str(unverified)
