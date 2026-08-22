// FILE: node/packages/core/src/gates.ts
/**
 * Write-path gates for content controls (spec-gates.md, acceptance A3).
 *
 * House doctrine, stated in spec-gates.md §7: **a silent no-op — or a
 * silently doomed op — is the most expensive bug an agent can consume.** Word
 * will not apply an edit inside a locked control, and a document-protected
 * file rejects whole classes of write. If Adeu passed those through, the agent
 * would be told its edit succeeded and the user would open a file that does
 * not contain it. Every gate here converts that silence into a refusal the
 * caller can act on.
 *
 * Every gate error carries the same FOUR components (spec-gates.md §2),
 * because an error that only says "no" makes the agent guess, and a guessing
 * agent retries the same edit:
 *
 *   1. which control, as `CC:N` plus its alias/tag when it has one;
 *   2. the rule, stated as a fact about Word rather than about Adeu;
 *   3. the sanctioned alternative — what to do instead;
 *   4. the override parameter, when one exists.
 *
 * A3 pins those four as substrings and explicitly does NOT pin the full
 * sentences, so wording can improve without breaking tests. `gateError` is the
 * single constructor, so a gate cannot accidentally ship three of the four.
 *
 * The Python twin is `python/src/adeu/redline/gates.py` and must stay
 * behaviourally identical: same gate order, same components, same override
 * semantics.
 */

import type { SdtInfo } from "./utils/content-controls.js";
import type { DocumentProtection } from "./utils/protection.js";
import { describeProtection, isProtectionActive } from "./utils/protection.js";

/**
 * Override parameter names, as callers spell them. Named constants because
 * they appear in error text, in tool schemas and in the CLI, and a typo in the
 * error text sends the agent looking for a parameter that does not exist.
 */
export const IGNORE_CONTROL_LOCKS = "ignore_control_locks";
export const IGNORE_DOCUMENT_PROTECTION = "ignore_document_protection";
export const ALLOW_UNTRACKED_WRITES = "allow_untracked_writes";

/**
 * The three per-batch opt-outs, all default-off.
 *
 * Default `false` everywhere, including the MCP schemas: spec-gates.md §1
 * requires it because a truthy default survives client stripping, and a gate
 * that defaults to off is a gate that does not exist.
 */
export interface GateOverrides {
  ignore_control_locks: boolean;
  ignore_document_protection: boolean;
  allow_untracked_writes: boolean;
}

/** The no-override default. Frozen, so it is safe to share. */
export const NO_OVERRIDES: GateOverrides = Object.freeze({
  ignore_control_locks: false,
  ignore_document_protection: false,
  allow_untracked_writes: false,
});

export function anyOverrideUsed(o: GateOverrides): boolean {
  return (
    o.ignore_control_locks ||
    o.ignore_document_protection ||
    o.allow_untracked_writes
  );
}

/**
 * `CC:7 "Payment Terms" (tag: fixed_clause)` — component 1 of the contract.
 *
 * Alias and tag are both optional in OOXML and frequently absent in real
 * templates, so every combination has to render sensibly; a control with
 * neither still names itself by ordinal, which is the identifier the
 * projection actually shows the agent.
 */
export function describeControl(info: SdtInfo): string {
  const parts = [`CC:${info.ordinal}`];
  if (info.alias) parts.push(`"${info.alias}"`);
  if (info.tag) parts.push(`(tag: ${info.tag})`);
  return parts.join(" ");
}

/**
 * Assemble one gate error with all four required components.
 *
 * The `- Edit N Failed: ` prefix is load-bearing, not cosmetic:
 * `extract_failed_indices` parses it back into (index, reason) pairs so
 * partial batches can report per-edit outcomes. Any gate error that loses the
 * prefix silently gets attributed to edit 0.
 */
function gateError(
  editNumber: number,
  opts: {
    subject: string;
    rule: string;
    alternative: string;
    override?: string;
  },
): string {
  let text = `- Edit ${editNumber} Failed: ${opts.subject} ${opts.rule} ${endSentence(opts.alternative)}`;
  if (opts.override) {
    text += ` Pass ${opts.override}=true to override deliberately.`;
  }
  return text.split(/\s+/).join(" ");
}

