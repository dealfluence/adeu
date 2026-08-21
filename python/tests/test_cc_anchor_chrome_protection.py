"""CC-1d / A1.6 — anchor tokens survive the chrome-stripping passes.

Two passes rewrite projected text for human consumption: the outline's
`_strip_inline_formatting` (markdown emphasis) and search's
`_emphasized_snippet` (highlight markers). Both already protected `{#...}`
tokens from marker *stripping* (QA 2026-07-23 F4), and that protection covers
CC-1's `{#cc:N}` anchors unchanged — the tests below pin it so a future tweak
to either regex cannot quietly drop them.

The gap A1.6 actually exposed is a different one, in both engines: a pass that
CUTS text does not know about tokens. Outline truncation at 200 chars and the
search radius ladder both used to slice straight through an anchor and emit
`{#cc:`. That is worse than dropping it — an agent reads a plausible target
that resolves to nothing. A1.6's rule is "the whole token, or omitted, never
split", and these tests enforce it on both cut sites.
"""

import io
import re

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.mcp_components._response_builders import (
    _balance_snippet_window,
    _emphasized_snippet,
    build_search_response,
)
from adeu.outline import (
    _OUTLINE_TEXT_MAX_CHARS as _OUTLINE_CAP,
)
from adeu.outline import _strip_inline_formatting, _truncate_outline_text
from tests.cc_fixture import cc_fixture_bytes

# A dangling `{#` with no closing brace, or a stray `}` with no opener.
_SPLIT_HEAD_RE = re.compile(r"\{#[^}\n]*$")
_SPLIT_TAIL_RE = re.compile(r"^[^{\n]*\}")


def _assert_no_split_anchor(text: str) -> None:
    assert not _SPLIT_HEAD_RE.search(text), f"split anchor (dangling opener) in {text!r}"


@pytest.fixture(scope="module")
def body() -> str:
    return extract_text_from_stream(io.BytesIO(cc_fixture_bytes()), clean_view=False, include_appendix=False)


# ---------------------------------------------------------------------------
# A1.6(a) — search snippets
# ---------------------------------------------------------------------------
def test_search_snippet_keeps_the_anchor_intact(body):
    """`{#cc:3}` survives the highlight pass unmangled."""
    result = build_search_response(body, "ACME", False, False, 1, "f.docx", False, None)
    assert "{#cc:3}**ACME** Corp{#/cc:3}" in result.content


def test_highlight_does_not_eat_anchor_underscores():
    """The word-edge `_` rule must not pair with an anchor's own characters."""
    region = "see {#cc:3}_ACME_{#/cc:3} now"
    out = _emphasized_snippet(region, [(region.index("ACME"), region.index("ACME") + 4)])
    assert "{#cc:3}" in out and "{#/cc:3}" in out


@pytest.mark.parametrize("radius", [2, 4, 6, 8, 12, 20])
def test_clamped_snippet_windows_never_split_an_anchor(body, radius):
    """The radius ladder is reachable in production, not theoretical.

    `build_search_response` clamps snippets whenever a result set exceeds the
    response budget, so any radius can occur. A window edge landing inside an
    anchor must move out to the token's edge.
    """
    line = next(ln for ln in body.splitlines() if "ACME" in ln)
    off = body.index(line)
    m_start, m_end = off + line.index("ACME"), off + line.index("ACME") + 4
    start, end = _balance_snippet_window(body, max(off, m_start - radius), min(off + len(line), m_end + radius))
    fragment = body[start:end]
    _assert_no_split_anchor(fragment)
    assert not _SPLIT_TAIL_RE.match(fragment), f"orphan closing brace in {fragment!r}"
    assert "ACME" in fragment, "widening must never drop the hit itself"


def test_window_widening_is_bounded_by_the_token(body):
    """Widening snaps to the token edge — it does not swallow the whole line."""
    line = next(ln for ln in body.splitlines() if "ACME" in ln)
    off = body.index(line)
    s = off + line.index("ACME")
    start, end = _balance_snippet_window(body, s - 2, s + 6)
    assert body[start:end] == "{#cc:3}ACME C"


# ---------------------------------------------------------------------------
# A1.6(b) — outline
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "{#cc:1}",
        "{#/cc:1}",
        "{#cc:7 locked}",
        "{#cc:8 group}",
        "Text {#cc:3}ACME Corp{#/cc:3} tail",
        "{#_Ref444615940} legacy bookmark anchor",
    ],
)
def test_emphasis_stripping_leaves_anchors_alone(text):
    assert _strip_inline_formatting(text) == text


