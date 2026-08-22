"""CC-4 acceptance: the write-path gate matrix (A3, spec-gates.md §2).

Error assertions pin the FOUR components the contract requires — the CC:N
reference, the rule, the sanctioned alternative, and the override parameter —
and deliberately NOT the full sentences. A3 says so explicitly: pinning
canonical prose would make every wording improvement a test failure, and the
components are what the agent actually consumes.

Each test names its acceptance example. Where behaviour here differs from the
A3 text as originally frozen, the reason is spelled out inline rather than
quietly encoded — see `test_a3_5_*`.
"""

import io

import pytest

from adeu.models import AcceptChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine
from tests.cc_fixture import cc_fixture_bytes


def engine(protection: str | None = None, **overrides) -> RedlineEngine:
    return RedlineEngine(io.BytesIO(cc_fixture_bytes(protection=protection)), author="Gate Test", **overrides)


def errors_for(eng: RedlineEngine, edit: ModifyText) -> str:
    return "\n".join(eng.validate_edits([edit]))


def modify(target: str, new: str, **kw) -> ModifyText:
    return ModifyText(type="modify", target_text=target, new_text=new, **kw)


def document_xml(eng: RedlineEngine) -> str:
    """word/document.xml of the saved package (the .docx is a ZIP)."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(eng.save_to_stream().getvalue())) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def assert_four_components(err: str, *, ref: str, rule: str, alternative: str, override: str | None):
    """The error contract, spec-gates §2. All four, or the agent has to guess."""
    assert ref in err, f"missing control reference {ref!r} in: {err}"
    assert rule.lower() in err.lower(), f"missing rule {rule!r} in: {err}"
    assert alternative.lower() in err.lower(), f"missing alternative {alternative!r} in: {err}"
    if override:
        assert override in err, f"missing override param {override!r} in: {err}"
    else:
        # A gate with no override must not invent one: naming a parameter that
        # does not exist sends the agent chasing a flag it cannot pass.
        assert "ignore_control_locks" not in err
        assert "ignore_document_protection" not in err


# --------------------------------------------------------------------------
# A3.1 — content-locked control refuses edits (G1)
# --------------------------------------------------------------------------


def test_a3_1_content_locked_control_refuses_edits():
    err = errors_for(engine(), modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days."))
    assert_four_components(
        err,
        ref="CC:7",
        rule="content-locked",
        alternative="remove the lock",
        override="ignore_control_locks",
    )
    assert '"Payment Terms"' in err
    assert "fixed_clause" in err


def test_a3_1_override_lets_the_edit_through():
    eng = engine(ignore_control_locks=True)
    assert errors_for(eng, modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days.")) == ""


def test_a3_1_override_is_disclosed_in_the_report():
    # The other half of the override bargain: the caller opted out of a safety
    # rail and the report says so where a human reviewing the batch sees it.
    eng = engine(ignore_control_locks=True)
    stats = eng.process_batch([modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days.")])
    note = stats.get("overrides_note") or ""
    assert "ignore_control_locks" in note
    assert "CC:7" in note


# --------------------------------------------------------------------------
# A3.2 — group refuses non-field edits, permits nested-field edits (G3)
# --------------------------------------------------------------------------


def test_a3_2a_group_region_refuses_boilerplate_edits():
    err = errors_for(engine(), modify("must not be modified", "may be modified"))
    assert_four_components(
        err,
        ref="CC:8",
        rule="group",
        alternative="nested",
        override="ignore_control_locks",
    )
    assert '"Standard Terms"' in err


def test_a3_2b_nested_field_inside_a_group_stays_editable():
    # The half that is easy to get wrong: over-broad group gating would make
    # the group's own fields uneditable, which is the opposite of what the
    # author built the group for.
    assert errors_for(engine(), modify("123 Main Street, Ottawa", "1 King Street, Toronto")) == ""


# --------------------------------------------------------------------------
# A3.3 — delete-locked wrapper survives (G2)
# --------------------------------------------------------------------------


def test_a3_3_emptying_a_control_is_allowed():
    # sdtLocked protects the control's EXISTENCE, not its text.
    assert errors_for(engine(), modify("123 Main Street, Ottawa", "")) == ""


def test_a3_3_wrapper_survives_the_deletion_in_the_xml():
    eng = engine()
    eng.process_batch([modify("123 Main Street, Ottawa", "")])
    xml = document_xml(eng)
    # The wrapper survives as an element even though its content is now a
    # tracked deletion — that is the whole point of G2's narrowness.
    assert 'w:val="sdtLocked"' in xml
    assert "notice_address" in xml


# --------------------------------------------------------------------------
# A3.4 — readOnly protection blocks everything (G4)
# --------------------------------------------------------------------------


def test_a3_4_readonly_blocks_every_edit():
    err = errors_for(engine(protection="readOnly"), modify("Signed by the parties below.", "Signed below."))
    assert_four_components(
        err,
        ref="read-only",
        rule="blocks every modification",
        alternative="restrict editing",
        override="ignore_document_protection",
    )
    assert "enforced" in err


def test_a3_4_readonly_blocks_edits_inside_controls_too():
    # Protection binds regardless of where the edit lands — it is checked
    # before anything about the control.
    err = errors_for(engine(protection="readOnly"), modify("ACME Corp", "Globex"))
    assert "read-only" in err


def test_a3_4_override_lets_the_edit_through():
    eng = engine(protection="readOnly", ignore_document_protection=True)
    assert errors_for(eng, modify("Signed by the parties below.", "Signed below.")) == ""


# --------------------------------------------------------------------------
# A3.5 — forms protection allows exactly the form surface (G5)
# --------------------------------------------------------------------------


def test_a3_5a_forms_protection_refuses_body_text_outside_controls():
    err = errors_for(engine(protection="forms"), modify("approved boilerplate", "revised boilerplate"))
    assert_four_components(
        err,
        ref="fill-in-forms",
        rule="body text outside a content control is locked",
        alternative="form field",
        override="ignore_document_protection",
    )


def test_a3_5c_forms_protection_refuses_even_permitted_fills_by_default():
    """A3.5 as frozen says this edit applies; spec-gates §1a supersedes that.

    Mikko's 2026-08-21 decision (spec-gates §1a) added a SECOND gate on the
    writes Word permits here: under `forms` protection Word records them
    untracked and reading TrackRevisions throws, so Adeu's "always tracked"
    contract is unenforceable. Refuse by default, opt in explicitly.

    A3.5's "(b) and (c) apply" predates that decision and was not restated
    when §1a landed. The decision is the newer and more specific statement, and
    §1a is unambiguous that the permitted writes are "additionally gated", so
    it wins. Flagged in PROGRESS.md and A3.5 updated to match.
    """
    err = errors_for(engine(protection="forms"), modify("ACME Corp", "Globex"))
    assert_four_components(
        err,
        ref="fill-in-forms",
        rule="untracked",
        alternative="remove the protection",
        override="allow_untracked_writes",
    )


def test_a3_5c_forms_fill_applies_with_the_opt_in():
    eng = engine(protection="forms", allow_untracked_writes=True)
    assert errors_for(eng, modify("ACME Corp", "Globex")) == ""


def test_a3_5_the_two_override_params_are_not_interchangeable():
    # §1a is explicit that these are different admissions: one bypasses a gate
    # the author set, the other accepts a downgrade of Adeu's own guarantee.
    # Neither should unlock the other.
    protection_only = engine(protection="forms", ignore_document_protection=True)
    assert "untracked" in errors_for(protection_only, modify("ACME Corp", "Globex"))

    tracking_only = engine(protection="forms", allow_untracked_writes=True)
    assert "fill-in-forms" in errors_for(tracking_only, modify("approved boilerplate", "revised"))


# --------------------------------------------------------------------------
# A3.6 — trackedChanges protection blocks review actions only (G7)
# --------------------------------------------------------------------------


def test_a3_6a_tracked_changes_protection_permits_text_edits():
    # Adeu always writes tracked changes, which is exactly what this
    # protection permits.
    assert errors_for(engine(protection="trackedChanges"), modify("ACME Corp", "Globex")) == ""


def test_a3_6b_tracked_changes_protection_refuses_accept():
    eng = engine(protection="trackedChanges")
    with pytest.raises(BatchValidationError) as excinfo:
        eng.process_batch([AcceptChange(type="accept", target_id="Chg:1")])
    err = "\n".join(excinfo.value.errors)
    assert "tracked-changes-only" in err
    assert "resolving revisions" in err.lower()
    assert "ignore_document_protection" in err


def test_a3_6_locks_do_not_gate_review_g9_is_allow():
    # CC-6(d) measured Word permitting Accept/Reject inside sdtContentLocked:
    # the lock stops typing, not review. Gating it would make Adeu stricter
    # than Word and strand revisions the user can resolve in two clicks.
    eng = engine()
    with pytest.raises(BatchValidationError) as excinfo:
        eng.process_batch([AcceptChange(type="accept", target_id="Chg:404")])
    # Fails because the id does not exist, NOT because of any lock.
    err = "\n".join(excinfo.value.errors)
    assert "lock" not in err.lower()


# --------------------------------------------------------------------------
# A3.7 — placeholder ghosts are not editable text (G8)
# --------------------------------------------------------------------------


def test_a3_7_placeholder_ghost_is_not_editable():
    err = errors_for(engine(), modify("Click or tap here to enter text.", "Ministry of Example"))
    assert_four_components(
        err,
        ref="CC:2",
        rule="placeholder",
        alternative="set_field",
        override=None,
    )
    assert '"Client Name"' in err
    # BOTH sanctioned fills, per A3.7.
    assert "{#cc:2}{#/cc:2}" in err


def test_a3_7_the_xml_is_untouched():
    # A3.7 pins the XML, not just the refusal: a gate that rejected but had
    # already mutated the ghost run would leave the document worse than if it
    # had done nothing.
    eng = engine()
    with pytest.raises(BatchValidationError):
        eng.process_batch([modify("Click or tap here to enter text.", "Ministry of Example")])
    xml = document_xml(eng)
    assert "showingPlcHdr" in xml
    assert "Click or tap here to enter text." in xml
    assert "Ministry of Example" not in xml


# --------------------------------------------------------------------------
# A3.8 — checkbox tokens accept only the toggle (G11)
# --------------------------------------------------------------------------


def test_a3_8b_checkbox_refuses_arbitrary_text():
    err = errors_for(engine(), modify("[x]", "yes"))
    assert_four_components(
        err,
        ref="CC:6",
        rule="checkbox",
        alternative="set_field",
        override=None,
    )
    assert "[ ]" in err


def test_a3_8a_checkbox_toggle_is_permitted():
    assert errors_for(engine(), modify("[x]", "[ ]")) == ""


# --------------------------------------------------------------------------
# A3.9 — bound content redirects to set_field (G13)
# --------------------------------------------------------------------------


def test_a3_9_bound_control_redirects_to_set_field():
    err = errors_for(engine(), modify("M-2026-001", "M-2026-002"))
    assert_four_components(
        err,
        ref="CC:10",
        rule="data-bound",
        alternative="set_field",
        override=None,
    )
    assert '"Matter Number"' in err
    assert "/root[1]/matter[1]" in err


# --------------------------------------------------------------------------
# A3.10 — boundary auto-segmentation (G14)
# --------------------------------------------------------------------------


def test_a3_10_an_edit_whose_change_stays_outside_the_control_applies():
    # The changed word is outside CC:3; the unchanged tail crosses into it.
    # trim_common_context narrows the effective range to "Counterparty:"
    # before any gate sees it, so the control is never touched. This is the
    # segmentation A3.10 asks for, already performed by machinery that
    # predates the gates.
    eng = engine()
    edit = modify("Counterparty: {#cc:3}ACME Corp", "Supplier: {#cc:3}ACME Corp")
    assert errors_for(eng, edit) == ""


def test_a3_10_a_genuine_crossing_applies_and_is_disclosed():
    # Here BOTH sides change, so the effective range really does span the
    # wall. Neither side is locked, so the edit is valid and applies — but
    # the report must say it touched text on both sides of the control,
    # because an agent that asked to change "CC:3" and silently got a change
    # half outside it has been told something untrue by omission.
    eng = engine()
    stats = eng.process_batch([modify("Counterparty: {#cc:3}ACME Corp", "Supplier: {#cc:3}GLOBEX Inc")])
    assert stats["edits_applied"] == 1
    warning = (stats["edits"][0].get("warning") or "").lower()
    assert "segmented" in warning
    assert "cc:3" in warning


# --------------------------------------------------------------------------
# A3.11 — no merges across block-control walls (G15)
# --------------------------------------------------------------------------


def test_a3_11_merge_out_of_an_anchored_block_control_is_refused():
    # A3.11's own example is caught one layer earlier, by CC-1e's anchor
    # gate: the merge would have to delete {#/cc:1}. That is a MORE precise
    # error than a generic merge refusal, so it is the right one to keep —
    # but it means A3.11 does not, by itself, exercise G15.
    err = errors_for(
        engine(),
        modify(
            "third-party claims.\n{#/cc:1}\n\nThis Agreement is made between",
            "third-party claims and this Agreement is made between",
        ),
    )
    assert "anchor" in err.lower()
    assert "CC:1" in err or "{#cc:N}" in err


def test_a3_11_merge_across_an_UNANCHORED_control_wall_is_refused():
    # G15's real job. CC:12 and CC:13 are repeating-section items: they are
    # UNANCHORED, so they project no tokens and the anchor gate cannot see
    # them. Without G15 a merge would silently hoist one item's content into
    # the other, and the repeating section would lose an item.
    err = errors_for(
        engine(),
        modify(
            "Initial report, due 2026-02-01.\n\nDeliverable: Final report",
            "Initial report and the final report",
        ),
    )
    assert "CC:12" in err and "CC:13" in err
    assert "merge" in err.lower() or "hoisted" in err.lower()
    assert "two edits" in err.lower() or "split" in err.lower()


def test_a3_9_bound_gate_has_no_override():
    # Unlike the lock gates, no parameter unlocks this: the other gates refuse
    # what Word would refuse, so overriding accepts Word's verdict. Here the
    # write would appear to succeed and then silently revert on open, and no
    # flag can make the text path keep the store consistent.
    eng = engine(ignore_control_locks=True, ignore_document_protection=True, allow_untracked_writes=True)
    assert "data-bound" in errors_for(eng, modify("M-2026-001", "M-2026-002"))