/**
 * Terminate `text` so the override sentence does not run into it.
 *
 * Without this the alternative and the override collide mid-line — "Remove the
 * protection to write tracked changes normally Pass allow_untracked_writes=true
 * to override deliberately." Two sentences, no boundary. These errors are
 * written to be read by an agent under pressure and then quoted back to a user,
 * so the seam matters more than it would in an internal log.
 */
function endSentence(text: string): string {
  const stripped = text.replace(/\s+$/, "");
  return /[.!?:]$/.test(stripped) ? stripped : `${stripped}.`;
}

// ---------------------------------------------------------------------------
// Lock gates: G1, G2, G3
// ---------------------------------------------------------------------------

/**
 * The outermost content-locked control in an enclosure stack, if any.
 *
 * Outermost wins because G1 is phrased "control (or ancestor)": when a locked
 * group wraps a locked leaf, the group is the fact the caller needs —
 * unlocking the leaf alone would not help.
 */
function lockedAncestor(controls: readonly SdtInfo[]): SdtInfo | null {
  return controls.find((i) => i.contentLocked) ?? null;
}

/** G1 — the edit range intersects a content-locked control. */
export function checkContentLock(
  editNumber: number,
  controls: readonly SdtInfo[],
  overrides: GateOverrides = NO_OVERRIDES,
): string | null {
  if (overrides.ignore_control_locks) return null;
  const info = lockedAncestor(controls);
  if (!info) return null;
  return gateError(editNumber, {
    subject: `edit targets content inside ${describeControl(info)}, which is content-locked (sdtContentLocked).`,
    rule: "Word refuses edits inside locked controls, so this edit would silently fail to appear in the document.",
    alternative: "Remove the lock in Word (Developer -> Properties)",
    override: IGNORE_CONTROL_LOCKS,
  });
}

/**
 * G3 — the edit targets group content outside any nested leaf control.
 *
 * A `w:group` is Word's "locked region" idiom: the boilerplate is fixed and
 * the nested leaf controls are the intended fill points. So the test is not
 * "is this inside a group" but "is this inside a group AND not inside one of
 * its nested leaves" — A3.2 asserts both halves, and getting the second half
 * wrong would make the group's own fields uneditable, which is the opposite
 * of what the author built it for.
 */
export function checkGroupRegion(
  editNumber: number,
  controls: readonly SdtInfo[],
  overrides: GateOverrides = NO_OVERRIDES,
): string | null {
  if (overrides.ignore_control_locks) return null;
  const group = controls.find((i) => i.cls === "group");
  if (!group) return null;
  // Any non-group control in the stack is a nested leaf, and the leaf is
  // exactly the sanctioned edit point.
  if (controls.some((i) => i.cls !== "group")) return null;
  return gateError(editNumber, {
    subject: `edit targets text inside ${describeControl(group)}, which is a group (a locked region).`,
    rule:
      "Word treats a group's own text as fixed boilerplate and only permits edits " +
      "to the fields nested inside it.",
    alternative: "Target one of the nested controls instead",
    override: IGNORE_CONTROL_LOCKS,
  });
}

/**
 * G2 — the edit would delete or unwrap a delete-locked control.
 *
 * Deliberately narrow. Deleting a delete-locked control's *contents* is
 * allowed and leaves the wrapper standing with an empty pair (A3.3) —
 * `sdtLocked` protects the control's existence, not its text. Only an edit
 * that would dissolve the wrapper itself is refused.
 */
export function checkDeleteLock(
  editNumber: number,
  controls: readonly SdtInfo[],
  deletesEntireControl: boolean,
  overrides: GateOverrides = NO_OVERRIDES,
): string | null {
  if (overrides.ignore_control_locks) return null;
  if (!deletesEntireControl) return null;
  const info = controls.find((i) => i.deleteLocked);
  if (!info) return null;
  return gateError(editNumber, {
    subject: `edit would remove ${describeControl(info)} itself, which is delete-locked (sdtLocked).`,
    rule: "Word refuses to delete a locked control, and hoisting its content out would dissolve the wrapper.",
    alternative:
      "Delete the control's CONTENT instead (the wrapper stays, leaving an empty field)",
    override: IGNORE_CONTROL_LOCKS,
  });
}

// ---------------------------------------------------------------------------
// Protection gates: G4, G5, G6, G7
// ---------------------------------------------------------------------------

