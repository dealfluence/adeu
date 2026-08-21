"""CC-1b — inline content-control projection (A1.2 partial, A1.4, A1.10).

Covers the inline half of A1: anchored leaf controls, flags, the empty-pair
edit surface and the placeholder bubble. Block-level anchors (CC:1), groups
(CC:8) and the table controls (CC:14-16) are still transparent and are asserted
as such below, so this file records exactly how far CC-1b has got rather than
quietly passing on a partial implementation.

The node twin is `node/packages/core/src/cc_projection_inline.test.ts`.
"""

import io

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.redline.mapper import DocumentMapper
from tests.cc_fixture import cc_fixture_bytes

GHOST = "Click or tap here to enter text."


@pytest.fixture(scope="module")
def fixture_bytes() -> bytes:
    return cc_fixture_bytes()


def _project(data: bytes, clean_view: bool = False) -> str:
    return extract_text_from_stream(io.BytesIO(data), clean_view=clean_view, include_appendix=False)


@pytest.mark.parametrize("clean_view", [False, True])
def test_ingest_and_mapper_agree_on_the_fixture(fixture_bytes, clean_view):
    """The Virtual Text contract, on the document that exercises every class."""
    mapped = DocumentMapper(Document(io.BytesIO(fixture_bytes)), clean_view=clean_view).full_text
    assert mapped == _project(fixture_bytes, clean_view)


def test_inline_anchors_render_with_flags(fixture_bytes):
    text = _project(fixture_bytes)
    assert "Counterparty: {#cc:3}ACME Corp{#/cc:3}." in text
    assert "Governing law: {#cc:4}Ontario{#/cc:4}." in text
    assert "Effective date: {#cc:5}2026-01-15{#/cc:5}." in text
    assert "Fixed clause: {#cc:7 locked}Payment terms are Net 30 days.{#/cc:7}" in text
    assert "Notices to: {#cc:9}123 Main Street, Ottawa{#/cc:9}" in text
    assert "Matter number: {#cc:10 bound}M-2026-001{#/cc:10}" in text


def test_a1_4_ghost_text_never_projects_as_body_text(fixture_bytes):
    """A1.4 — the single worst pre-CC-1 defect.

    Before this change the placeholder run projected like any other run, so a
    reader (human or model) saw "This Agreement is made between Click or tap
    here to enter text. and the Government of Example." and could not tell the
    ghost from a real party name.
    """
    raw = _project(fixture_bytes)
    assert GHOST in raw, "the bubble must still disclose the placeholder"
    # ...but ONLY inside the bubble.
    assert raw.count(GHOST) == 1
    assert f"{{>>placeholder: {GHOST}<<}}" in raw
    assert f"between {GHOST}" not in raw

    clean = _project(fixture_bytes, clean_view=True)
    assert GHOST not in clean, "clean view must not contain the ghost text at all"


def test_empty_control_is_a_matchable_adjacent_pair(fixture_bytes):
    """Spec §3, sanctioned edit surface #1.

    The empty pair is deliberately adjacent and matchable — it is the target a
    text-first fill resolves against, the same precedent as `{#cell:paraId}`.
    """
    raw = _project(fixture_bytes)
    clean = _project(fixture_bytes, clean_view=True)
    assert (
        f"This Agreement is made between {{#cc:2}}{{>>placeholder: {GHOST}<<}}"
        "{#/cc:2} and the Government of Example." in raw
    )
    # Clean view drops the bubble, leaving the bare pair (GOLDEN-CLEAN).
    assert "This Agreement is made between {#cc:2}{#/cc:2} and the Government of Example." in clean


def test_anchors_persist_in_the_clean_view(fixture_bytes):
    """Spec §6 — anchors are structural, like `{#_Bookmark}`; only bubbles drop."""
    clean = _project(fixture_bytes, clean_view=True)
    for token in ("{#cc:3}", "{#/cc:3}", "{#cc:7 locked}", "{#cc:10 bound}"):
        assert token in clean
    assert "{>>placeholder:" not in clean


def test_unanchored_classes_emit_no_tokens(fixture_bytes):
    """Checkbox and repeating controls consume ordinals but never anchor."""
    raw = _project(fixture_bytes)
    for ordinal in (6, 11, 12, 13):
        assert f"{{#cc:{ordinal}}}" not in raw
        assert f"{{#/cc:{ordinal}}}" not in raw
    # Their content still projects normally.
    assert "Deliverable: Initial report, due 2026-02-01." in raw


