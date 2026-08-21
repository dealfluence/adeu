"""CC-4 / A3.12 — context widening never crosses a locked control wall.

spec-gates.md §4: widened context MUST NOT cross a content-locked control
boundary or a group wall, "otherwise diff output stops being closed under
apply". That closure property is the whole point: `adeu diff` exists to emit a
batch that `adeu apply` accepts, and a widened target reaching into a locked
control is one the gates added in this same task would then refuse. The tool
would be emitting work its own engine rejects.

The walls are read back out of the projection (`{#cc:7 locked}`), not taken
from the mapper, so these tests operate on text alone — which is also the only
thing `make_edits_self_contained` receives.
"""

from adeu.diff import _locked_control_walls, make_edits_self_contained
from adeu.models import ModifyText


def pinned(target: str, new: str, idx: int) -> ModifyText:
    edit = ModifyText(type="modify", target_text=target, new_text=new)
    edit._match_start_index = idx
    return edit


def test_only_flagged_controls_are_walls():
    # An ordinary editable control is NOT a wall. Clamping on it would block
    # legitimate widening for no safety gain — the gates permit edits there.
    text = "a {#cc:3}ACME{#/cc:3} b {#cc:7 locked}Fixed{#/cc:7} c {#cc:8 group}G{#/cc:8} d"
    walls = _locked_control_walls(text)
    assert len(walls) == 2
    assert text[walls[0][0] : walls[0][1]] == "{#cc:7 locked}Fixed{#/cc:7}"
    assert text[walls[1][0] : walls[1][1]] == "{#cc:8 group}G{#/cc:8}"


def test_an_unbalanced_anchor_produces_no_wall():
    # Conservative on malformed input: no clamp beats a bogus clamp, and
    # unbalanced anchors are CC-1e's problem, not this function's.
    assert _locked_control_walls("x {#cc:7 locked}Fixed y") == []


def test_widening_stops_before_a_locked_control():
    # "Total" appears twice; the only disambiguating text to the right of the
    # first is inside a locked control. Widening must not reach into it.
    text = "Total {#cc:7 locked}Net 30{#/cc:7} and later Total again."
    edit = pinned("Total", "Sum", text.index("Total"))
    surviving = make_edits_self_contained([edit], text)

    assert len(surviving) == 1
    widened = surviving[0].target_text
    assert "{#cc:7" not in widened, f"widened into the locked control: {widened!r}"
    assert "Net 30" not in widened


def test_widening_inside_a_locked_control_stays_inside_it():
    # The mirror case. An edit already inside a locked control (only reachable
    # with ignore_control_locks) must not widen OUT across the wall either:
    # the resulting target would straddle the boundary and be refused by G14.
    text = "before {#cc:7 locked}Net 30 then Net 30{#/cc:7} after"
    second = text.index("Net 30", text.index("Net 30") + 1)
    edit = pinned("Net 30", "Net 90", second)
    surviving = make_edits_self_contained([edit], text)

    widened = surviving[0].target_text
    assert "before" not in widened
    assert "after" not in widened


def test_widening_is_unaffected_where_no_wall_intervenes():
    # The guard must not be a blanket brake: with no locked control in play,
    # widening still reaches as far as it needs to disambiguate.
    text = "Total alpha and later Total beta."
    edit = pinned("Total", "Sum", 0)
    surviving = make_edits_self_contained([edit], text)
    assert surviving[0].target_text != "Total"
    assert "alpha" in surviving[0].target_text


def test_the_widened_batch_still_replays_through_apply():
    """A3.12's actual acceptance: closure under apply, not just a shorter target.

    A clamp that produced a still-ambiguous target would satisfy every
    assertion above and still break the tool, because `apply` would reject the
    batch for ambiguity instead of for crossing a wall.
    """
    import io

    from adeu.redline.engine import RedlineEngine
    from tests.cc_fixture import cc_fixture_bytes

    eng = RedlineEngine(io.BytesIO(cc_fixture_bytes()), author="Widen Test")
    text = eng.mapper.full_text

    # "Deliverable: " occurs twice; widening has to disambiguate it.
    first = text.index("Deliverable: ")
    edit = pinned("Deliverable: ", "Item: ", first)
    surviving = make_edits_self_contained([edit], text)

    for e in surviving:
        e._match_start_index = None
        e._resolved_start_idx = None
    assert eng.validate_edits(surviving) == []