/**
 * G4 / G5 / G6 — document protection versus a text edit.
 *
 * G7 is absent by design: under `trackedChanges` protection text edits
 * proceed, because Adeu always writes tracked changes and that is precisely
 * what the protection permits. Review actions are the gated operation there —
 * see `checkProtectionBlocksReview`.
 */
export function checkProtectionBlocksEdit(
  editNumber: number,
  protection: DocumentProtection,
  opts: {
    controls?: readonly SdtInfo[];
    isCommentOnly?: boolean;
    overrides?: GateOverrides;
  } = {},
): string | null {
  const overrides = opts.overrides ?? NO_OVERRIDES;
  if (!isProtectionActive(protection)) return null;
  if (overrides.ignore_document_protection) return null;

  const described = describeProtection(protection);

  if (protection.edit === "readOnly") {
    return gateError(editNumber, {
      subject: `the document is protected (${described}).`,
      rule: "Word blocks every modification while read-only protection is enforced.",
      alternative:
        "Remove the restriction in Word (Review -> Restrict Editing -> Stop Protection)",
      override: IGNORE_DOCUMENT_PROTECTION,
    });
  }

  if (protection.edit === "comments") {
    if (opts.isCommentOnly) return null;
    return gateError(editNumber, {
      subject: `the document is protected (${described}).`,
      rule: "Word permits only comments while comments-only protection is enforced; text mutations are blocked.",
      alternative:
        "Attach a comment instead (set new_text equal to target_text and supply comment)",
      override: IGNORE_DOCUMENT_PROTECTION,
    });
  }

  if (protection.edit === "forms") {
    // G5: the form surface is what Word permits — content inside a leaf
    // control. Everything else in the body is locked boilerplate.
    const insideLeaf = (opts.controls ?? []).some((i) => i.cls !== "group");
    if (!insideLeaf) {
      return gateError(editNumber, {
        subject: `the document is protected (${described}).`,
        rule:
          "Word permits only form-field fills while fill-in-forms protection is enforced; " +
          "body text outside a content control is locked.",
        alternative: "Target the content of a form field instead",
        override: IGNORE_DOCUMENT_PROTECTION,
      });
    }
    // Permitted by Word — but see checkUntrackedWrite.
    return null;
  }

  return null;
}

/**
 * G7 / G4 — review actions versus document protection.
 *
 * `trackedChanges` protection exists precisely to stop revisions being
 * resolved, so Accept and Reject are refused while ordinary tracked editing
 * continues. CC-6 measured this against Word 16.0: both fail with "This
 * command is not available", document-wide.
 *
 * Note what is NOT here: G9. Word permits Accept and Reject *inside*
 * content-locked controls — the lock stops typing, not review — so locks do
 * not gate review at all (CC-6(d) downgraded G9 to allow). Rejecting there
 * would make Adeu stricter than Word for no protective benefit and strand
 * revisions the user can resolve in two clicks.
 */
export function checkProtectionBlocksReview(
  actionNumber: number,
  actionType: string,
  protection: DocumentProtection,
  overrides: GateOverrides = NO_OVERRIDES,
): string | null {
  if (!isProtectionActive(protection)) return null;
  if (overrides.ignore_document_protection) return null;
  if (actionType !== "accept" && actionType !== "reject") return null;
  if (protection.edit !== "trackedChanges" && protection.edit !== "readOnly") {
    return null;
  }
  const rule =
    protection.edit === "readOnly"
      ? "Word blocks every modification while read-only protection is enforced."
      : "Resolving revisions is exactly what this protection forbids, so Word refuses Accept and Reject.";
  return (
    `- Action ${actionNumber} Failed: the document is protected ` +
    `(${describeProtection(protection)}). ${rule} ` +
    "Remove the restriction in Word (Review -> Restrict Editing -> Stop Protection) " +
    `or pass ${IGNORE_DOCUMENT_PROTECTION}=true to override deliberately.`
  );
}

/**
 * G5's tracking half (spec-gates.md §1a), resolved by Mikko 2026-08-21.
 *
 * Under `forms` protection Word records the permitted fills UNTRACKED, and
 * reading `Document.TrackRevisions` throws outright (CC-6). So Adeu's standing
 * "always writes tracked changes" contract is not merely inconvenient here, it
 * is unenforceable — and it cannot even detect that it has been broken.
 *
 * Writing anyway with only a report note was rejected as the worst option: a
 * guarantee that quietly weakens under a condition the caller cannot detect is
 * more dangerous than no guarantee, because callers automate against the
 * guarantee. Hence refuse by default, with an explicit opt-in.
 *
 * This is deliberately a SEPARATE parameter from `ignore_document_protection`.
 * That one bypasses a gate the author set; this one accepts a downgrade in
 * Adeu's own output guarantee. The writes in question are ones Word itself
 * permits, so no protection is being ignored.
 */
