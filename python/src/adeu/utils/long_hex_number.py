"""ST_LongHexNumber generator and validator according to Word signed int32 bounds."""

import random

ST_LONG_HEX_NUMBER_MIN = 0x00000001
ST_LONG_HEX_NUMBER_MAX = 0x7FFFFFFF


def to_long_hex_number(value: int) -> str:
    """Fold integer into the signed int32 Word range and render hex."""
    return f"{(value & ST_LONG_HEX_NUMBER_MAX) or ST_LONG_HEX_NUMBER_MIN:08X}"


def generate_long_hex_number() -> str:
    """A fresh ST_LongHexNumber (w14:paraId, w16cid:durableId, w:rsid*)."""
    return f"{random.randint(ST_LONG_HEX_NUMBER_MIN, ST_LONG_HEX_NUMBER_MAX):08X}"


def is_word_readable_long_hex_number(value: str) -> bool:
    """True when value is within Word readable signed int32 hex range."""
    if not value or len(value) > 8:
        return False
    try:
        number = int(value, 16)
    except ValueError:
        return False
    return ST_LONG_HEX_NUMBER_MIN <= number <= ST_LONG_HEX_NUMBER_MAX
