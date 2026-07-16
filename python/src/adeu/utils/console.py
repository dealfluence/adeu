"""Terminal-encoding setup for the adeu CLI.

Console encodings differ wildly across hosts: modern terminals and agent
harnesses speak UTF-8, but Python picks the ANSI code page (cp1252, cp932,
...) for piped/redirected streams on Windows and honours legacy locales
elsewhere. Left alone, that yields three levels of failure:

1. stdout (document payloads, JSON) encodes with errors="strict", so any
   document character outside the host code page crashes `adeu extract`.
2. stderr uses errors="backslashreplace", so status emoji degrade to
   literal escape text like ``\\u274c``.
3. Output bytes differ per host, so a file produced by redirecting adeu's
   own output may not round-trip through `adeu diff`.

Policy (regression-tested in tests/test_repro_fte_2026_07_16.py and
tests/test_cli_encoding.py):

- Output bytes are always UTF-8. Document data must never be adapted to a
  terminal, so both streams are reconfigured at startup, keeping
  errors="backslashreplace" as a never-crash safety net.
- Decorative status glyphs adapt instead: when the terminal's original
  encoding could not have displayed them — or ADEU_ASCII=1 is set — stderr
  is wrapped so ❌/✅/⚠️ degrade to stable ASCII tokens like [x]/[ok]/[!].
"""

import os
import sys

# Glyphs and typographic punctuation that may appear in CLI stderr messages,
# with ASCII stand-ins. Longest sequences first so "⚠️" (warning sign plus
# variation selector) wins over the bare "⚠".
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
    """Whether the stream's native encoding can represent our status glyphs.

    Must be checked against the encoding the stream had *before* we force
    UTF-8 — that is the best available signal for what the console, or the
    consumer on the other side of a pipe, will actually decode and render.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        # No declared encoding (StringIO and friends): assume a modern
        # UTF-8 consumer rather than degrading output on a guess.
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
    """Proxy for sys.stderr that swaps decorative glyphs for ASCII tokens.

    Installed as a stream wrapper (rather than rewriting each message at its
    call site) so every stderr producer is covered: cli.py prints, structlog
    debug output, and error strings built in other modules.
    """

    _adeu_glyph_proxy = True

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        return self._stream.write(demote_glyphs(s))

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _DynamicStderr:
    """File-like proxy that resolves sys.stderr at each write.

    Long-lived consumers (structlog's logger factory) must not pin the stderr
    *object* that happened to exist at configure time: it may be replaced or
    closed later (glyph proxy installation, pytest's capsys teardown).
    """

    def write(self, s):
        return sys.stderr.write(s)

    def flush(self):
        stream = sys.stderr
        if stream is not None:
            stream.flush()


dynamic_stderr = _DynamicStderr()


def configure_cli_streams() -> None:
    """Force deterministic UTF-8 output and adapt status glyphs to the host.

    Call once at CLI entry, before anything is printed and before structlog
    captures sys.stderr. Never raises: hosts whose streams cannot be
    reconfigured simply keep their original behavior.
    """
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