export function checkUntrackedWrite(
  editNumber: number,
  protection: DocumentProtection,
  overrides: GateOverrides = NO_OVERRIDES,
): string | null {
  if (!isProtectionActive(protection) || protection.edit !== "forms") return null;
  if (overrides.allow_untracked_writes) return null;
  return gateError(editNumber, {
    subject: `the document is protected (${describeProtection(protection)}).`,
    rule:
      "Word records fills in a forms-protected document as UNTRACKED changes, so Adeu " +
      "cannot honour its guarantee that every write is a tracked change.",
    alternative: "Remove the protection to write tracked changes normally",
    override: ALLOW_UNTRACKED_WRITES,
  });
}

export const UNTRACKED_WRITE_NOTE =
  "written UNTRACKED: the document is forms-protected, where Word does not record revisions " +
  `(${ALLOW_UNTRACKED_WRITES}=true was passed)`;

// ---------------------------------------------------------------------------
// Content-shape gates: G8, G11, G13
// ---------------------------------------------------------------------------

/**
 * G8 — the target is an empty control's placeholder ghost text.
 *
 * Not a span-intersection gate like its siblings, and that is forced rather
 * than chosen: an empty control projects its ghost as a virtual
 * `{>>placeholder: …<<}` bubble, so it has no content spans to intersect
 * (pinned in cc_span_control_identity.test.ts). The match is therefore
 * against the placeholder text itself.
 *
 * Worth gating loudly because the failure is invisible: ghost runs are not
 * content, so an edit "succeeds", Word discards the run the moment the field
 * is touched, and the text the agent wrote is gone with no error anywhere. No
 * override — this is not a lock the caller can reasonably insist past, it is a
 * category error about what the text IS.
 */
export function checkPlaceholderTarget(
  editNumber: number,
  targetText: string,
  infos: readonly SdtInfo[],
): string | null {
  const needle = (targetText || "").trim();
  if (!needle) return null;
  for (const info of infos) {
    if (!info.showingPlaceholder) continue;
    const ghost = (info.placeholderText || "").trim();
    if (!ghost || !ghost.includes(needle)) continue;
    return gateError(editNumber, {
      subject: `target_text is the placeholder text of ${describeControl(info)}, which is EMPTY.`,
      rule:
        "Placeholder text is a ghost prompt, not content: Word discards it as soon as the " +
        "field is filled, so an edit to it would be silently lost.",
      alternative:
        `Fill the field with set_field, or insert at its empty pair ` +
        `{#cc:${info.ordinal}}{#/cc:${info.ordinal}}`,
    });
  }
  return null;
}

/** The only two texts a checkbox control's projection may be edited to or from. */
export const CHECKBOX_STATES = ["[ ]", "[x]"] as const;

/**
 * G11 — a checkbox may be toggled, not rewritten.
 *
 * `[x]` is a projection of `w14:checked` plus a glyph run, not text. Replacing
 * it with prose would write the prose into the control's content while leaving
 * `w14:checked` untouched, so the checkbox would render with its old state and
 * stray text beside it.
 */
export function checkCheckboxEdit(
  editNumber: number,
  controls: readonly SdtInfo[],
  targetText: string,
  newText: string,
): string | null {
  const info = controls.find((i) => i.cls === "checkbox");
  if (!info) return null;
  const states: readonly string[] = CHECKBOX_STATES;
  if (states.includes(targetText.trim()) && states.includes(newText.trim())) {
    return null;
  }
  return gateError(editNumber, {
    subject: `edit targets ${describeControl(info)}, which is a checkbox.`,
    rule:
      "Its [x] / [ ] projection reflects the w14:checked state rather than editable text, " +
      "so replacing it with other text would leave the checkbox state unchanged.",
    alternative:
      "Toggle it by replacing [x] with [ ] (or the reverse), or use set_field",
  });
}

