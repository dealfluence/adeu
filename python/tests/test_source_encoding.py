# FILE: tests/test_source_encoding.py
"""Repo-wide guard against cp1252-through-UTF-8 mojibake and stray BOMs.

This exists because the same defect landed three separate times during the
content-controls initiative — 73 corrupted sequences across five source files
and both shared spec documents, plus a BOM on the one TypeScript file that
had no business carrying one.

The cause is `Set-Content -Encoding utf8` in Windows PowerShell 5.1, which
writes a BOM and, when it round-trips text that was already UTF-8, re-encodes
it through the host ANSI code page. AGENTS.md documents the trap. Knowing
about it was demonstrably not enough: the failure is silent, the damage is
invisible in a diff viewer that renders both sides the same way, and the
corrupted characters (em-dashes in prose and docstrings) are exactly the ones
nobody greps for.

So this is a mechanical check rather than another line of documentation. It
scans every tracked text file, not just Python: the corruption crossed
languages, and a guard that only covered its own package would have missed
three of the five files it was written in response to.

Deliberately NOT enforced by excluding the affected files. The one legitimate
occurrence — PROGRESS.md quoting a mojibake sequence while explaining this very
bug — opts out per line with an explicit marker, so a new exemption has to be
written down and justified rather than inherited.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A line carrying this marker may contain mojibake on purpose. Documentation
# that explains the defect has to be able to show it.
EXEMPT_MARKER = "mojibake-example"

# BOM is not allowed on tracked text files.
BOM_ALLOWED: set[str] = set()

# A curated FLOOR, not the whole table. The table proper is derived from every
# non-ASCII character the repository actually contains (see
# `_repo_non_ascii`), because a hand-maintained list is guaranteed to develop
# blind spots and this one already did — twice. It shipped without the ballot
# box, and it shipped without `§`, which is the single most common non-ASCII
# character in this repo (every spec cross-reference uses one) and which was
# sitting corrupted in four places at the time.
#
# These stay listed anyway so the important ones are checked even if the
# derivation is ever narrowed.
_SUSPECT_CHARS = (
    "\u2014",  # em dash        — by far the most common casualty
    "\u2013",  # en dash
    "\u201c",  # left double quotation mark
    "\u201d",  # right double quotation mark
    "\u2018",  # left single quotation mark
    "\u2019",  # right single quotation mark
    "\u2026",  # horizontal ellipsis
    "\u2192",  # rightwards arrow
    "\u2610",  # ballot box        — checkbox rendering, core to this initiative
    "\u2612",  # ballot box with x
    "\u00b7",  # middle dot
    "\u00a7",  # section sign      — every spec cross-reference in this repo
)


def _decode_cp1252_lenient(data: bytes) -> str:
    """Decode as cp1252 the way the corrupting tools actually do it.

    Five bytes (0x81, 0x8D, 0x8F, 0x90, 0x9D) are undefined in cp1252 and
    Python's strict codec refuses them. Real-world decoders — the WHATWG
    windows-1252 table, and whatever PowerShell went through here — instead
    pass them through to the matching C1 control character.

    That distinction is not academic. `☐` (U+2610) encodes as E2 98 90, and
    that trailing 0x90 is one of the five. Deriving fingerprints with the
    strict codec silently DROPS the ballot box from the table, so a guard built
    on it would have walked straight past a corrupted checkbox glyph — which is
    how the one in PROGRESS.md line 12 survived a scan that reported the ballot
    box beside it.
    """
    out: list[str] = []
    for byte in data:
        try:
            out.append(bytes([byte]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(chr(byte))
    return "".join(out)


def _repo_non_ascii(texts: Iterable[str]) -> set[str]:
    """Every non-ASCII character the repository actually contains.

    The point of deriving this is that the guard then has no blind spot by
    construction: any character the repo uses is a character the repo can
    corrupt, and both of this table's historical gaps — `☐` and `§` — were
    characters sitting in plain sight while a curated list looked elsewhere.
    """
    found: set[str] = set()
    for text in texts:
        found.update(ch for ch in text if ord(ch) > 127)
    return found


def _mojibake_fingerprints(extra: Sequence[str] = ()) -> dict[str, str]:
    """Map each corrupt sequence back to the character it should have been.

    Computed rather than hardcoded so the table cannot drift from the encodings
    it claims to describe.
    """
    table: dict[str, str] = {}
    for char in (*_SUSPECT_CHARS, *extra):
        broken = _decode_cp1252_lenient(char.encode("utf-8"))
        if broken != char:
            table[broken] = char
    return table


FINGERPRINTS = _mojibake_fingerprints()


def _tracked_text_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        # Explicit: the default decodes with the host ANSI code page, which is
        # the very bug this module guards against (see tests/utils.run_cli).
        encoding="utf-8",
        check=True,
    ).stdout
    return [line for line in listing.splitlines() if line and (REPO_ROOT / line).is_file()]


def _is_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:8000]


def _read_tracked_texts() -> tuple[list[tuple[str, str]], list[str]]:
    """Decode every tracked text file once. Returns (texts, undecodable)."""
    texts: list[tuple[str, str]] = []
    broken: list[str] = []
    for rel in _tracked_text_files():
        raw = (REPO_ROOT / rel).read_bytes()
        if _is_binary(raw):
            continue
        try:
            texts.append((rel, raw.decode("utf-8-sig")))
        except UnicodeDecodeError as exc:
            broken.append(f"{rel}: not valid UTF-8 ({exc})")
    return texts, broken


def test_no_tracked_file_contains_mojibake():
    """No tracked text file holds a cp1252-through-UTF-8 sequence.

    Reports every offender at once with file, line number and the specific
    corrupt sequence. A guard that failed on the first hit would turn a
    repo-wide cleanup into a one-file-per-run grind.
    """
    texts, offenders = _read_tracked_texts()

    # Derived from the corpus of the repo itself, so a character cannot be
    # missed merely because nobody thought to list it.
    fingerprints = _mojibake_fingerprints(sorted(_repo_non_ascii(t for _, t in texts)))
    assert fingerprints, "the fingerprint table computed empty; the guard would be vacuous"

    for rel, text in texts:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if EXEMPT_MARKER in line:
                continue
            for broken, intended in fingerprints.items():
                if broken in line:
                    offenders.append(f"{rel}:{lineno}: {broken!r} should be {intended!r}")

    assert not offenders, (
        "Mojibake found in tracked files — a UTF-8 file was read as cp1252 and "
        "re-encoded, almost certainly by `Set-Content -Encoding utf8` on Windows "
        "PowerShell 5.1.\n\n"
        + "\n".join(offenders)
        + "\n\nRepair the characters and write the file with an explicit UTF-8 "
        "encoding and no BOM. If a line must show a corrupt sequence to document "
        f"it, mark that line with '{EXEMPT_MARKER}'."
    )


def test_no_unexpected_utf8_bom():
    """Source files carry no UTF-8 BOM.

    Same root cause and the same silence: a BOM survives most tooling, then
    surfaces as a stray character in the first token — a shebang that stops
    working, a JSON parse that fails on byte one, an import that resolves to
    nothing.
    """
    offenders = [
        rel
        for rel in _tracked_text_files()
        if rel not in BOM_ALLOWED and (REPO_ROOT / rel).read_bytes()[:3] == b"\xef\xbb\xbf"
    ]

    assert not offenders, (
        "Unexpected UTF-8 BOM in tracked files:\n  "
        + "\n  ".join(offenders)
        + "\n\nWrite these without a BOM. In PowerShell, `Set-Content -Encoding utf8` "
        "adds one; use an explicit UTF-8-without-BOM writer instead."
    )


@pytest.mark.parametrize(
    ("broken", "intended"),
    sorted(FINGERPRINTS.items()),
)
def test_fingerprint_table_round_trips(broken: str, intended: str):
    """Every fingerprint really is what the corruption produces.

    Guards the guard. If this table were wrong the suite above would pass on a
    thoroughly corrupt repository, which is the one failure mode a check like
    this must not have.
    """
    assert _decode_cp1252_lenient(intended.encode("utf-8")) == broken
    assert broken != intended


def test_fingerprint_table_covers_the_c1_fallback_characters():
    """The ballot box is in the table.

    A regression guard for the specific blind spot described in
    `_decode_cp1252_lenient`: derive the table with Python's strict cp1252 and
    `☐` vanishes from it without a word. This asserts the lenient decode is
    still being used, so the table cannot quietly shrink back.
    """
    assert "\u2610" in FINGERPRINTS.values(), (
        "ballot box missing from the fingerprint table — the derivation has "
        "reverted to a strict cp1252 decode, which drops every character whose "
        "UTF-8 bytes include 0x81, 0x8D, 0x8F, 0x90 or 0x9D"
    )
