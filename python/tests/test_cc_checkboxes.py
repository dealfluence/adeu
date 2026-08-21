"""CC-1c — checkbox projection (A1.8).

A `w14:checkbox` control projects as the three-character token `[x]` or `[ ]`,
never as the raw ballot glyph. The node twin is
`node/packages/core/src/cc_checkboxes.test.ts`.

Two facts these tests encode came out of the COM reconnaissance against real
Word 16.0 rather than from the spec, and both are load-bearing:

* The mark is read from `w14:checked`, not from the glyph run. Word restores
  `w14:checked` when a checkbox toggle is REJECTED, so the attribute is the
  settled value; the glyph can lag it inside a tracked change.
* Word writes the glyph as literal `w:t` text, not `w:sym`. That is what makes
  the substitution one character for one character, so no offset arithmetic
  anywhere has to learn about a width difference.

The corpus supplied the trap in `test_bare_glyphs_outside_a_control_are_left_alone`:
of ~7,700 checkboxes across ten documents, every one is `w14:checkbox` and not
one is ticked — but `odot_uic_drywell` also carries two bare `U+2610` runs
sitting in ordinary prose, outside any control. A substitution keyed on the
character rather than on the control would invent two checkboxes there.
"""

import io
from pathlib import Path

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.redline.mapper import DocumentMapper
from tests.sdt_fixtures import build_sdt_docx

CHECKED_GLYPH = "\u2612"
UNCHECKED_GLYPH = "\u2610"

_MS_GOTHIC = '<w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr>'


def _checkbox(sdt_id: int, checked: bool, glyph: str | None = None) -> str:
    """A `w14:checkbox` control shaped exactly as Word writes one."""
    if glyph is None:
        glyph = CHECKED_GLYPH if checked else UNCHECKED_GLYPH
    val = "1" if checked else "0"
    content = f"<w:r>{_MS_GOTHIC}<w:t>{glyph}</w:t></w:r>" if glyph else ""
    return (
        f'<w:sdt><w:sdtPr><w:tag w:val="cb{sdt_id}"/><w:id w:val="{sdt_id}"/>'
        f'<w14:checkbox><w14:checked w14:val="{val}"/>'
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
        f"</w14:checkbox></w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>"
    )


def _para(*fragments: str) -> str:
    return "<w:p>" + "".join(fragments) + "</w:p>"


