"""Write-path gates for content controls (spec-gates.md, acceptance A3).

House doctrine, stated in spec-gates.md §7: **a silent no-op — or a silently
doomed op — is the most expensive bug an agent can consume.** Word will not
apply an edit inside a locked control, and a document-protected file rejects
whole classes of write. If Adeu passed those through, the agent would be told
its edit succeeded and the user would open a file that does not contain it.
Every gate here converts that silence into a refusal the caller can act on.

Every gate error carries the same FOUR components (spec-gates.md §2), because
an error that only says "no" makes the agent guess, and a guessing agent
retries the same edit:

1. which control, as ``CC:N`` plus its alias/tag when it has one;
2. the rule, stated as a fact about Word rather than about Adeu;
3. the sanctioned alternative — what to do instead;
4. the override parameter, when one exists.

A3 pins those four as substrings and explicitly does NOT pin the full
sentences, so wording can improve without breaking tests. `_gate_error` is the
single constructor, so a gate cannot accidentally ship three of the four.

The TypeScript twin is `node/packages/core/src/redline/gates.ts` and must stay
behaviourally identical: same gate order, same components, same override
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from adeu.utils.protection import UNPROTECTED, DocumentProtection

#: Override parameter names, as callers spell them. Named constants because
#: they appear in error text, in tool schemas and in the CLI, and a typo in
#: the error text sends the agent looking for a parameter that does not exist.
IGNORE_CONTROL_LOCKS = "ignore_control_locks"
IGNORE_DOCUMENT_PROTECTION = "ignore_document_protection"
ALLOW_UNTRACKED_WRITES = "allow_untracked_writes"


@dataclass(frozen=True)
class GateOverrides:
    """The three per-batch opt-outs, all default-off.

    Default `false` everywhere, including the MCP schemas: spec-gates.md §1
    requires it because a truthy default survives client stripping, and a
    gate that defaults to off is a gate that does not exist.
    """

    ignore_control_locks: bool = False
    ignore_document_protection: bool = False
    allow_untracked_writes: bool = False

    @property
    def any_used(self) -> bool:
        return self.ignore_control_locks or self.ignore_document_protection or self.allow_untracked_writes


#: The no-override default, shared because it is frozen.
NO_OVERRIDES = GateOverrides()


def describe_control(info: Any) -> str:
    """``CC:7 "Payment Terms" (tag: fixed_clause)`` — component 1 of the contract.

    Alias and tag are both optional in OOXML and frequently absent in
    real templates, so every combination has to render sensibly; a control
    with neither still names itself by ordinal, which is the identifier the
    projection actually shows the agent.
    """
    parts = [f"CC:{info.ordinal}"]
    alias = getattr(info, "alias", None)
    tag = getattr(info, "tag", None)
    if alias:
        parts.append(f'"{alias}"')
    if tag:
        parts.append(f"(tag: {tag})")
    return " ".join(parts)


def _gate_error(
    edit_number: int,
    *,
    subject: str,
    rule: str,
    alternative: str,
    override: Optional[str] = None,
) -> str:
    """Assemble one gate error with all four required components.

    The ``- Edit N Failed: `` prefix is load-bearing, not cosmetic:
    `_extract_failed_indices` parses it back into (index, reason) pairs so
    partial batches can report per-edit outcomes. Any gate error that loses
    the prefix silently gets attributed to edit 0.
    """
    text = f"- Edit {edit_number} Failed: {subject} {rule} {_end_sentence(alternative)}"
    if override:
        text += f" Pass {override}=true to override deliberately."
    return " ".join(text.split())


def _end_sentence(text: str) -> str:
    """Terminate ``text`` so the override sentence does not run into it.

    Without this the alternative and the override collide mid-line — "Remove
    the protection to write tracked changes normally Pass
    allow_untracked_writes=true to override deliberately." Two sentences, no
    boundary. These errors are written to be read by an agent under pressure
    and then quoted back to a user, so the seam matters more than it would in
    an internal log.
    """
    stripped = text.rstrip()
    return stripped if stripped.endswith((".", "!", "?", ":")) else stripped + "."


# --------------------------------------------------------------------------
# Lock gates: G1, G2, G3
# --------------------------------------------------------------------------


def _locked_ancestor(controls: Sequence[Any]) -> Optional[Any]:
    """The outermost content-locked control in an enclosure stack, if any.

    Outermost wins because G1 is phrased "control (or ancestor)": when a
    locked group wraps a locked leaf, the group is the fact the caller needs
    — unlocking the leaf alone would not help.
    """
    for info in controls:
        if getattr(info, "content_locked", False):
            return info
    return None


def check_content_lock(
    edit_number: int,
    controls: Sequence[Any],
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G1 — the edit range intersects a content-locked control.

    ``controls`` is the enclosure stack for the edit's range, outermost first
    (``DocumentMapper.controls_intersecting``).
    """
    if overrides.ignore_control_locks:
        return None
    info = _locked_ancestor(controls)
    if info is None:
        return None
    return _gate_error(
        edit_number,
        subject=f"edit targets content inside {describe_control(info)}, which is content-locked (sdtContentLocked).",
        rule="Word refuses edits inside locked controls, so this edit would silently fail to appear in the document.",
        alternative="Remove the lock in Word (Developer -> Properties)",
        override=IGNORE_CONTROL_LOCKS,
    )


