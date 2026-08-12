import asyncio
import re
import zipfile
from io import BytesIO
from typing import Callable, List, Tuple
from unittest.mock import AsyncMock

from fastmcp.tools import ToolResult

from adeu.utils.long_hex_number import is_word_readable_long_hex_number


def run_async(coro):
    """Simple wrapper to run a coroutine in a new event loop."""
    return asyncio.run(coro)


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