def _text(s: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{s}</w:t></w:r>'


@pytest.fixture(scope="module")
def both_states(tmp_path_factory) -> bytes:
    """A1.8's fixture variant: one checked and one unchecked control."""
    body = _para(_text("Confidential: "), _checkbox(301, True)) + _para(_text("Urgent: "), _checkbox(302, False))
    path = build_sdt_docx(tmp_path_factory.mktemp("cb") / "both.docx", body)
    return Path(path).read_bytes()


def _project(data: bytes, clean_view: bool = False) -> str:
    return extract_text_from_stream(io.BytesIO(data), clean_view=clean_view, include_appendix=False)


def _mapped(data: bytes, clean_view: bool = False) -> str:
    return DocumentMapper(Document(io.BytesIO(data)), clean_view=clean_view).full_text


def test_a1_8_tokens_replace_glyphs_in_both_directions(both_states):
    """A1.8 — `[x]` for checked, `[ ]` for unchecked, and no glyph survives."""
    raw = _project(both_states)
    assert "Confidential: [x]" in raw
    assert "Urgent: [ ]" in raw
    assert CHECKED_GLYPH not in raw
    assert UNCHECKED_GLYPH not in raw


@pytest.mark.parametrize("clean_view", [False, True])
def test_ingest_and_mapper_agree(both_states, clean_view):
    """The Virtual Text contract. The whole substitution is worthless if the
    two projections disagree by even one character, because every offset the
    redline engine computes against mapper text would then be wrong."""
    assert _mapped(both_states, clean_view) == _project(both_states, clean_view)


def test_checkbox_tokens_persist_in_the_clean_view(both_states):
    """Spec §6 — checkbox tokens are structural, like anchors, not commentary.

    The clean view is the accepted-changes view, and an accepted document
    still has checkboxes in it.
    """
    clean = _project(both_states, clean_view=True)
    assert "Confidential: [x]" in clean
    assert "Urgent: [ ]" in clean


def test_the_token_is_exactly_three_characters(both_states):
    """A3.8's edit surface depends on the token's width being fixed."""
    raw = _project(both_states)
    for line, token in (("Confidential: ", "[x]"), ("Urgent: ", "[ ]")):
        start = raw.index(line) + len(line)
        assert raw[start : start + 3] == token
        assert len(token) == 3


def test_the_mark_follows_w14_checked_not_the_glyph(tmp_path):
    """Read the attribute, not the picture — the COM battery's finding.

    Word restores `w14:checked` when a toggle is rejected, so a document can
    legitimately hold `checked=1` while the glyph run still shows the
    unchecked box inside a pending revision. Projecting the glyph would render
    a confident `[ ]` over a box that is, once the review settles, ticked.
    This fixture forces the disagreement directly.
    """
    body = _para(_text("Disagreeing: "), _checkbox(303, True, glyph=UNCHECKED_GLYPH))
    path = build_sdt_docx(tmp_path / "disagree.docx", body)
    raw = _project(Path(path).read_bytes())
    assert "Disagreeing: [x]" in raw, "w14:checked=1 must win over the unchecked glyph"
    assert UNCHECKED_GLYPH not in raw


def test_bare_glyphs_outside_a_control_are_left_alone(tmp_path):
    """The corpus trap: `odot_uic_drywell` has 21 ballot glyphs but 19 controls.

    The other two are Segoe UI Symbol runs in ordinary prose. They are not
    checkboxes, nothing can toggle them, and rewriting them to `[ ]` would
    fabricate two controls in a document with 19 real ones to hide among.
    A1.8's "no glyphs" clause is therefore scoped to control CONTENT.
    """
    body = _para(_text(f"See the box {UNCHECKED_GLYPH} in the margin.")) + _para(_text("Real: "), _checkbox(304, False))
    path = build_sdt_docx(tmp_path / "bare.docx", body)
    raw = _project(Path(path).read_bytes())
    assert f"See the box {UNCHECKED_GLYPH} in the margin." in raw, "prose glyph must survive"
    assert "Real: [ ]" in raw
    assert raw.count(UNCHECKED_GLYPH) == 1, "exactly the prose one, never the control's"


def test_an_empty_checkbox_still_projects_three_characters(tmp_path):
    """Robustness: Word always writes the glyph run, but a generator might not.

    Falling back to a virtual mark keeps the token three characters wide
    instead of degrading to a two-character `[]` that no edit surface expects.
    """
    body = _para(_text("Empty: "), _checkbox(305, True, glyph=""))
    path = build_sdt_docx(tmp_path / "empty.docx", body)
    data = Path(path).read_bytes()
    assert "Empty: [x]" in _project(data)
    assert _mapped(data) == _project(data)


def test_the_mark_carries_no_emphasis_markers(tmp_path):
    """A bold glyph run must not project `[**x**]`.

    The mark is chrome, not prose. Emphasis on it would hand every
    marker-stripping pass (outline, search snippets) something to mangle,
    which is the QA F4/F22b failure class the anchor work already guards.
    """
    bold_glyph = f'<w:r><w:rPr><w:b/><w:rFonts w:ascii="MS Gothic"/></w:rPr><w:t>{CHECKED_GLYPH}</w:t></w:r>'
    body = _para(
        _text("Bold box: "),
        '<w:sdt><w:sdtPr><w:tag w:val="cb306"/><w:id w:val="306"/>'
        '<w14:checkbox><w14:checked w14:val="1"/>'
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
        f"</w14:checkbox></w:sdtPr><w:sdtContent>{bold_glyph}</w:sdtContent></w:sdt>",
    )
    path = build_sdt_docx(tmp_path / "bold.docx", body)
    data = Path(path).read_bytes()
    raw = _project(data)
    assert "Bold box: [x]" in raw
    assert "**" not in raw
    assert _mapped(data) == _project(data)
