import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from adeu.cli import main
from adeu.disk_cache import disk_projection_cache


def get_fixture_path(name: str) -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "shared" / "fixtures").is_dir():
            return parent / "shared" / "fixtures" / name
    raise FileNotFoundError(f"Could not find fixtures directory for {name}")


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Isolate disk projection cache to a temporary directory per test."""
    cache_dir = tmp_path / "adeu_cache"
    monkeypatch.setenv("ADEU_CACHE_DIR", str(cache_dir))
    monkeypatch.delenv("ADEU_NO_CACHE", raising=False)
    monkeypatch.delenv("ADEU_DISABLE_DISK_CACHE", raising=False)
    disk_projection_cache.cache_dir = cache_dir
    disk_projection_cache.clear_stats()
    yield
    disk_projection_cache.clear_stats()


def _run_cli(args: list[str], capsys) -> str:
    test_args = ["adeu"] + args
    with patch.object(sys, "argv", test_args):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0, f"CLI exited with code {e.code}"
    captured = capsys.readouterr()
    return captured.out


def test_second_read_is_byte_identical_and_hits_the_cache(capsys):
    fixture_path = get_fixture_path("golden.docx")

    # First read (cold cache miss)
    out1 = _run_cli(["extract", str(fixture_path)], capsys)
    assert disk_projection_cache.stats["misses"] >= 1
    hits_before = disk_projection_cache.stats["hits"]

    # Second read (warm cache hit)
    out2 = _run_cli(["extract", str(fixture_path)], capsys)
    assert out1 == out2
    assert disk_projection_cache.stats["hits"] > hits_before


def test_outline_mode_is_byte_identical_from_cache(capsys):
    fixture_path = get_fixture_path("golden.docx")

    # First read (outline mode, cold miss)
    out1 = _run_cli(["extract", str(fixture_path), "--mode", "outline"], capsys)
    hits_before = disk_projection_cache.stats["hits"]

    # Second read (outline mode, warm hit)
    out2 = _run_cli(["extract", str(fixture_path), "--mode", "outline"], capsys)
    assert out1 == out2
    assert disk_projection_cache.stats["hits"] > hits_before


def test_mtime_change_invalidates(tmp_path, capsys):
    fixture_path = get_fixture_path("golden.docx")
    doc_copy = tmp_path / "test_doc.docx"
    doc_copy.write_bytes(fixture_path.read_bytes())

    # Read 1
    out1 = _run_cli(["extract", str(doc_copy)], capsys)
    hits_1 = disk_projection_cache.stats["hits"]

    # Read 2 (cache hit)
    out2 = _run_cli(["extract", str(doc_copy)], capsys)
    assert out1 == out2
    assert disk_projection_cache.stats["hits"] == hits_1 + 1

    # Modify mtime
    st = os.stat(doc_copy)
    new_mtime = st.st_mtime + 10.0
    os.utime(doc_copy, (new_mtime, new_mtime))

    # Read 3 (cache invalidated due to mtime change -> miss)
    misses_before = disk_projection_cache.stats["misses"]
    out3 = _run_cli(["extract", str(doc_copy)], capsys)
    assert out1 == out3
    assert disk_projection_cache.stats["misses"] > misses_before


def test_disable_switch_and_unwritable_dir_are_non_fatal(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")

    # 1. Test ADEU_NO_CACHE=1
    monkeypatch.setenv("ADEU_NO_CACHE", "1")
    hits_before = disk_projection_cache.stats["hits"]
    out1 = _run_cli(["extract", str(fixture_path)], capsys)
    assert out1
    assert disk_projection_cache.stats["hits"] == hits_before  # No hit when disabled

    monkeypatch.delenv("ADEU_NO_CACHE")

    # 2. Test unwritable cache dir
    unwritable_dir = tmp_path / "unwritable_cache"
    unwritable_dir.write_text(
        "i_am_a_file_not_a_directory"
    )  # Path exists as a file, so mkdir will fail with NotADirectoryError/OSError
    monkeypatch.setenv("ADEU_CACHE_DIR", str(unwritable_dir))
    disk_projection_cache.cache_dir = unwritable_dir

    # Extract should succeed without crashing
    out2 = _run_cli(["extract", str(fixture_path)], capsys)
    assert out2
    assert out1 == out2


def test_corrupt_cache_entry_is_ignored(tmp_path, capsys):
    fixture_path = get_fixture_path("golden.docx")
    doc_copy = tmp_path / "corrupt_test.docx"
    doc_copy.write_bytes(fixture_path.read_bytes())

    # Read 1 to populate cache
    out1 = _run_cli(["extract", str(doc_copy)], capsys)

    # Locate cache file and corrupt it
    cache_files = list(disk_projection_cache.cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    cache_file = cache_files[0]
    cache_file.write_text("CORRUPTED_NOT_JSON {{{", encoding="utf-8")

    # Read 2: should ignore corrupt entry, perform fresh extract, and succeed
    out2 = _run_cli(["extract", str(doc_copy)], capsys)
    assert out1 == out2

    # Verify cache file was regenerated and contains valid JSON again
    repaired_text = cache_file.read_text(encoding="utf-8")
    parsed = json.loads(repaired_text)
    assert "key" in parsed
