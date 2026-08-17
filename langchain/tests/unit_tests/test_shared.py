"""Unit tests for the `_shared` helpers: path validation + error wrapping."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import ToolException

from langchain_adeu._shared import (
    validate_docx_path,
    validate_path,
    wrap_tool_errors,
)


class TestValidatePath:
    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ToolException, match="cannot be empty"):
            validate_path("", must_exist=False)

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ToolException, match="cannot be empty"):
            validate_path("   ", must_exist=False)

    def test_resolves_relative_path(self, tmp_path: Path) -> None:
        # must_exist=False so we can resolve a non-existent path.
        result = validate_path("nope.txt", must_exist=False)
        assert result.is_absolute()

    def test_raises_when_must_exist_and_missing(self) -> None:
        with pytest.raises(ToolException, match="does not exist"):
            validate_path("/nonexistent/path/that/cannot/be.real", must_exist=True)

    def test_returns_resolved_path_when_exists(self, tmp_path: Path) -> None:
        f = tmp_path / "real.txt"
        f.write_text("hi")
        result = validate_path(str(f))
        assert result == f.resolve()

    def test_custom_label_in_error_message(self) -> None:
        with pytest.raises(ToolException, match="baseline document"):
            validate_path("", must_exist=False, label="baseline document")


class TestValidateDocxPath:
    def test_rejects_non_docx_suffix(self, tmp_path: Path) -> None:
        f = tmp_path / "real.pdf"
        f.write_text("not actually a pdf")
        with pytest.raises(ToolException, match=r"must be a \.docx file"):
            validate_docx_path(str(f))

    def test_rejects_doc_extension(self, tmp_path: Path) -> None:
        # Old binary .doc format is unsupported; ensure we say so clearly.
        f = tmp_path / "legacy.doc"
        f.write_text("ms-word binary")
        with pytest.raises(ToolException, match="not supported"):
            validate_docx_path(str(f))

    def test_accepts_uppercase_docx(self, tmp_path: Path) -> None:
        f = tmp_path / "REAL.DOCX"
        f.write_bytes(b"\x50\x4b")  # PK header just for realism; content is irrelevant.
        result = validate_docx_path(str(f))
        assert result.suffix.lower() == ".docx"

    def test_rejects_directory_at_docx_path(self, tmp_path: Path) -> None:
        d = tmp_path / "looks_like_a_doc.docx"
        d.mkdir()
        with pytest.raises(ToolException, match="not a regular file"):
            validate_docx_path(str(d))

    def test_must_exist_false_skips_existence_check(self, tmp_path: Path) -> None:
        # When creating a brand new output file, the path won't exist yet.
        target = tmp_path / "future_output.docx"
        result = validate_docx_path(str(target), must_exist=False)
        assert result.name == "future_output.docx"


class TestWrapToolErrors:
    def test_passes_through_normal_return(self) -> None:
        @wrap_tool_errors
        def ok() -> str:
            return "fine"

        assert ok() == "fine"

    def test_passes_through_tool_exception_unchanged(self) -> None:
        @wrap_tool_errors
        def raises_te() -> str:
            raise ToolException("already a tool exception")

        with pytest.raises(ToolException, match="already a tool exception"):
            raises_te()

    def test_converts_filenotfound(self) -> None:
        @wrap_tool_errors
        def raises_fnf() -> str:
            raise FileNotFoundError("missing thing")

        with pytest.raises(ToolException, match="File not found"):
            raises_fnf()

    def test_converts_value_error(self) -> None:
        @wrap_tool_errors
        def raises_ve() -> str:
            raise ValueError("bad value")

        with pytest.raises(ToolException, match="bad value"):
            raises_ve()

    def test_converts_unknown_exception_with_type_prefix(self) -> None:
        class CustomError(Exception):
            pass

        @wrap_tool_errors
        def raises_custom() -> str:
            raise CustomError("something weird")

        with pytest.raises(ToolException, match="CustomError: something weird"):
            raises_custom()

    def test_does_not_swallow_keyboard_interrupt(self) -> None:
        @wrap_tool_errors
        def raises_ki() -> str:
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            raises_ki()


def test_id_discovery_hint_names_the_langchain_tool() -> None:
    from langchain_adeu._shared import LANGCHAIN_ID_DISCOVERY_HINT

    # A LangChain agent cannot run the CLI, so the engine's default hint
    # ("Run `adeu extract ...`", engine.py:5229-5231) is useless to it.
    assert "adeu_read_docx" in LANGCHAIN_ID_DISCOVERY_HINT
    assert "mode='changes'" in LANGCHAIN_ID_DISCOVERY_HINT
    assert "adeu extract" not in LANGCHAIN_ID_DISCOVERY_HINT


class TestAdeuFloor:
    def test_declared_adeu_floor_covers_the_apis_we_call(self) -> None:
        """The engine APIs this package needs, all added after adeu 1.17."""
        import inspect
        import tomllib
        from pathlib import Path

        from adeu.mcp_components._response_builders import (
            build_budget_guard_message,
            build_changes_response,
            build_page_range_response,
        )
        from adeu.pagination import parse_page_arg
        from adeu.redline.engine import RedlineEngine

        assert callable(parse_page_arg)
        assert callable(build_changes_response)
        assert callable(build_page_range_response)
        assert callable(build_budget_guard_message)
        assert "partial" in inspect.signature(RedlineEngine.process_batch).parameters
        assert "id_discovery_hint" in inspect.signature(RedlineEngine.__init__).parameters

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
        adeu_req = next(d for d in deps if d.startswith("adeu"))
        assert adeu_req == "adeu>=2.4.0", f"stale adeu floor: {adeu_req}"
