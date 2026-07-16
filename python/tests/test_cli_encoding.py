r"""
Terminal-encoding strategy tests (see adeu/utils/console.py).

Three pillars, each pinned here:

1. Output bytes are always UTF-8, independent of the host code page —
   document payloads must neither crash (`UnicodeEncodeError` on a cp1252
   pipe) nor vary per host (a redirected extract must round-trip through
   `adeu diff` on any machine).
2. Status glyphs adapt to the terminal: emoji where the original stream
   encoding can display them, stable ASCII tokens ([x]/[ok]/[!]) on legacy
   code pages or when ADEU_ASCII=1 is set.
3. Text inputs decode through a tolerant, deterministic ladder:
   BOM-marked UTF-8/16/32 silently, plain UTF-8 silently, cp1252 with a
   loud warning, and guided errors for binary-looking or undecodable
   content. Newlines are normalized like text-mode open().
"""

import codecs
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from adeu.cli import _read_text_file
from adeu.utils.console import demote_glyphs


def get_fixture_path(name: str) -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "shared" / "fixtures").is_dir():
            return parent / "shared" / "fixtures" / name
    raise FileNotFoundError(f"Could not find fixtures directory for {name}")


def _run_cli_subprocess(args, **env_overrides):
    """Run the real CLI with a controlled host encoding (set at interpreter
    startup, so this cannot be simulated in-process)."""
    env = {**os.environ, "PYTHONUTF8": "0"}
    env.pop("ADEU_ASCII", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "adeu.cli", *args],
        capture_output=True,
        timeout=120,
        env=env,
    )


@pytest.fixture
def arrow_docx(tmp_path):
    """A document with characters outside every legacy Windows code page."""
    from docx import Document

    path = tmp_path / "arrow.docx"
    doc = Document()
    doc.add_paragraph("The parties agree: → delivery within 30 days (λ-clause).")
    doc.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Pillar 1: output bytes are always UTF-8, whatever the host code page
# ---------------------------------------------------------------------------


def test_extract_survives_cp1252_host_with_non_cp1252_chars(arrow_docx):
    proc = _run_cli_subprocess(["extract", str(arrow_docx)], PYTHONIOENCODING="cp1252")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    text = proc.stdout.decode("utf-8")
    assert "→" in text
    assert "λ" in text


def test_stdout_bytes_identical_across_host_encodings(arrow_docx):
    outputs = {}
    for host_encoding in ("utf-8", "cp1252", "cp932", "ascii"):
        proc = _run_cli_subprocess(["extract", str(arrow_docx)], PYTHONIOENCODING=host_encoding)
        assert proc.returncode == 0, (host_encoding, proc.stderr.decode("utf-8", errors="replace"))
        outputs[host_encoding] = proc.stdout
    assert len(set(outputs.values())) == 1, "stdout bytes must not depend on the host code page"


def test_extract_json_survives_ascii_host(arrow_docx):
    proc = _run_cli_subprocess(["extract", str(arrow_docx), "--json"], PYTHONIOENCODING="ascii")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert "→" in payload["markdown"]


# ---------------------------------------------------------------------------
# Pillar 2: glyphs adapt to what the terminal can display
# ---------------------------------------------------------------------------


def test_stderr_glyphs_demoted_to_ascii_on_cp1252_host():
    proc = _run_cli_subprocess(["extract", "missing_file_xyz.docx"], PYTHONIOENCODING="cp1252")
    stderr_text = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 1
    assert "[x] File not found" in stderr_text
    assert "❌" not in stderr_text
    assert "\\u274c" not in stderr_text


def test_stderr_glyphs_stay_emoji_on_utf8_host():
    proc = _run_cli_subprocess(["extract", "missing_file_xyz.docx"], PYTHONIOENCODING="utf-8")
    stderr_text = proc.stderr.decode("utf-8")
    assert proc.returncode == 1
    assert "❌ File not found" in stderr_text


def test_adeu_ascii_env_forces_ascii_glyphs_even_on_utf8_host():
    proc = _run_cli_subprocess(
        ["extract", "missing_file_xyz.docx"],
        PYTHONIOENCODING="utf-8",
        ADEU_ASCII="1",
    )
    stderr_text = proc.stderr.decode("utf-8")
    assert proc.returncode == 1
    assert "[x] File not found" in stderr_text
    assert "❌" not in stderr_text


