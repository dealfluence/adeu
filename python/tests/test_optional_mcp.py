"""Tests for optional MCP (`adeu[mcp]`) packaging and framework-free seams."""

from __future__ import annotations

import subprocess
import sys

from adeu.diff import generate_edits_from_text


def _run_python_without_mcp(code: str) -> subprocess.CompletedProcess[str]:
    """Execute `code` in a subprocess where `fastmcp`, `mcp`, and `jinja2` fail to import."""
    blocker = (
        "import builtins\n"
        "_real_import = builtins.__import__\n"
        "_BLOCKED = {'fastmcp', 'mcp', 'jinja2'}\n"
        "def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "    root = name.split('.', 1)[0]\n"
        "    if level == 0 and root in _BLOCKED:\n"
        "        raise ModuleNotFoundError(f\"No module named '{root}'\", name=root)\n"
        "    return _real_import(name, globals, locals, fromlist, level)\n"
        "builtins.__import__ = _guarded_import\n"
    )
    return subprocess.run(
        [sys.executable, "-c", blocker + "\n" + code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


class TestWordPatchDiffSeam:
    def test_format_word_patch_diff_in_core_diff_module(self):
        from adeu.diff import format_word_patch_diff

        orig = "1. Governing Law. Delaware law governs."
        mod = "1. Governing Law. New York law governs."
        edits = generate_edits_from_text(orig, mod, atomic_criticmarkup=True)

        patch_text = format_word_patch_diff("/tmp/v1.docx", "/tmp/v2.docx", orig, edits)
        assert "--- v1.docx" in patch_text
        assert "+++ v2.docx" in patch_text
        assert "@@ Word Patch @@" in patch_text
        assert "- Delaware" in patch_text
        assert "+ New York" in patch_text

    def test_document_diff_helper_importable_without_fastmcp_for_legacy_langchain(self):
        """Already-published `langchain-adeu<=3.2.2` imports `_create_diff_output`
        from `adeu.mcp_components.tools.document` while declaring `adeu>=2.4.0`."""
        proc = _run_python_without_mcp(
            "from adeu.diff import generate_edits_from_text\n"
            "from adeu.mcp_components.tools.document import _create_diff_output\n"
            "orig = 'Alpha clause.'\n"
            "mod = 'Beta clause.'\n"
            "edits = generate_edits_from_text(orig, mod, atomic_criticmarkup=True)\n"
            "out = _create_diff_output('a.docx', 'b.docx', orig, edits)\n"
            "assert '@@ Word Patch @@' in out\n"
        )
        assert proc.returncode == 0, proc.stderr


class TestLiveWordAndCliSeamsWithoutMcp:
    def test_live_word_core_importable_without_fastmcp(self):
        proc = _run_python_without_mcp(
            "from adeu.mcp_components.tools.live_word import (\n"
            "    _build_mock_docx_stream,\n"
            "    _process_active_word_batch_core,\n"
            "    _read_active_word_document_core,\n"
            "    is_document_open_in_word,\n"
            ")\n"
            "assert callable(_build_mock_docx_stream)\n"
            "assert callable(_read_active_word_document_core)\n"
            "assert callable(_process_active_word_batch_core)\n"
            "assert callable(is_document_open_in_word)\n"
        )
        assert proc.returncode == 0, proc.stderr

    def test_adeu_init_includes_mcp_extra_and_fallback(self, tmp_path, monkeypatch):
        import json
        from unittest.mock import MagicMock

        import adeu
        import adeu.cli as cli_mod

        cfg = tmp_path / "Claude" / "claude_desktop_config.json"
        monkeypatch.setattr(cli_mod, "_get_claude_config_path", lambda: cfg)
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/local/bin/uvx")

        args = MagicMock()
        args.local = False
        args.scope = "all"

        cli_mod.handle_init(args)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert f"adeu[mcp]=={adeu.__version__}" in data["mcpServers"]["adeu"]["args"]

        # Unversioned fallback still includes the [mcp] extra
        monkeypatch.setattr(cli_mod, "get_build_info", lambda: ("unknown", "none", False))
        cli_mod.handle_init(args)
        data_fallback = json.loads(cfg.read_text(encoding="utf-8"))
        assert "adeu[mcp]" in data_fallback["mcpServers"]["adeu"]["args"]


class TestServerGracefulDegradationWithoutMcp:
    def test_server_help_and_version_succeed_without_fastmcp(self):
        for flag in ("--version", "--help"):
            proc = _run_python_without_mcp(
                f"import sys\nsys.argv = ['adeu-server', {flag!r}]\nfrom adeu.server import main\nmain()\n"
            )
            assert proc.returncode == 0, f"{flag} failed without fastmcp: {proc.stderr}"
            assert ("adeu-server" in proc.stdout) or ("usage" in proc.stdout.lower())

    def test_server_exits_cleanly_with_install_guidance_without_fastmcp(self):
        proc = _run_python_without_mcp("import sys\nsys.argv = ['adeu-server']\nfrom adeu.server import main\nmain()\n")
        assert proc.returncode == 1
        assert "adeu[mcp]" in proc.stderr
        assert "Traceback" not in proc.stderr

    def test_importing_mcp_symbol_from_server_raises_clean_importerror(self):
        proc = _run_python_without_mcp(
            "try:\n"
            "    from adeu.server import mcp\n"
            "except ImportError as exc:\n"
            "    assert 'adeu[mcp]' in str(exc), str(exc)\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n"
        )
        assert proc.returncode == 0, proc.stderr


class TestPyprojectOptionalMcpMetadata:
    def test_fastmcp_and_jinja2_live_in_mcp_extra_not_core_dependencies(self):
        import tomllib
        from pathlib import Path

        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        core_deps = data["project"]["dependencies"]
        for dep in core_deps:
            lower = dep.lower()
            assert not lower.startswith("fastmcp"), f"fastmcp still in core dependencies: {dep}"
            assert not lower.startswith("jinja2"), f"jinja2 still in core dependencies: {dep}"

        optional = data["project"].get("optional-dependencies", {})
        assert "mcp" in optional, "missing [project.optional-dependencies].mcp"
        mcp_deps = optional["mcp"]
        assert any(d.lower().startswith("fastmcp") for d in mcp_deps)
        assert any(d.lower().startswith("jinja2") for d in mcp_deps)

        # Dev group must mirror every mcp extra spec so plain `uv sync` keeps
        # the full test suite operational.
        dev_deps = set(data["dependency-groups"]["dev"])
        for dep in mcp_deps:
            assert dep in dev_deps, f"dev dependency group missing mcp extra pin {dep!r}"