/**
 * G13 — text edits may not touch data-bound content.
 *
 * A bound control's text is a projection of an XML store item. Word re-reads
 * the store on open, so a text edit that skips the store is reverted — the
 * change survives the save and vanishes for the user.
 *
 * No override, and that asymmetry is deliberate: the other gates refuse
 * something Word would refuse, so overriding them just accepts Word's verdict.
 * Here the write would appear to succeed and then silently revert, and no
 * parameter can make the text path keep the store consistent. The capable path
 * is `set_field`, which dual-writes (spec-set-field §6).
 */
export function checkBoundControl(
  editNumber: number,
  controls: readonly SdtInfo[],
): string | null {
  const info = controls.find((i) => i.bound);
  if (!info) return null;
  const where = info.bindingXpath ? ` (bound to ${info.bindingXpath})` : "";
  return gateError(editNumber, {
    subject: `edit targets the content of ${describeControl(info)}, which is data-bound${where}.`,
    rule:
      "Its text is a projection of an XML data store, and Word re-reads that store when the " +
      "document opens, so an edit that skips the store is reverted.",
    alternative: "Use set_field, which writes the control and its bound store together",
  });
}

// ---------------------------------------------------------------------------
// Structural gates: G14, G15
// ---------------------------------------------------------------------------

/**
 * Controls the range only PARTLY covers — the walls G14/G15 care about.
 *
 * A control fully inside the range, or fully containing it, is not a wall
 * crossing: the first is ordinary content being replaced, the second is an
 * ordinary edit within a control. Only a control that some of the range is
 * inside and some outside has a wall running through the target.
 */
export function crossedControlWalls(
  controlsInRange: readonly SdtInfo[],
  controlsAtStart: readonly SdtInfo[],
  controlsAtEnd: readonly SdtInfo[],
): SdtInfo[] {
  const startSet = new Set(controlsAtStart);
  const endSet = new Set(controlsAtEnd);
  return controlsInRange.filter((i) => startSet.has(i) !== endSet.has(i));
}

/**
 * G15 — a paragraph merge may not hoist content across a control wrapper.
 *
 * The Double-Sided Merge Refusal class, extended from paragraph walls to
 * control walls. Merging a paragraph inside a control with one outside it has
 * to move content across the wrapper in one direction or the other; either way
 * a control gains or loses text its author never scoped to it, and no
 * tracked-change representation of that is honest.
 */
export function checkBlockMergeAcrossControl(
  editNumber: number,
  crossed: readonly SdtInfo[],
): string | null {
  if (crossed.length === 0) return null;
  const names = crossed.map(describeControl).join(", ");
  return gateError(editNumber, {
    subject: `edit merges paragraphs across the boundary of ${names}.`,
    rule:
      "Content may not be hoisted into or out of a content control by a merge: the wrapper " +
      "would gain or lose text that was never scoped to it.",
    alternative:
      "Split this into two edits, one on each side of the control boundary",
  });
}

/** G14's per-edit report note (spec-gates.md §5). */
export function segmentationNote(controls: readonly SdtInfo[]): string {
  const names = controls.map(describeControl).join(", ");
  return `segmented at the boundary of ${names}: only the text outside the control was changed`;
}

// ---------------------------------------------------------------------------
// Report disclosure (spec-gates.md §5)
// ---------------------------------------------------------------------------

/**
 * `Overrides: ignore_control_locks (CC:7, CC:12 edited inside locked controls)`
 *
 * Disclosure is the other half of the override bargain: the caller opted out
 * of a safety rail, and the report says so where a human reviewing the batch
 * will see it.
 */
export function overridesNote(
  overrides: GateOverrides,
  controlsTouched: readonly SdtInfo[],
): string | null {
  if (!anyOverrideUsed(overrides)) return null;
  const used: string[] = [];
  if (overrides.ignore_control_locks) {
    const detail =
      controlsTouched.length > 0
        ? ` (${controlsTouched.map(describeControl).join(", ")} edited inside locked controls)`
        : "";
    used.push(`${IGNORE_CONTROL_LOCKS}${detail}`);
  }
  if (overrides.ignore_document_protection) used.push(IGNORE_DOCUMENT_PROTECTION);
  if (overrides.allow_untracked_writes) used.push(ALLOW_UNTRACKED_WRITES);
  return "Overrides: " + used.join(", ");
}
