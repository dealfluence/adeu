"""Local diagnostics require explicit inputs; permission templates use this checkout."""

import re
import runpy
import subprocess
import sys
from pathlib import Path

import docx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_tracked_text_avoids_personal_home_paths():
    """Check tracked UTF-8 text, not ignored private data or binary fixtures."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, encoding="utf-8", check=True
    ).stdout.split("\0")
    pattern = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:Users|home)/)([^\\/\s\"'<>]+)[\\/]+")
    # Documented synthetic path-handling fixtures and instructional placeholders.
    examples = {"test", "you", "someone", "o’brien"}
    findings = []
    for name in filter(None, tracked):
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in pattern.finditer(line):
                user = match[1]
                if user.lower() not in examples and not any(char in user for char in "{}$%"):
                    findings.append(f"{name}:{number}")
    assert not findings, "Personal home paths in tracked text (values redacted): " + ", ".join(findings)


@pytest.mark.parametrize("script", ["debug_rels.py", "inspect_test_docx.py"])
def test_document_diagnostics_require_an_explicit_input(script, tmp_path, monkeypatch):
    path = ROOT / "python/scripts" / script
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(path)])

    def unexpected_read(*args, **kwargs):
        raise AssertionError("a missing argument must never trigger document access")

    monkeypatch.setattr(docx, "Document", unexpected_read)
    with pytest.raises(SystemExit, match=f"Usage: {script} DOCUMENT.docx"):
        runpy.run_path(str(path), run_name="__main__")


@pytest.mark.parametrize("script", ["debug_rels.py", "inspect_test_docx.py"])
def test_document_diagnostics_accept_an_explicit_fixture(script, tmp_path, monkeypatch, capsys):
    path = ROOT / "python/scripts" / script
    document = tmp_path / "sample.docx"
    docx.Document().save(document)
    before = document.read_bytes()
    monkeypatch.setattr(sys, "argv", [str(path), str(document)])
    runpy.run_path(str(path), run_name="__main__")
    assert "Error" not in capsys.readouterr().out
    assert document.read_bytes() == before


def test_test_creator_permission_scope_is_checkout_relative():
    # Load definitions only; never run main or read/write any agent settings.
    module = runpy.run_path(str(ROOT / "scripts/enable_test_creator_mode.py"), run_name="permission_definitions")
    permissions = module["TEST_CREATOR_PERMISSIONS"]
    root = ROOT.as_posix()
    allowed_writes = [
        "python/tests/*",
        "tests/*",
        "node/packages/core/src/*.test.ts",
        "node/packages/core/src/test-utils.ts",
        "node/packages/n8n-nodes-adeu/test/*",
        "node/packages/mcp-server/tests/*",
    ]
    denied_writes = [
        "python/src/*",
        *[
            f"node/packages/core/src/{name}.ts"
            for name in (
                "comments",
                "diff",
                "domain",
                "engine",
                "index",
                "ingest",
                "mapper",
                "markup",
                "models",
                "outline",
                "pagination",
            )
        ],
        "node/packages/mcp-server/src/*",
        "node/packages/n8n-nodes-adeu/nodes/*",
    ]
    assert permissions == {
        "allow": [
            f"read_file({root}/*)",
            "command(uv run pytest)",
            "command(npm run test)",
            *[f"write_file({root}/{path})" for path in allowed_writes],
        ],
        "deny": [f"write_file({root}/{path})" for path in denied_writes],
        "ask": [],
    }