def check_group_region(
    edit_number: int,
    controls: Sequence[Any],
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G3 — the edit targets group content outside any nested leaf control.

    A ``w:group`` is Word's "locked region" idiom: the boilerplate is fixed
    and the nested leaf controls are the intended fill points. So the test is
    not "is this inside a group" but "is this inside a group AND not inside
    one of its nested leaves" — A3.2 asserts both halves, and getting the
    second half wrong would make the group's own fields uneditable, which is
    the opposite of what the author built it for.
    """
    if overrides.ignore_control_locks:
        return None
    group = next((i for i in controls if getattr(i, "cls", None) == "group"), None)
    if group is None:
        return None
    # Any non-group control in the stack is a nested leaf, and the leaf is
    # exactly the sanctioned edit point.
    if any(getattr(i, "cls", None) != "group" for i in controls):
        return None
    return _gate_error(
        edit_number,
        subject=f"edit targets text inside {describe_control(group)}, which is a group (a locked region).",
        rule=(
            "Word treats a group's own text as fixed boilerplate and only permits edits to the fields nested inside it."
        ),
        alternative="Target one of the nested controls instead",
        override=IGNORE_CONTROL_LOCKS,
    )


def check_delete_lock(
    edit_number: int,
    controls: Sequence[Any],
    *,
    deletes_entire_control: bool,
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G2 — the edit would delete or unwrap a delete-locked control.

    Deliberately narrow. Deleting a delete-locked control's *contents* is
    allowed and leaves the wrapper standing with an empty pair (A3.3) —
    ``sdtLocked`` protects the control's existence, not its text. Only an edit
    that would dissolve the wrapper itself is refused.
    """
    if overrides.ignore_control_locks:
        return None
    if not deletes_entire_control:
        return None
    info = next((i for i in controls if getattr(i, "delete_locked", False)), None)
    if info is None:
        return None
    return _gate_error(
        edit_number,
        subject=f"edit would remove {describe_control(info)} itself, which is delete-locked (sdtLocked).",
        rule="Word refuses to delete a locked control, and hoisting its content out would dissolve the wrapper.",
        alternative="Delete the control's CONTENT instead (the wrapper stays, leaving an empty field)",
        override=IGNORE_CONTROL_LOCKS,
    )


# --------------------------------------------------------------------------
# Protection gates: G4, G5, G6, G7
# --------------------------------------------------------------------------


def check_protection_blocks_edit(
    edit_number: int,
    protection: DocumentProtection,
    *,
    controls: Sequence[Any] = (),
    is_comment_only: bool = False,
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G4 / G5 / G6 — document protection versus a text edit.

    G7 is absent by design: under ``trackedChanges`` protection text edits
    proceed, because Adeu always writes tracked changes and that is precisely
    what the protection permits. Review actions are the gated operation there
    — see `check_protection_blocks_review`.
    """
    if not protection.active:
        return None
    if overrides.ignore_document_protection:
        return None

    if protection.edit == "readOnly":
        return _gate_error(
            edit_number,
            subject=f"the document is protected ({protection.describe()}).",
            rule="Word blocks every modification while read-only protection is enforced.",
            alternative="Remove the restriction in Word (Review -> Restrict Editing -> Stop Protection)",
            override=IGNORE_DOCUMENT_PROTECTION,
        )

    if protection.edit == "comments":
        if is_comment_only:
            return None
        return _gate_error(
            edit_number,
            subject=f"the document is protected ({protection.describe()}).",
            rule="Word permits only comments while comments-only protection is enforced; text mutations are blocked.",
            alternative="Attach a comment instead (set new_text equal to target_text and supply comment)",
            override=IGNORE_DOCUMENT_PROTECTION,
        )

    if protection.edit == "forms":
        # G5: the form surface is what Word permits — content inside a leaf
        # control. Everything else in the body is locked boilerplate.
        inside_leaf = any(getattr(i, "cls", None) != "group" for i in controls)
        if not inside_leaf:
            return _gate_error(
                edit_number,
                subject=f"the document is protected ({protection.describe()}).",
                rule=(
                    "Word permits only form-field fills while fill-in-forms protection is enforced; "
                    "body text outside a content control is locked."
                ),
                alternative="Target the content of a form field instead",
                override=IGNORE_DOCUMENT_PROTECTION,
            )
        # Permitted by Word — but see check_untracked_write.
        return None

    return None


def check_protection_blocks_review(
    action_number: int,
    action_type: str,
    protection: DocumentProtection,
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G7 / G4 — review actions versus document protection.

    ``trackedChanges`` protection exists precisely to stop revisions being
    resolved, so Accept and Reject are refused while ordinary tracked editing
    continues. CC-6 measured this against Word 16.0: both fail with "This
    command is not available", document-wide.

    Note what is NOT here: G9. Word permits Accept and Reject *inside*
    content-locked controls — the lock stops typing, not review — so locks do
    not gate review at all (CC-6(d) downgraded G9 to allow). Rejecting there
    would make Adeu stricter than Word for no protective benefit and strand
    revisions the user can resolve in two clicks.
    """
    if not protection.active:
        return None
    if overrides.ignore_document_protection:
        return None
    if action_type not in ("accept", "reject"):
        return None
    if protection.edit not in ("trackedChanges", "readOnly"):
        return None

    rule = (
        "Word blocks every modification while read-only protection is enforced."
        if protection.edit == "readOnly"
        else "Resolving revisions is exactly what this protection forbids, so Word refuses Accept and Reject."
    )
    return (
        f"- Action {action_number} Failed: the document is protected "
        f"({protection.describe()}). {rule} "
        "Remove the restriction in Word (Review -> Restrict Editing -> Stop Protection) "
        f"or pass {IGNORE_DOCUMENT_PROTECTION}=true to override deliberately."
    )


def check_untracked_write(
    edit_number: int,
    protection: DocumentProtection,
    overrides: GateOverrides = NO_OVERRIDES,
) -> Optional[str]:
    """G5's tracking half (spec-gates.md §1a), resolved by Mikko 2026-08-21.

    Under ``forms`` protection Word records the permitted fills UNTRACKED, and
    reading ``Document.TrackRevisions`` throws outright (CC-6). So Adeu's
    standing "always writes tracked changes" contract is not merely
    inconvenient here, it is unenforceable — and it cannot even detect that it
    has been broken.

    Writing anyway with only a report note was rejected as the worst option: a
    guarantee that quietly weakens under a condition the caller cannot detect
    is more dangerous than no guarantee, because callers automate against the
    guarantee. Hence refuse by default, with an explicit opt-in.

    This is deliberately a SEPARATE parameter from
    ``ignore_document_protection``. That one bypasses a gate the author set;
    this one accepts a downgrade in Adeu's own output guarantee. The writes in
    question are ones Word itself permits, so no protection is being ignored.
    """
    if not protection.active or protection.edit != "forms":
        return None
    if overrides.allow_untracked_writes:
        return None
    return _gate_error(
        edit_number,
        subject=f"the document is protected ({protection.describe()}).",
        rule=(
            "Word records fills in a forms-protected document as UNTRACKED changes, so Adeu "
            "cannot honour its guarantee that every write is a tracked change."
        ),
        alternative="Remove the protection to write tracked changes normally",
        override=ALLOW_UNTRACKED_WRITES,
    )


UNTRACKED_WRITE_NOTE = (
    "written UNTRACKED: the document is forms-protected, where Word does not record revisions "
    f"({ALLOW_UNTRACKED_WRITES}=true was passed)"
)


# --------------------------------------------------------------------------
# Content-shape gates: G8, G11, G13
# --------------------------------------------------------------------------


def check_placeholder_target(
    edit_number: int,
    target_text: str,
    infos: Sequence[Any],
) -> Optional[str]:
    """G8 — the target is an empty control's placeholder ghost text.

    Not a span-intersection gate like its siblings, and that is forced rather
    than chosen: an empty control projects its ghost as a virtual
    ``{>>placeholder: …<<}`` bubble, so it has no content spans to intersect
    (pinned in test_cc_span_control_identity). The match is therefore against
    the placeholder text itself.

    Worth gating loudly because the failure is invisible: ghost runs are not
    content, so an edit "succeeds", Word discards the run the moment the field
    is touched, and the text the agent wrote is gone with no error anywhere.
    No override — this is not a lock the caller can reasonably insist past,
    it is a category error about what the text IS.
    """
    needle = (target_text or "").strip()
    if not needle:
        return None
    for info in infos:
        if not getattr(info, "showing_placeholder", False):
            continue
        ghost = (getattr(info, "placeholder_text", None) or "").strip()
        if not ghost or needle not in ghost:
            continue
        return _gate_error(
            edit_number,
            subject=f"target_text is the placeholder text of {describe_control(info)}, which is EMPTY.",
            rule=(
                "Placeholder text is a ghost prompt, not content: Word discards it as soon as the "
                "field is filled, so an edit to it would be silently lost."
            ),
            alternative=(
                f"Fill the field with set_field, or insert at its empty pair "
                f"{{#cc:{info.ordinal}}}{{#/cc:{info.ordinal}}}"
            ),
        )
    return None


#: The only two texts a checkbox control's projection may be edited to or from.
CHECKBOX_STATES = ("[ ]", "[x]")


def check_checkbox_edit(
    edit_number: int,
    controls: Sequence[Any],
    target_text: str,
    new_text: str,
) -> Optional[str]:
    """G11 — a checkbox may be toggled, not rewritten.

    ``[x]`` is a projection of ``w14:checked`` plus a glyph run, not text.
    Replacing it with prose would write the prose into the control's content
    while leaving ``w14:checked`` untouched, so the checkbox would render with
    its old state and stray text beside it.
    """
    info = next((i for i in controls if getattr(i, "cls", None) == "checkbox"), None)
    if info is None:
        return None
    if target_text.strip() in CHECKBOX_STATES and new_text.strip() in CHECKBOX_STATES:
        return None
    return _gate_error(
        edit_number,
        subject=f"edit targets {describe_control(info)}, which is a checkbox.",
        rule=(
            "Its [x] / [ ] projection reflects the w14:checked state rather than editable text, "
            "so replacing it with other text would leave the checkbox state unchanged."
        ),
        alternative="Toggle it by replacing [x] with [ ] (or the reverse), or use set_field",
    )


def check_bound_control(
    edit_number: int,
    controls: Sequence[Any],
) -> Optional[str]:
    """G13 — text edits may not touch data-bound content.

    A bound control's text is a projection of an XML store item. Word
    re-reads the store on open, so a text edit that skips the store is
    reverted — the change survives the save and vanishes for the user.

    No override, and that asymmetry is deliberate: the other gates refuse
    something Word would refuse, so overriding them just accepts Word's
    verdict. Here the write would appear to succeed and then silently revert,
    and no parameter can make the text path keep the store consistent. The
    capable path is `set_field`, which dual-writes (spec-set-field §6).
    """
    info = next((i for i in controls if getattr(i, "bound", False)), None)
    if info is None:
        return None
    xpath = getattr(info, "binding_xpath", None)
    where = f" (bound to {xpath})" if xpath else ""
    return _gate_error(
        edit_number,
        subject=f"edit targets the content of {describe_control(info)}, which is data-bound{where}.",
        rule=(
            "Its text is a projection of an XML data store, and Word re-reads that store when the "
            "document opens, so an edit that skips the store is reverted."
        ),
        alternative="Use set_field, which writes the control and its bound store together",
    )


# --------------------------------------------------------------------------
# Structural gates: G14, G15
# --------------------------------------------------------------------------


def crossed_control_walls(
    controls_in_range: Sequence[Any],
    controls_at_start: Sequence[Any],
    controls_at_end: Sequence[Any],
) -> List[Any]:
    """Controls the range only PARTLY covers — the walls G14/G15 care about.

    A control fully inside the range, or fully containing it, is not a wall
    crossing: the first is ordinary content being replaced, the second is an
    ordinary edit within a control. Only a control that some of the range is
    inside and some outside has a wall running through the target.
    """
    start_ids = {id(i) for i in controls_at_start}
    end_ids = {id(i) for i in controls_at_end}
    crossed = []
    for info in controls_in_range:
        inside_start = id(info) in start_ids
        inside_end = id(info) in end_ids
        if inside_start != inside_end:
            crossed.append(info)
    return crossed


def check_block_merge_across_control(
    edit_number: int,
    crossed: Sequence[Any],
) -> Optional[str]:
    """G15 — a paragraph merge may not hoist content across a control wrapper.

    The Double-Sided Merge Refusal class, extended from paragraph walls to
    control walls. Merging a paragraph inside a control with one outside it
    has to move content across the wrapper in one direction or the other;
    either way a control gains or loses text its author never scoped to it,
    and no tracked-change representation of that is honest.
    """
    if not crossed:
        return None
    names = ", ".join(describe_control(i) for i in crossed)
    return _gate_error(
        edit_number,
        subject=f"edit merges paragraphs across the boundary of {names}.",
        rule=(
            "Content may not be hoisted into or out of a content control by a merge: the wrapper "
            "would gain or lose text that was never scoped to it."
        ),
        alternative="Split this into two edits, one on each side of the control boundary",
    )


def segmentation_note(controls: Sequence[Any]) -> str:
    """G14's per-edit report note (spec-gates.md §5)."""
    names = ", ".join(describe_control(i) for i in controls)
    return f"segmented at the boundary of {names}: only the text outside the control was changed"


# --------------------------------------------------------------------------
# Report disclosure (spec-gates.md §5)
# --------------------------------------------------------------------------


def overrides_note(overrides: GateOverrides, controls_touched: Sequence[Any]) -> Optional[str]:
    """``Overrides: ignore_control_locks (CC:7, CC:12 edited inside locked controls)``.

    Disclosure is the other half of the override bargain: the caller opted
    out of a safety rail, and the report says so where a human reviewing the
    batch will see it.
    """
    if not overrides.any_used:
        return None
    used = []
    if overrides.ignore_control_locks:
        detail = ""
        if controls_touched:
            names = ", ".join(describe_control(i) for i in controls_touched)
            detail = f" ({names} edited inside locked controls)"
        used.append(f"{IGNORE_CONTROL_LOCKS}{detail}")
    if overrides.ignore_document_protection:
        used.append(IGNORE_DOCUMENT_PROTECTION)
    if overrides.allow_untracked_writes:
        used.append(ALLOW_UNTRACKED_WRITES)
    return "Overrides: " + ", ".join(used)


__all__ = [
    "ALLOW_UNTRACKED_WRITES",
    "CHECKBOX_STATES",
    "IGNORE_CONTROL_LOCKS",
    "IGNORE_DOCUMENT_PROTECTION",
    "NO_OVERRIDES",
    "UNPROTECTED",
    "UNTRACKED_WRITE_NOTE",
    "DocumentProtection",
    "GateOverrides",
    "check_block_merge_across_control",
    "check_bound_control",
    "check_checkbox_edit",
    "check_content_lock",
    "check_delete_lock",
    "check_group_region",
    "check_placeholder_target",
    "check_protection_blocks_edit",
    "check_protection_blocks_review",
    "check_untracked_write",
    "crossed_control_walls",
    "describe_control",
    "overrides_note",
    "segmentation_note",
]
