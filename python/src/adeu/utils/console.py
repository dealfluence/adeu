import os
import sys

GLYPH_FALLBACKS: tuple = (
    ("⚠️", "[!]"),
    ("⚠", "[!]"),
    ("❌", "[x]"),
    ("✅", "[ok]"),
    ("✓", "+"),
    ("✗", "x"),
    ("🤖", "*"),
    ("📄", "*"),
    ("📍", "*"),
    ("📦", "*"),
    ("🔍", "*"),
    ("🔧", "*"),
    ("→", "->"),
    ("—", "-"),
    ("…", "..."),
    ("‘", "'"),
    ("’", "'"),
    ("“", '"'),
    ("”", '"'),
)

_EMOJI_PROBE = "❌✅⚠️"


def _terminal_can_display_glyphs(stream) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        _EMOJI_PROBE.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def demote_glyphs(text: str) -> str:
    for glyph, ascii_form in GLYPH_FALLBACKS:
        if glyph in text:
            text = text.replace(glyph, ascii_form)
    return text


class _GlyphDemotingStderr:
    _adeu_glyph_proxy = True

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        return self._stream.write(demote_glyphs(s))

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _DynamicStderr:
    def write(self, s):
        return sys.stderr.write(s)

    def flush(self):
        stream = sys.stderr
        if stream is not None:
            stream.flush()


dynamic_stderr = _DynamicStderr()


def configure_cli_streams() -> None:
    ascii_flag = os.environ.get("ADEU_ASCII", "").strip()
    force_ascii = ascii_flag not in ("", "0")
    glyphs_ok = not force_ascii and _terminal_can_display_glyphs(sys.stderr)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    if not glyphs_ok and not getattr(sys.stderr, "_adeu_glyph_proxy", False):
        sys.stderr = _GlyphDemotingStderr(sys.stderr)
