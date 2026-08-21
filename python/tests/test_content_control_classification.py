"""CC-1a — classification and ordinal assignment for the 16-control fixture.

Every expectation here is read off the normative listing in
``specs/content-controls/acceptance/fixture-standard.md``; the fixture body
itself is the shared ``shared/fixtures/cc_fixture.body.xml`` that
``scripts/make_cc_fixture.py`` and the node suite also read, so a change to the
fixture cannot silently desynchronise the two engines.

The node twin is ``node/packages/core/src/content_control_classification.test.ts``
and asserts the same table.
"""

import pytest

from adeu.utils.content_controls import (
    assign_ordinals,
    classify_sdt,
    iter_sdt_elements_in_order,
)
from tests.cc_fixture import cc_fixture_body_element

# (ordinal, class, tag, anchored, flags) — straight from fixture-standard.md.
EXPECTED = [
    (1, "richtext", "indemnity", True, ()),
    (2, "text", "client_name", True, ()),
    (3, "text", "counterparty", True, ()),
    (4, "dropdown", "governing_law", True, ()),
    (5, "date", "effective_date", True, ()),
    (6, "checkbox", "confidential", False, ()),
    (7, "text", "fixed_clause", True, ("locked",)),
    (8, "group", "std_terms", True, ("group",)),
    (9, "text", "notice_address", True, ()),
    (10, "text", "matter_number", True, ("bound",)),
    (11, "repeating", "deliverables", False, ()),
    (12, "repeating-item", None, False, ()),
    (13, "repeating-item", None, False, ()),
    (14, "text", "cell_role", True, ()),
    (15, "richtext", "row_approver", True, ()),
    (16, "richtext", "cell_notes", True, ()),
]


@pytest.fixture(scope="module")
def infos():
    body = cc_fixture_body_element()
    ordered = list(iter_sdt_elements_in_order(body))
    return [classify_sdt(el, i + 1) for i, el in enumerate(ordered)]


def test_fixture_has_exactly_sixteen_controls(infos):
    assert len(infos) == 16


@pytest.mark.parametrize("expected", EXPECTED, ids=lambda e: f"CC{e[0]}-{e[1]}")
def test_control_classification(infos, expected):
    ordinal, cls, tag, anchored, flags = expected
    info = infos[ordinal - 1]
    assert info.ordinal == ordinal
    assert info.cls == cls, f"CC:{ordinal} classified {info.cls!r}, expected {cls!r}"
    assert info.tag == tag
    assert info.anchored is anchored, f"CC:{ordinal} ({cls}) anchored={info.anchored}, expected {anchored}"
    assert info.flags == flags, f"CC:{ordinal} flags {info.flags} != {flags}"


def test_ordinals_are_document_ordered_and_gapless(infos):
    """A1.3: 1..16 in projection order, un-anchored classes consuming numbers."""
    assert [i.ordinal for i in infos] == list(range(1, 17))
    # The checkbox (CC:6) and the repeating trio (CC:11-13) are un-anchored yet
    # still consume ordinals — the property that makes ordinals stable when a
    # future change starts or stops anchoring a class.
    unanchored = [i.ordinal for i in infos if not i.anchored]
    assert unanchored == [6, 11, 12, 13]


def test_ordinals_are_stable_across_independent_loads():
    """A1.3: two independent loads assign identical ordinals."""
    first = [(i.ordinal, i.cls, i.tag) for i in assign_ordinals([cc_fixture_body_element()]).values()]
    second = [(i.ordinal, i.cls, i.tag) for i in assign_ordinals([cc_fixture_body_element()]).values()]
    assert first == second
    assert sorted(o for o, _, _ in first) == list(range(1, 17))


def test_nested_controls_are_ordered_container_first(infos):
    """The group (CC:8) precedes the control it wraps (CC:9).

    Pre-order matters: the open token of a container must be able to carry a
    lower ordinal than anything inside it, or block-level anchor pairs would
    interleave rather than nest.
    """
    assert infos[7].cls == "group"
    assert infos[8].tag == "notice_address"
    assert infos[10].cls == "repeating"
    assert [i.cls for i in infos[11:13]] == ["repeating-item", "repeating-item"]


def test_content_lock_distinguishes_delete_lock_from_content_lock(infos):
    """`sdtLocked` alone is delete-locked but editable — ledger-only, no flag."""
    group = infos[7]
    assert group.delete_locked is True
    assert group.content_locked is False, "w:lock=sdtLocked must NOT count as content-locked (spec §2)"
    # ...and it therefore emits `group`, never `locked`.
    assert group.flags == ("group",)

    fixed = infos[6]
    assert fixed.content_locked is True
    assert fixed.delete_locked is True, "sdtContentLocked implies delete-locked too"
    assert fixed.flags == ("locked",)


def test_richtext_containing_a_control_is_not_anchored(infos):
    """Spec §1: a richtext wrapping another control is ledger-only."""
    group = infos[7]
    assert group.has_nested_sdt is True
    # CC:16 is a plain in-cell richtext with no nested control, so it anchors.
    assert infos[15].has_nested_sdt is False
    assert infos[15].anchored is True


def test_dropdown_options_and_date_format_are_captured(infos):
    dropdown = infos[3]
    assert [display for display, _ in dropdown.options] == [
        "Ontario",
        "British Columbia",
        "Federal",
    ]
    assert [value for _, value in dropdown.options] == ["ON", "BC", "FED"]
    assert infos[4].date_format == "yyyy-MM-dd"


def test_checkbox_checked_state_and_binding_xpath(infos):
    assert infos[5].checked is True
    assert infos[9].bound is True
    assert infos[9].binding_xpath == "/root[1]/matter[1]"
    # A control with no binding reports None rather than "" so the ledger can
    # distinguish "not bound" from "bound to nothing".
    assert infos[2].bound is False
    assert infos[2].binding_xpath is None


def test_placeholder_state_is_detected_only_where_declared(infos):
    assert infos[1].showing_placeholder is True, "CC:2 carries w:showingPlcHdr"
    assert [i.ordinal for i in infos if i.showing_placeholder] == [2]


def test_tokens_render_with_normative_flag_order(infos):
    assert infos[0].open_token == "{#cc:1}"
    assert infos[0].close_token == "{#/cc:1}"
    assert infos[6].open_token == "{#cc:7 locked}"
    assert infos[7].open_token == "{#cc:8 group}"
    assert infos[9].open_token == "{#cc:10 bound}"
