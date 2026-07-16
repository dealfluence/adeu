r"""
Regression tests for the 2026-07-16 first-time-user QA report
(mailroom: room/qa-report/adeu-fte-2026-07-16.md).

Findings covered:

- F9 (correctness-risk): `adeu diff <docx> <txt>` dies with an unhandled
  UnicodeDecodeError when the text file is not valid UTF-8 — e.g. the cp1252
  file produced by redirecting `adeu extract` output on a legacy-code-page
  Windows console, so the tool crashes on its own output. The same strict
  `open(..., encoding="utf-8")` read exists on the `apply` text-changes path
  and the `markup` text-input path.

  Pinned contract: a non-UTF-8 text input must either be decoded via a
  fallback or rejected with a guided error (exit 1 naming the file and the
  encoding problem) — never an unhandled traceback.

- F10 (cosmetic): status emoji (❌ / ✅) in stderr messages degrade to
  literal `❌` escape text when stderr's encoding cannot represent them.
  Python picks the ANSI code page (e.g. cp1252) for piped/redirected streams
  on Windows and stderr always uses the `backslashreplace` error handler, so
  any agent harness capturing stderr sees the escaped form — exactly how the
  QA report's environment hit it.

  Pinned contract: human-facing error output must never contain literal
  `\uXXXX` escape sequences, whatever encoding the host forces on stderr.

- F9 review extension: `handle_diff` never checks that the modified text file
  exists, so a missing path raises a raw FileNotFoundError traceback instead
  of the guided "File not found" message the original argument gets.

Not covered (judged not-a-defect during review):

- F11 (Windows backslash paths in messages): the CLI echoes native OS paths;
  the surprising `C:\Program Files\Git\...` prefix in the report came from Git
  Bash's POSIX-to-Windows path translation before the path ever reached adeu.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Byte 0x97 is the cp1252 em dash — the exact byte from the QA report's
# traceback ("can't decode byte 0x97") and an invalid UTF-8 start byte.
CP1252_TEXT_BYTES = b"This is a test \x97 with a cp1252 em dash.\n"


def get_fixture_path(name: str) -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "shared" / "fixtures").is_dir():
            return parent / "shared" / "fixtures" / name
    raise FileNotFoundError(f"Could not find fixtures directory for {name}")


def _run_cli(argv) -> int:
    from adeu.cli import main

    with patch.object(sys, "argv", ["adeu", *argv]):
        try:
            main()
        except SystemExit as e:
            return e.code if e.code is not None else 0
    return 0


def _assert_guided_encoding_error(err: str, filename: str):
    """A rejected non-UTF-8 input must produce a guided message, not a traceback."""
    assert "❌" in err
    assert filename in err
    lowered = err.lower()
    assert "utf-8" in lowered or "encoding" in lowered or "decode" in lowered


def test_f9_diff_cp1252_text_file_must_not_crash(tmp_path, capsys):
    """F9: `adeu diff <docx> <cp1252.txt>` must not escape with UnicodeDecodeError."""
    fixture = get_fixture_path("golden.docx")
    mod_txt = tmp_path / "modified.txt"
    mod_txt.write_bytes(CP1252_TEXT_BYTES)

    exit_code = _run_cli(["diff", str(fixture), str(mod_txt)])

    captured = capsys.readouterr()
    assert exit_code in (0, 1)
    if exit_code == 1:
        _assert_guided_encoding_error(captured.err, mod_txt.name)


def test_f9_apply_cp1252_text_changes_must_not_crash(tmp_path, capsys):
    """F9 (same read on the apply path): text-changes file with cp1252 bytes."""
    fixture = get_fixture_path("golden.docx")
    changes_txt = tmp_path / "changes.txt"
    changes_txt.write_bytes(CP1252_TEXT_BYTES)

    exit_code = _run_cli(["apply", str(fixture), str(changes_txt), "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code in (0, 1)
    if exit_code == 1:
        _assert_guided_encoding_error(captured.err, changes_txt.name)


def test_f9_markup_cp1252_text_input_must_not_crash(tmp_path, capsys):
    """F9 (same read on the markup path): Markdown input with cp1252 bytes."""
    input_txt = tmp_path / "input.txt"
    input_txt.write_bytes(CP1252_TEXT_BYTES)
    edits_json = tmp_path / "edits.json"
    edits_json.write_text(
        json.dumps([{"type": "modify", "target_text": "test", "new_text": "exam"}]),
        encoding="utf-8",
    )

    exit_code = _run_cli(["markup", str(input_txt), str(edits_json)])

    captured = capsys.readouterr()
    assert exit_code in (0, 1)
    if exit_code == 1:
        _assert_guided_encoding_error(captured.err, input_txt.name)


def test_f9_diff_missing_text_file_gets_guided_error(tmp_path, capsys):
    """
    F9 review extension: a missing modified-text path must exit 1 with the
    guided "File not found" message (as the original argument already does),
    not a raw FileNotFoundError traceback.
    """
    from adeu.cli import main

    fixture = get_fixture_path("golden.docx")
    missing = tmp_path / "does_not_exist.txt"

    test_args = ["adeu", "diff", str(fixture), str(missing)]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "File not found" in captured.err


def test_f10_stderr_emoji_never_backslash_escaped():
    """
    F10: with stderr forced to a non-UTF-8 encoding (what Python picks by
    default for piped stderr on a cp1252 Windows host), error messages must
    not leak literal `\\u274c` escape text to the user.

    Runs the real CLI in a subprocess because stream encodings are fixed at
    interpreter startup and cannot be simulated through capsys.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "adeu.cli", "extract", "definitely_missing_file_xyz.docx"],
        capture_output=True,
        timeout=120,
        env=env,
    )

    stderr_text = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 1
    assert "File not found" in stderr_text
    assert "\\u274c" not in stderr_text
