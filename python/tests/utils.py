import asyncio
import json
import os
import re
import subprocess
import sys
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastmcp.tools import ToolResult

from adeu.utils.long_hex_number import is_word_readable_long_hex_number


def run_async(coro):
    """Simple wrapper to run a coroutine in a new event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Running the real CLI (see BUG_cli_test_encoding_and_n8n_lint_toolchain.md)
# ---------------------------------------------------------------------------

#: The CLI always writes UTF-8 bytes (adeu/utils/console.py), so every test
#: that decodes its output must say so. Never rely on the default.
CLI_OUTPUT_ENCODING = "utf-8"


def run_cli(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """Run `python -m adeu.cli <args>` and decode its output as UTF-8.

    ALWAYS decode explicitly. `capture_output=True, text=True` alone decodes
    with `locale.getpreferredencoding()` — cp1252 on a Windows host — while the
    CLI emits UTF-8 whenever the consumer declares it (`PYTHONIOENCODING=utf-8`),
    including the `❌` that prefixes every error message. U+274C is `E2 9D 8C`,
    and `0x9D` is undefined in cp1252, so the mismatch kills subprocess's reader
    THREAD; `communicate()` does not propagate that, it just leaves `.stdout` /
    `.stderr` as `None`. The test then fails somewhere else entirely, with an
    `AttributeError: 'NoneType' object has no attribute 'lower'` that reads like
    a CLI which printed nothing.

    This helper is the choke point: route every CLI subprocess through it so no
    call site can re-introduce the defect by writing the obvious thing.
    Tests that want the raw bytes (to pin what the CLI *emitted*, rather than
    consume what it means) should call `subprocess.run` without `text=True` and
    decode explicitly — see tests/test_cli_encoding.py.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", CLI_OUTPUT_ENCODING)
    return subprocess.run([sys.executable, "-m", "adeu.cli", *[str(a) for a in args]], **kwargs)


def get_mock_ctx():
    """Returns a mock FastMCP Context."""
    return AsyncMock()


def extract_content(res):
    """Extracts markdown from a ToolResult or string."""
    if isinstance(res, ToolResult) and res.structured_content is not None:
        return res.structured_content["markdown"]
    return str(res)


def approx_tokens(s: str) -> int:
    return len(s) // 4


# ---------------------------------------------------------------------------
# ST_LongHexNumber auditing (BUG_paraId_signed_int32_thread_collapse.md)
# ---------------------------------------------------------------------------

#: Every attribute in the WordprocessingML schemas typed `ST_LongHexNumber`
#: that Adeu can end up writing. Word parses all of them as SIGNED 32-bit
#: integers, so a value outside (0x00000000, 0x80000000) is discarded and
#: regenerated on load — taking every reference to it with it, and renumbering
#: the rest of the part for good measure.
#:
#: Kept identical to `LONG_HEX_NUMBER_ATTRIBUTES` in
#: node/packages/core/src/test-utils.ts. `w14:docId` is the same type but is
#: only ever spelled as an element (`<w14:docId w14:val="…"/>`), never as an
#: attribute, and Adeu never writes it.
LONG_HEX_NUMBER_ATTRIBUTES = (
    "w14:paraId",
    "w14:textId",
    "w15:paraId",
    "w15:paraIdParent",
    "w16cid:paraId",
    "w16cid:durableId",
    "w16cex:durableId",
    "w:rsidR",
    "w:rsidRPr",
    "w:rsidRDefault",
    "w:rsidP",
    "w:rsidDel",
    "w:rsidSect",
    "w:rsidTr",
    "w:rsidRoot",
)

_LONG_HEX_ATTR_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in LONG_HEX_NUMBER_ATTRIBUTES) + r')="([0-9A-Fa-f]{1,8})"'
)
# <w:rsid w:val="00FC693F"/> and <w:rsidRoot w:val="…"/> inside <w:rsids>.
_RSIDS_ELEMENT_RE = re.compile(r'<w:(rsid|rsidRoot)\b[^>]*\bw:val="([0-9A-Fa-f]{1,8})"')