def test_emphasis_stripping_still_strips_markers_around_an_anchor():
    """Protection is for the token's OWN characters, not its neighbours."""
    assert _strip_inline_formatting("_{#cc:3}_") == "{#cc:3}"
    assert _strip_inline_formatting("**bold** {#cc:3}") == "bold {#cc:3}"


def test_outline_truncation_never_splits_an_anchor():
    """The 200-char cap landing mid-token must drop the token, not halve it."""
    out = _truncate_outline_text("X" * 195 + "{#cc:3}tail")
    _assert_no_split_anchor(out)
    assert out.endswith("…")
    assert "{#cc" not in out, "a token that does not fit is omitted entirely"


def test_outline_truncation_keeps_an_anchor_that_fits():
    text = "X" * 150 + "{#cc:3} and then a good deal more text past the cap " + "Y" * 80
    out = _truncate_outline_text(text)
    assert "{#cc:3}" in out
    _assert_no_split_anchor(out)


@pytest.mark.parametrize("pad", range(190, 201))
def test_truncation_is_safe_at_every_boundary_offset(pad):
    """Sweep the cut across the token: no offset may produce a fragment."""
    out = _truncate_outline_text("X" * pad + "{#cc:12}" + "Z" * 40)
    _assert_no_split_anchor(out)
    assert "{#cc:12}" in out or "{#cc" not in out


def test_short_text_is_untouched():
    assert _truncate_outline_text("short {#cc:3} text") == "short {#cc:3} text"


# ---------------------------------------------------------------------------
# A1.6(b) integration — the FAST outline path, which is what production runs.
#
# `extract_outline` has two heading-text derivations. The legacy path rebuilds
# text with `build_paragraph_text`, which carries no sdt anchors at all and so
# satisfies A1.6 by omission. The fast path SLICES the projected body, which
# since CC-1b contains `{#cc:N}` — and both MCP servers and the CLI take it,
# because they pass `paragraph_offsets`. A suite that exercised only the legacy
# path would be green over the one code path that cannot exhibit the bug.
# ---------------------------------------------------------------------------
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _outline_texts(pad_before: int) -> list[str]:
    from docx import Document
    from docx.oxml import parse_xml

    from adeu.ingest import _extract_text_from_doc
    from adeu.outline import extract_outline
    from adeu.pagination import paginate, split_structural_appendix

    # A Heading1 pStyle, where the node twin uses w:outlineLvl: python-docx's
    # default template ships a styles.xml that defines Heading1, and this
    # engine does NOT treat a bare w:outlineLvl as a heading (the node engine
    # does — see CC-15 on the board). The `assert nodes` below is what keeps that asymmetry
    # honest instead of letting the suite pass vacuously.
    body_xml = (
        f"<w:p {_W_NS}><w:pPr><w:pStyle w:val='Heading1'/></w:pPr>"
        f"<w:r><w:t xml:space='preserve'>{'X' * pad_before}</w:t></w:r>"
        "<w:sdt><w:sdtPr><w:alias w:val='Party'/><w:tag w:val='party'/><w:text/></w:sdtPr>"
        "<w:sdtContent><w:r><w:t xml:space='preserve'>ACME</w:t></w:r></w:sdtContent></w:sdt>"
        f"<w:r><w:t xml:space='preserve'>{'Z' * 60}</w:t></w:r></w:p>"
    )
    doc = Document()
    doc.element.body.insert(0, parse_xml(body_xml))
    text, offsets = _extract_text_from_doc(doc, clean_view=False, include_appendix=False, return_paragraph_offsets=True)
    projected, _ = split_structural_appendix(text)
    assert "{#cc:" in projected, "fixture must project the anchor at all"
    pg = paginate(projected, structural_appendix="")
    nodes = extract_outline(doc, projected, pg.body_pages, pg.body_page_offsets, paragraph_offsets=offsets)
    assert nodes, "fixture produced no outline entry"
    return [n.text for n in nodes]


@pytest.mark.parametrize("pad", [190, 193, 195, 197, 199, 200, 205])
def test_fast_outline_never_splits_an_anchor(pad):
    for text in _outline_texts(pad):
        _assert_no_split_anchor(text)
        if "{#cc" in text:
            assert re.search(r"\{#cc:\d+[^}]*\}", text), "whole token, or omitted"


def test_fast_outline_still_truncates():
    """QA 2026-07-23 F13b: an outline is a navigation map, not the document."""
    for text in _outline_texts(300):
        assert len(text) <= _OUTLINE_CAP + 1  # + the ellipsis
        assert text.endswith("…")