def test_demote_glyphs_handles_variation_selector():
    # "⚠️" is U+26A0 + U+FE0F; the pair must collapse to one token, and the
    # bare sign must map too, with no stray variation selector left behind.
    assert demote_glyphs("⚠️ careful") == "[!] careful"
    assert demote_glyphs("⚠ careful") == "[!] careful"
    assert demote_glyphs("✅ Sanitized → out.docx") == "[ok] Sanitized -> out.docx"
    assert demote_glyphs("plain ascii stays") == "plain ascii stays"


# ---------------------------------------------------------------------------
# Pillar 3: tolerant, deterministic decode ladder for text inputs
# ---------------------------------------------------------------------------


def test_read_text_plain_utf8(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_bytes("Héllo — world".encode("utf-8"))
    assert _read_text_file(f) == "Héllo — world"


def test_read_text_utf8_bom_stripped(tmp_path, capsys):
    f = tmp_path / "bom.txt"
    f.write_bytes(codecs.BOM_UTF8 + "Héllo".encode("utf-8"))
    text = _read_text_file(f)
    assert text == "Héllo"
    assert "﻿" not in text
    assert capsys.readouterr().err == ""  # BOMs are trusted silently


def test_read_text_utf16_le_with_bom(tmp_path):
    f = tmp_path / "utf16le.txt"
    f.write_bytes("Héllo — utf16".encode("utf-16"))  # includes LE BOM
    assert _read_text_file(f) == "Héllo — utf16"


def test_read_text_utf16_be_with_bom(tmp_path):
    f = tmp_path / "utf16be.txt"
    f.write_bytes(codecs.BOM_UTF16_BE + "Héllo".encode("utf-16-be"))
    assert _read_text_file(f) == "Héllo"


def test_read_text_cp1252_fallback_warns(tmp_path, capsys):
    f = tmp_path / "legacy.txt"
    f.write_bytes(b"em \x97 dash")
    text = _read_text_file(f)
    assert text == "em — dash"
    err = capsys.readouterr().err
    assert "Windows-1252" in err
    assert "0x97" in err
    assert "legacy.txt" in err


def test_read_text_bomless_utf16_gets_guided_error(tmp_path, capsys):
    # BOM-less UTF-16 of ASCII text is byte-valid UTF-8 (interleaved NULs),
    # so the NUL guard must fire before any decode succeeds silently.
    f = tmp_path / "utf16-nobom.txt"
    f.write_bytes("hello".encode("utf-16-le"))
    with pytest.raises(SystemExit) as exc_info:
        _read_text_file(f)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "NUL" in err
    assert "UTF-16" in err


def test_read_text_undecodable_gets_guided_error(tmp_path, capsys):
    f = tmp_path / "junk.txt"
    f.write_bytes(b"\x81\x8d\x8f\x90\x9d")  # invalid UTF-8, undefined in cp1252
    with pytest.raises(SystemExit) as exc_info:
        _read_text_file(f)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Re-save the file as UTF-8" in err


def test_read_text_corrupt_bom_gets_guided_error(tmp_path, capsys):
    f = tmp_path / "badbom.txt"
    f.write_bytes(codecs.BOM_UTF8 + b"\xff\xfe\xfd")  # claims UTF-8, isn't
    with pytest.raises(SystemExit) as exc_info:
        _read_text_file(f)
    assert exc_info.value.code == 1
    assert "byte-order mark" in capsys.readouterr().err


def test_read_text_missing_file_gets_sandbox_warning(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        _read_text_file(tmp_path / "nope.txt")
    assert exc_info.value.code == 1
    assert "File not found" in capsys.readouterr().err


def test_read_text_normalizes_crlf_like_text_mode_open(tmp_path):
    # The old open(..., "r") used universal newlines; byte-level reading must
    # keep that behavior or CRLF files would produce \r-polluted diffs.
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"line one\r\nline two\rline three")
    assert _read_text_file(f) == "line one\nline two\nline three"


def test_diff_cp1252_end_to_end_warns_and_diffs(tmp_path, capsys):
    from adeu.cli import main

    fixture = get_fixture_path("golden.docx")
    mod = tmp_path / "modified.txt"
    mod.write_bytes(b"This is a test \x97 with a cp1252 em dash.\n")

    with patch.object(sys, "argv", ["adeu", "diff", str(fixture), str(mod)]):
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, None)

    err = capsys.readouterr().err
    assert "Windows-1252" in err
    assert "Found 1 changes" in err
