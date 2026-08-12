# FILE: src/adeu/utils/long_hex_number.py
"""
The one place Adeu produces an `ST_LongHexNumber`.

`ST_LongHexNumber` is schema-typed `xsd:hexBinary` of length 4, so every value
from `00000000` to `FFFFFFFF` validates. **Word does not read it that way.**
Word parses it as a SIGNED 32-bit integer, and ECMA-376 states the constraint
in prose rather than in the schema:

> The value ... shall be greater than `0x00000000` and less than `0x80000000`.

Out-of-range values are not rejected. They are silently discarded and
regenerated on load, which breaks everything that referenced them, with no
error, no repair prompt and nothing wrong-looking in the XML:

* `w14:paraId`        -> `w15:paraIdParent` dangles, the reply leaves its thread
                         and renders as a new top-level comment (2026-08-12 B5)
* `w16cid:durableId`  -> the comment anchor collapses to a zero-length point:
                         right author, right text, no highlight (2026-08-11 B3)
* `w14:paraId` == 0   -> Word refuses the package outright: "The file appears
                         to be corrupted" (Word-verified 2026-08-12)

The blast radius is bigger than the one id. Word-verified on Word 16.0: a
package with no bad ids keeps all 32 of its `w14:paraId`s across an open/save;
push exactly ONE of them over `0x7FFFFFFF` and it keeps NONE — Word renumbers
the whole part. So a single bad id invalidates every `{#cell:<paraId>}` anchor
in the document, not just its own.

**This module exists because the previous fix was per-attribute.** B3 was
closed by giving `durableId` a dedicated masked generator and writing down that
`paraId` and `rsid` were "opaque 32-bit tokens with no such constraint" — an
assumption, recorded as fact in two engines' docstrings, in AI_CONTEXT.md and
in two tests that went red when the bug was finally fixed. It was wrong, and it
is why B5 shipped. There is no attribute for which the high half is safe:
Word's own output never uses it (127 paraIds, 271 rsids and every textId in the
pinned WAWD model order are high-bit clear, with zero exceptions).

Do not "reclaim" the high half. 2^31 values minted per comment, in documents
with tens to hundreds of comments, is no collision pressure at all. See
BUG_paraId_signed_int32_thread_collapse.md.

Mirrored byte-for-byte by `node/packages/core/src/docx/long-hex-number.ts`.
"""

import random

#: Smallest value Word accepts. `0x00000000` is forbidden — and unlike the high
#: half it is not silently repaired, it makes Word reject the package.
ST_LONG_HEX_NUMBER_MIN = 0x00000001

#: Largest value Word accepts: `0x80000000` and above are negative int32.
ST_LONG_HEX_NUMBER_MAX = 0x7FFFFFFF


def to_long_hex_number(value: int) -> str:
    """Fold any integer into the legal range and render it as Word writes it.

    For DERIVED ids (a hash of something stable) where the value cannot simply
    be redrawn. Clearing the high bit is what Word effectively does anyway; the
    `or MIN` guards the one input that would otherwise map to the forbidden
    zero.
    """
    return f"{(value & ST_LONG_HEX_NUMBER_MAX) or ST_LONG_HEX_NUMBER_MIN:08X}"


def generate_long_hex_number() -> str:
    """A fresh `ST_LongHexNumber`: `w14:paraId`, `w16cid:durableId`, `w:rsid*`.

    Every ST_LongHexNumber Adeu mints comes from here. Adding a second
    generator is how this bug happened twice; add call sites, not generators.
    """
    return f"{random.randint(ST_LONG_HEX_NUMBER_MIN, ST_LONG_HEX_NUMBER_MAX):08X}"


def is_word_readable_long_hex_number(value: str) -> bool:
    """True when Word will keep `value` rather than discard and regenerate it."""
    if not value or len(value) > 8:
        return False
    try:
        number = int(value, 16)
    except ValueError:
        return False
    return ST_LONG_HEX_NUMBER_MIN <= number <= ST_LONG_HEX_NUMBER_MAX
