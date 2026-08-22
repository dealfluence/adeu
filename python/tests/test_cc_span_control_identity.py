"""CC-4 — every projected SPAN knows which content controls enclose it.

`test_cc_run_control_identity.py` pins the run-level half of this. This file
pins the half the gates actually consume: `TextSpan.sdt_stack` and the
`control_ranges` / `controls_at` / `controls_intersecting` queries derived
from it, over the standard 16-control fixture.

The two halves are separate because they have different blind spots and only
together cover the document. The run walk sees inline controls and misses
block ones (a block control wraps whole paragraphs; `iter_paragraph_content`
is never told it opened). The mapper's block cursor sees block controls and
misses inline ones (it does not descend into runs). A span concatenates both,
and the assertions below are chosen so that either source going missing fails
something: CC:7 is inline-only, CC:8 is block-only, and CC:9 is an inline
control nested inside a block one, which no single source can produce.

This is the control-wall twin of `part_index`, and deliberately so — the OPC
part wall is the same shape of problem with a decade of scar tissue behind it
(QA 2026-07-18 C1), so the gates inherit its structure rather than invent one.
"""

import io

import pytest
from docx import Document

from adeu.redline.mapper import DocumentMapper
from tests.cc_fixture import cc_fixture_bytes


@pytest.fixture(scope="module")
def mapper() -> DocumentMapper:
    return DocumentMapper(Document(io.BytesIO(cc_fixture_bytes())))


def ordinals(infos) -> list[int]:
    return [i.ordinal for i in infos]


def at(mapper: DocumentMapper, needle: str) -> int:
    """Offset of `needle` in the projection, asserted unique."""
    first = mapper.full_text.find(needle)
    assert first != -1, f"{needle!r} not in projection"
    assert mapper.full_text.find(needle, first + 1) == -1, f"{needle!r} is ambiguous"
    return first


def test_body_text_outside_every_control_is_unenclosed(mapper):
    idx = at(mapper, "SERVICES AGREEMENT (fixture)")
    assert mapper.controls_at(idx) == []


def test_an_inline_control_encloses_its_own_content_only(mapper):
    # CC:7, the content-locked one A3.1 rejects edits inside.
    inside = at(mapper, "Payment terms are Net 30 days.")
    assert ordinals(mapper.controls_at(inside)) == [7]
    # "Fixed clause: " is the run BEFORE the control in the same paragraph.
    # If the inline stack leaked past sdt_end this would also report CC:7,
    # and G1 would refuse edits to ordinary body text.
    assert mapper.controls_at(at(mapper, "Fixed clause: ")) == []


def test_a_block_control_encloses_whole_paragraphs(mapper):
    # CC:1 is block-level: no run inside it carries CC:1 on Run.sdt_stack,
    # so this assertion passes only via the mapper's block cursor.
    inside = at(mapper, "The Supplier shall indemnify")
    assert ordinals(mapper.controls_at(inside)) == [1]


def test_nesting_reports_outermost_first(mapper):
    # CC:9 (inline) inside CC:8 (block group). Neither source alone can
    # produce this pair, and A3.2 depends on the ORDER: the group is the
    # locked region, the nested leaf is the editable exception.
    inside = at(mapper, "123 Main Street, Ottawa")
    assert ordinals(mapper.controls_at(inside)) == [8, 9]
    # Boilerplate in the group but outside the nested leaf: group only.
    # This is exactly the text A3.2(a) must reject and A3.2(b) must not.
    boilerplate = at(mapper, "approved boilerplate")
    assert ordinals(mapper.controls_at(boilerplate)) == [8]


def test_unanchored_controls_get_ranges_too(mapper):
    # CC:6 is a checkbox: UNANCHORED, so it projects "[x]" and no {#cc:6}
    # token. A gate reading anchor events would not see it at all.
    #
    # The brackets are virtual chrome and the MARK is the run-backed span, so
    # the control owns offset+1 and not offset. That asymmetry is load-bearing
    # for G11: the toggle edit targets "[x]", whose first character is not
    # inside the control, so G11 cannot be written as a containment test on
    # the target's start offset alone.
    bracket = at(mapper, "[x]")
    assert mapper.controls_at(bracket) == []
    assert ordinals(mapper.controls_at(bracket + 1)) == [6]
    assert ordinals(mapper.controls_intersecting(bracket, 3)) == [6]


def test_intersecting_reports_a_span_that_crosses_a_wall(mapper):
    # A3.10's target: starts outside CC:3, ends inside it. This is the query
    # G14 segments on, so it must report the control rather than stay silent.
    target = "Counterparty: {#cc:3}ACME Corp"
    start = at(mapper, target)
    assert ordinals(mapper.controls_intersecting(start, len(target))) == [3]


def test_a_zero_length_range_reports_nothing(mapper):
    # An insertion point is not "inside" anything for lock purposes; the
    # boundary logic owns it. If this returned the enclosing control, every
    # insertion adjacent to a locked control would be refused.
    inside = at(mapper, "Payment terms are Net 30 days.")
    assert mapper.controls_intersecting(inside, 0) == []


def test_control_ranges_cover_exactly_the_content(mapper):
    by_ordinal = {info.ordinal: (start, end) for start, end, info in mapper.control_ranges}
    start, end = by_ordinal[7]
    assert mapper.full_text[start:end] == "Payment terms are Net 30 days."


def test_table_cell_and_row_controls_are_enclosing_too(mapper):
    # CC:14 (cell-level) and CC:15 (row-level) wrap w:tc / w:tr, which the
    # mapper reaches through _map_table rather than the block or run walks.
    # Both were invisible to the first cut of this field — table controls are
    # the third structural kind and they need their own push site.
    assert ordinals(mapper.controls_at(at(mapper, "Contracting Officer"))) == [14]
    assert ordinals(mapper.controls_at(at(mapper, "Jane Roe"))) == [15]
    # CC:16 is an ordinary block control that merely lives inside a cell; it
    # must NOT pick up a phantom cell wrapper.
    assert ordinals(mapper.controls_at(at(mapper, "Approved without conditions."))) == [16]


def test_every_content_bearing_control_has_a_range(mapper):
    # A control silently missing from control_ranges is a hole in every gate
    # at once, and the failure mode is permissive, not loud — so assert the
    # whole set, not samples.
    #
    # CC:2 is the sole exclusion and is not an oversight: it is EMPTY, and its
    # placeholder ghost projects as a virtual {>>placeholder: ...<<} bubble
    # rather than as content. It therefore has no content range at all, which
    # is why G8 (A3.7) cannot be a span-intersection gate like its siblings
    # and must match the target against the control's placeholder text.
    present = sorted(info.ordinal for _, _, info in mapper.control_ranges)
    assert present == [n for n in range(1, 17) if n != 2]