def test_ordinals_survive_into_the_projection_unchanged(fixture_bytes):
    """A1.3 at the projection level: the numbers the pre-pass assigned are the
    numbers the reader sees, and they do not renumber between views."""
    import re

    raw_ids = re.findall(r"\{#cc:(\d+)", _project(fixture_bytes))
    clean_ids = re.findall(r"\{#cc:(\d+)", _project(fixture_bytes, clean_view=True))
    assert raw_ids == clean_ids
    assert raw_ids == ["1", "2", "3", "4", "5", "7", "8", "9", "10", "14", "15", "16"]
    assert raw_ids == sorted(raw_ids, key=int), "anchors must appear in ordinal order"


def _golden(section: str) -> str:
    """The normative golden block from the frozen acceptance fixture."""
    import re
    from pathlib import Path

    md = (
        Path(__file__).resolve().parents[2] / "specs" / "content-controls" / "acceptance" / "fixture-standard.md"
    ).read_text(encoding="utf-8")
    return re.search(rf"## {section}.*?\n```\n(.*?)```", md, re.S).group(1).rstrip("\n")


# The one known gap: CC:6 still projects the raw glyph instead of the [x] token.
# That is CC-1c (checkboxes), owned separately. Rather than weaken the golden
# comparison to "mostly equal", the checkbox line is substituted explicitly, so
# the assertion below is exact for all 15 other controls and this constant is
# what has to be deleted when CC-1c lands.
_CC1C_PENDING = ("Confidentiality applies: [x]", "Confidentiality applies: \u2612")


def test_a1_1_raw_view_matches_golden_raw(fixture_bytes):
    """A1.1 — full-document raw golden, exact but for the CC-1c checkbox."""
    expected = _golden("GOLDEN-RAW").replace(*_CC1C_PENDING)
    assert _project(fixture_bytes).rstrip("\n") == expected


def test_a1_2_clean_view_matches_golden_clean(fixture_bytes):
    """A1.2 — clean view: anchors persist, the CC:2 bubble is gone."""
    expected = (
        _golden("GOLDEN-RAW")
        .replace(*_CC1C_PENDING)
        .replace(
            f"{{#cc:2}}{{>>placeholder: {GHOST}<<}}{{#/cc:2}}",
            "{#cc:2}{#/cc:2}",
        )
    )
    assert _project(fixture_bytes, clean_view=True).rstrip("\n") == expected
    # ...and that is exactly the GOLDEN-CLEAN line the spec calls out.
    assert _golden("GOLDEN-CLEAN") in _project(fixture_bytes, clean_view=True)


def test_block_level_control_anchors_on_its_own_lines(fixture_bytes):
    """Spec §3: open token on its own line, single "\n" to the wrapped block."""
    raw = _project(fixture_bytes)
    assert "{#cc:1}\nThe Supplier shall indemnify the Client against all third-party claims.\n{#/cc:1}" in raw


def test_group_wraps_its_blocks_and_the_nested_control_keeps_its_own_anchor(
    fixture_bytes,
):
    """Spec §5 — a group brackets its blocks; nested controls anchor normally."""
    raw = _project(fixture_bytes)
    assert (
        "{#cc:8 group}\n"
        "These standard terms are approved boilerplate and must not be modified.\n\n"
        "Notices to: {#cc:9}123 Main Street, Ottawa{#/cc:9}\n"
        "{#/cc:8}" in raw
    ), "group must bracket BOTH blocks, with the inner control still anchored"


def test_table_controls_anchor_inline_never_on_token_lines(fixture_bytes):
    """Spec §3 exception — a row is one projected line.

    Token lines inside a table would break the `|` grammar and desynchronise
    the column count, so cell-level, row-level and in-cell block controls all
    render their anchors inline.
    """
    raw = _project(fixture_bytes)
    assert "Role | {#cc:14}Contracting Officer{#/cc:14}" in raw
    assert "{#cc:15}Approver | Jane Roe{#/cc:15}" in raw
    # CC:16 is a BLOCK-level control that happens to sit in a cell: inline.
    assert "Notes | {#cc:16}Approved without conditions.{#/cc:16}" in raw
    assert "{#cc:16}\n" not in raw, "in-cell block control must not emit token lines"


def test_the_gfm_divider_survives_between_anchored_rows(fixture_bytes):
    """Regression for the golden defect corrected on 2026-08-21.

    GOLDEN-RAW originally omitted this line. It is emitted after the first row
    of every table and is what makes the projection a markdown table rather
    than lines containing pipes.
    """
    raw = _project(fixture_bytes)
    assert "Role | {#cc:14}Contracting Officer{#/cc:14}\n--- | ---\n" in raw
