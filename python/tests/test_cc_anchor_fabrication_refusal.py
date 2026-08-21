"""CC-1e / A1.7 — anchor fabrication, mutation and deletion are refused.

`{#cc:N}` / `{#/cc:N}` are read-only projections of a control's structure. An
agent that can write them can invent a control that does not exist, silently
unbalance a pair, or strip a `locked` flag it is not allowed to clear.

VAL-OBS-9 already refused anchors that GAINED copies, so A1.7(a) fabrication
and A1.7(c) flag-stripping were covered before this task. A1.7(b) — deletion —
was not: that loop iterates `new_text`'s anchors, so an edit whose target
covers `{#/cc:3}` and whose `new_text` omits it had nothing to iterate and
passed.

The refusal is scoped to `cc` anchors on purpose. Two anchor classes are
deliberate TARGETING surfaces that a blanket symmetric rule would break, and
both are pinned below: `{#cell:paraId}` empty-cell writes, and the empty pair
`{#cc:N}{#/cc:N}` that spec-projection.md §3 names as sanctioned edit surface
#1 (the text-first fill CC-4/CC-5 route through set_field semantics).
"""

import io

import pytest

from adeu.models import ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine, validate_edit_strings
from tests.cc_fixture import cc_fixture_bytes

_REFUSAL = "content-control anchor markers"


def _errors(target: str, new: str) -> list[str]:
    return validate_edit_strings([ModifyText(target_text=target, new_text=new)])


def _refused(target: str, new: str) -> bool:
    return any(_REFUSAL in e for e in _errors(target, new))


# ---------------------------------------------------------------------------
# A1.7(a) / (b) / (c) — the three named cases
# ---------------------------------------------------------------------------
def test_a1_7_a_fabricating_an_anchor_is_refused():
    assert _refused("Counterparty: ", "Counterparty: {#cc:99}ACME{#/cc:99}")


def test_a1_7_b_deleting_a_closing_anchor_is_refused():
    """The regression this task existed for: deletion had no check at all."""
    assert _refused("ACME Corp{#/cc:3}", "ACME Corp")


def test_a1_7_b_deleting_an_opening_anchor_is_refused():
    assert _refused("{#cc:3}ACME Corp", "ACME Corp")


def test_a1_7_b_deleting_both_halves_is_refused():
    """Not the empty-pair surface: this target carries CONTENT between them."""
    assert _refused("{#cc:3}ACME Corp{#/cc:3}", "ACME Corp")


def test_a1_7_c_stripping_a_flag_is_refused():
    assert _refused("{#cc:7 locked}", "{#cc:7}")


def test_adding_a_flag_is_refused():
    assert _refused("{#cc:7}", "{#cc:7 locked}")


def test_renumbering_an_anchor_is_refused():
    assert _refused("{#cc:3}ACME{#/cc:3}", "{#cc:4}ACME{#/cc:4}")


def test_swapping_open_for_close_is_refused():
    assert _refused("{#cc:3}ACME{#/cc:3}", "{#/cc:3}ACME{#cc:3}")


# ---------------------------------------------------------------------------
# What must STAY legal — the reason this is not a blanket symmetric rule
# ---------------------------------------------------------------------------
def test_editing_content_between_the_anchors_is_allowed():
    """The whole point: the control's CONTENT is editable."""
    assert not _refused("{#cc:3}ACME Corp{#/cc:3}", "{#cc:3}Beta Ltd{#/cc:3}")


@pytest.mark.parametrize(
    "target",
    [
        "{#cc:5}{#/cc:5}",
        "{#cc:5 locked}{#/cc:5}",
        "{#cc:2}{>>placeholder: Click or tap here to enter text.<<}{#/cc:2}",
    ],
)
def test_the_empty_pair_fill_surface_stays_open(target):
    """spec-projection.md §3, sanctioned edit surface #1.

    The anchors are not being deleted here — the wrapper survives and only the
    control's content changes. Refusing this would close the surface CC-4/CC-5
    build the text-first fill on.
    """
    assert not _refused(target, "Jane Roe")


def test_the_empty_cell_write_surface_stays_open():
    """`{#cell:paraId}` is the precedent the empty pair was modelled on."""
    assert not _refused("{#cell:abc123}", "Hello")


def test_a_mismatched_pair_is_not_the_empty_pair_surface():
    """Open and close must be the SAME ordinal to count as a fill."""
    assert _refused("{#cc:5}{#/cc:6}", "Jane Roe")


def test_bookmark_anchors_keep_their_old_asymmetric_rule():
    """VAL-OBS-9 is untouched: `{#_Ref}` may still be dropped by a target."""
    assert not _refused("{#_Ref44}old", "{#_Ref44}new")
    assert not any(_REFUSAL in e for e in _errors("{#_Ref44}old", "new"))


def test_plain_edits_are_unaffected():
    assert _errors("old text", "new text") == []


# ---------------------------------------------------------------------------
# A1.7 — "the document is unchanged"
# ---------------------------------------------------------------------------
def test_the_document_is_unchanged_after_a_refusal():
    """Refusal must reach the caller as BatchValidationError, with no write."""
    data = cc_fixture_bytes()
    engine = RedlineEngine(io.BytesIO(data))
    before = engine.doc.element.xml

    with pytest.raises(BatchValidationError) as exc:
        engine.process_batch([ModifyText(target_text="ACME Corp{#/cc:3}", new_text="ACME Corp")])

    assert any(_REFUSAL in e for e in exc.value.errors)
    assert engine.doc.element.xml == before, "a refused batch must not mutate the document"