def find_out_of_range_long_hex_numbers(package: bytes) -> List[Tuple[str, str, str]]:
    """Every `ST_LongHexNumber` in a saved DOCX that Word will refuse to keep.

    Returns `(part name, attribute, value)` for each offending value. This is
    the general guard for the whole bug class: `w16cid:durableId` (2026-08-11
    B3), `w14:paraId` (2026-08-12 B5) and anything minted next are all caught
    by it, because it does not care which attribute is "the special one".

    The range predicate is the one the ENGINE mints against
    (`adeu.utils.long_hex_number`), so the guard cannot drift away from the
    generator. That the predicate itself matches ECMA-376 is pinned separately
    and from literals, in tests/test_repro_para_id_signed_int32.py.
    """
    offenders: List[Tuple[str, str, str]] = []
    with zipfile.ZipFile(BytesIO(package)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            text = archive.read(name).decode("utf-8", "ignore")
            found = [(attr, value) for attr, value in _LONG_HEX_ATTR_RE.findall(text)]
            found += [(f"w:{tag}/@w:val", value) for tag, value in _RSIDS_ELEMENT_RE.findall(text)]
            for attr, value in found:
                if not is_word_readable_long_hex_number(value):
                    offenders.append((name, attr, value))
    return offenders


def assert_word_readable_ids(package: bytes, context: str = "") -> None:
    """Fail with the offending ids named, not just a count."""
    offenders = find_out_of_range_long_hex_numbers(package)
    assert not offenders, (
        f"{context}the saved package carries {len(offenders)} ST_LongHexNumber value(s) outside "
        f"(0x00000000, 0x80000000): {offenders[:8]}. Word parses these as signed 32-bit "
        "integers, discards them on load and regenerates the whole part's ids — collapsing "
        "comment threads and invalidating every {#cell:paraId} anchor. See "
        "BUG_paraId_signed_int32_thread_collapse.md."
    )


# ---------------------------------------------------------------------------
# The two paraId rules that are not about range ([MS-DOCX] 2.6.2.4 / 2.6.2.6)
# ---------------------------------------------------------------------------

#: `<w:p …>` / `<w15:commentEx …>` — any start tag, captured whole so the
#: attributes on ONE element can be examined together.
_ELEMENT_RE = re.compile(r"<[A-Za-z][-\w:.]*\b[^>]*/?>")


def find_duplicate_para_ids(package: bytes) -> List[Tuple[str, str, str]]:
    """Every `w14:paraId` used more than once within a single part.

    [MS-DOCX] 2.6.2.4: the value "specifies an identifier for a paragraph that
    is unique within the document part". Stated in prose, not in the schema, so
    a duplicate validates exactly like the out-of-range values do.

    Scoped PER PART because that is the scope the specification gives: the same
    paraId in document.xml and in comments.xml is legal and common.
    """
    duplicates: List[Tuple[str, str, str]] = []
    with zipfile.ZipFile(BytesIO(package)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            text = archive.read(name).decode("utf-8", "ignore")
            seen: dict = {}
            for value in re.findall(r'w14:paraId="([0-9A-Fa-f]{1,8})"', text):
                seen[value.upper()] = seen.get(value.upper(), 0) + 1
            duplicates += [(name, "w14:paraId", value) for value, count in seen.items() if count > 1]
    return duplicates


def find_text_ids_without_para_id(package: bytes) -> List[Tuple[str, str]]:
    """Every element carrying `w14:textId` but no `w14:paraId`.

    [MS-DOCX] 2.6.2.6: "Any element having this attribute MUST also have the
    paraId attribute." textId is a version stamp for the paragraph's TEXT and
    is meaningless without the identity it versions, so Word treats the pair as
    one unit — which makes it a constraint on any pass that rewrites ids.
    """
    orphans: List[Tuple[str, str]] = []
    with zipfile.ZipFile(BytesIO(package)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            text = archive.read(name).decode("utf-8", "ignore")
            for tag in _ELEMENT_RE.findall(text):
                if "w14:textId=" in tag and "w14:paraId=" not in tag:
                    orphans.append((name, tag))
    return orphans


def edge_of_range_randint(high: bool) -> Callable[[int, int], int]:
    """A deterministic stand-in for `random.randint` that always draws from one
    END of whatever range the caller asks for, stepping inward one per call so
    successive ids stay distinct.

    This is what makes the id-range tests deterministic instead of ~50/50: an
    unmasked generator asked for `(0, 0xFFFFFFFF)` yields `FFFFFFFF, FFFFFFFE…`
    (every one invalid), while a generator masked to `(1, 0x7FFFFFFF)` yields
    `7FFFFFFF, 7FFFFFFE…` (every one valid). `high=False` probes the other end,
    where an unmasked generator yields the equally-forbidden `00000000`.

    It is a legitimate RNG: every value it returns is one `random.randint`
    could have returned for that call. The test therefore pins the GENERATOR'S
    RANGE, which is the actual defect, rather than sampling its luck.
    """
    counter = {"n": 0}

    def randint(a: int, b: int) -> int:
        step = counter["n"]
        counter["n"] += 1
        value = b - step if high else a + step
        return min(max(value, a), b)

    return randint


# ---------------------------------------------------------------------------
# Real-document corpus
# ---------------------------------------------------------------------------

#: Manifest of the fetch-on-demand corpus. The documents themselves are real
#: government files that are deliberately NOT committed, so every corpus test
#: has to tolerate their absence — CI runs green without ever downloading one.
CORPUS_MANIFEST = Path(__file__).resolve().parents[2] / "shared" / "corpus" / "manifest.json"


def corpus_dir() -> Path:
    """Where corpus documents live. `ADEU_CORPUS_DIR` relocates it.

    Mirrors `scripts/fetch_corpus.py.corpus_dir()`; the fetcher and the tests
    must agree or a fetch lands somewhere the suite cannot see.
    """
    override = os.environ.get("ADEU_CORPUS_DIR")
    return Path(override) if override else CORPUS_MANIFEST.parent


@lru_cache(maxsize=1)
def _corpus_manifest() -> dict:
    with open(CORPUS_MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)["documents"]


def corpus_path(key: str) -> Path:
    """Path to corpus document `key`, or `pytest.skip` when it is not on disk.

    The two failure modes are deliberately NOT the same, and conflating them is
    the trap this helper exists to avoid. An **absent document** is normal: the
    corpus is fetch-on-demand, so the test skips with the exact command that
    would fix it. An **unknown key** is a typo, and a typo that skipped would
    make the test vacuously green forever — the silent no-op this repo treats as
    the most expensive bug — so it raises instead, listing the valid keys.

    Never downloads. Fetching inside a test would make the suite depend on
    government web servers being up and on the network being there at all.
    """
    documents = _corpus_manifest()
    if key not in documents:
        raise KeyError(f"unknown corpus key {key!r}; manifest defines: {', '.join(sorted(documents))}")
    path = corpus_dir() / documents[key]["file"]
    if not path.exists():
        pytest.skip(f"corpus document {key!r} absent at {path} — run `python scripts/fetch_corpus.py --only {key}`")
    return path
