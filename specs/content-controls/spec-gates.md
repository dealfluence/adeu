# Spec: Write-Path Gates

Status: frozen v1 · Task: CC-4 · Acceptance: [A3](acceptance/A3-gates.md)
`[COM-PENDING]` items resolved by CC-6 on 2026-08-21 (G9 downgraded to allow, G7
confirmed); findings and the two open sign-off items in PROGRESS.md.

Design principle (house doctrine): **a silent no-op — or a silently doomed op — is the
most expensive bug an agent can consume.** Every gate rejects transactionally
(`BatchValidationError` class, snapshot-restore path) with a self-service teaching error.
Every override is an explicit batch-level parameter, disclosed in tool descriptions,
default `false`. Gates run in `validate_edits` AND independently in the apply path
(pinned edits bypass validation — the part-boundary precedent, AI_CONTEXT §5b).

## 1. Override parameters

| Param | Surfaces | Unlocks |
| --- | --- | --- |
| `ignore_control_locks` | `process_document_batch` (both MCP servers), CLI `--ignore-control-locks`, engine kwarg | Gates G1, G2, G3, G9 |
| `ignore_document_protection` | `process_document_batch`, CLI `--ignore-document-protection`, engine kwarg | Gates G4–G7 |
| `allow_untracked_writes` | `process_document_batch`, CLI `--allow-untracked-writes`, engine kwarg | G5 only — permits the forms-protection writes Word itself records untracked |

Booleans, schema default `false` (truthy defaults survive client stripping; the
defaults are additionally stated in the tool description per §7a rules). Overrides exist
because document owners legitimately edit their own rails; agents must opt in per batch,
and reports note when an override was exercised.

### 1a. Forms protection and the tracking contract

Resolved by Mikko, 2026-08-21, on CC-6's finding. Adeu's standing contract is that it
**always writes tracked changes**. Under `w:documentProtection w:edit="forms"` that
contract is not merely inconvenient, it is unenforceable: CC-6 measured real Word 16.0
writing the permitted fills **untracked**, and *reading* `Document.TrackRevisions` throws
outright there — so Adeu cannot honour the contract and cannot even inspect whether it
has been broken.

Three options were costed. Refusing outright guts the feature for forms-protected
templates, which are precisely the documents a content-controls initiative exists to
serve. Writing untracked with only a report note was rejected as the worst of the three:
a guarantee that quietly weakens under a condition the caller cannot detect is more
dangerous than no guarantee, because callers automate against the guarantee.

**Chosen: refuse by default, with an explicit per-batch opt-in.**

1. Default (`allow_untracked_writes = false`): reject the write with a teaching error
   that names the cause — the document is forms-protected, Word records these writes
   untracked, and Adeu will not silently downgrade its tracking guarantee.
2. With the override: perform the write, and emit a per-edit report note on **every**
   untracked write, not once per batch.

This is deliberately a *separate* param from `ignore_document_protection`. That one
bypasses a gate the document author set; this one accepts a downgrade in Adeu's own
output guarantee. They are different admissions and a caller may well want one without
the other — in particular, the G5 writes in question are ones Word itself *permits*, so
no protection is being ignored at all.

## 2. Gate matrix

Error-message contract for every gate: names the control (`CC:N` + alias/tag when
present), states the rule, names the sanctioned alternative, and names the override
param when one exists. Canonical texts in A3; tests pin the four components as
substrings, not full strings.

| # | Condition | Default behavior |
| --- | --- | --- |
| G1 | Edit range intersects content of a control (or ancestor) with `w:lock` = `sdtContentLocked`/`contentLocked` | Reject. "Word refuses edits inside locked controls." |
| G2 | Edit would delete/unwrap a control whose `w:lock` = `sdtLocked`/`sdtContentLocked` (target consumes the entire control content plus surrounding text, or a block merge would dissolve the wrapper) | Reject; content-only deletion inside a merely delete-locked control is allowed and leaves the wrapper + empty pair |
| G3 | Edit targets text inside a `w:group` control but outside any nested leaf control | Reject as locked region (nested leaves stay editable) |
| G4 | `w:documentProtection w:edit="readOnly"` | Reject all mutating operations (edits, set_field, review actions, row ops) |
| G5 | `w:edit="forms"` | Allow only: `set_field`, edits fully inside leaf-control content, checkbox toggles. Reject any body/table edit outside controls (and legacy-form-field regions are out of v1 scope — reject with that stated). **Those permitted writes are additionally gated on `allow_untracked_writes` (default `false`) — see §1a.** |
| G6 | `w:edit="comments"` | Allow only comment-only changes (`target == new` + comment) and `ReplyComment`; reject text mutations |
| G7 | `w:edit="trackedChanges"` | Text edits proceed (Adeu always writes tracked changes); `AcceptChange`/`RejectChange` are rejected (resolving revisions is exactly what this protection forbids). Confirmed against Word by CC-6(d): Accept and Reject both fail with "This command is not available", document-wide, while tracked editing stays allowed |
| G8 | `ModifyText` target overlaps placeholder ghost text | Reject; point to `set_field` and to inserting at the empty pair `{#cc:N}{#/cc:N}`. Never editable "as text" — ghost runs are not content |
| G9 | Review action (`AcceptChange`/`RejectChange`) on a revision inside a content-locked control | **Allow** (downgraded by CC-6(d), 2026-08-21). Word permits Accept and Reject inside `sdtContentLocked` — the lock stops *typing*, not *review*. Rejecting would make Adeu stricter than Word for no protective benefit and strand revisions a user can resolve in two clicks. Revision resolution inside locked controls is therefore ungated, consistent with §4's accept-all doctrine. Protection, not locks, is what gates review: see G7 |
| G10 | `set_field` value not in a dropdown's `listItem`s | Reject; error lists display texts (first 8). Combobox: free text allowed |
| G11 | Checkbox content edited as text other than the exact `[ ]`↔`[x]` swap | Reject; point to the toggle or `set_field` |
| G12 | Date `set_field` value that is neither ISO (`YYYY-MM-DD`) nor an exact match of the control's `w:dateFormat` rendering | Reject naming the expected format |
| G13 | `ModifyText` targets content of a `bound` control | Reject; point to `set_field` (which dual-writes the store, spec-set-field §6). No override — the text path cannot keep the store consistent |
| G14 | Edit target crosses a leaf-control boundary (starts outside, ends inside, or vice versa) | Auto-segment at the control wall into per-side sub-edits (the table cell-wall precedent) when both segments are independently valid; otherwise reject with the split suggestion |
| G15 | Block-level paragraph merge across an SDT boundary (deletion spanning from outside a block control into it, or across group/repeating walls) | Reject structurally (Double-Sided Merge Refusal class) — content must not be hoisted out of, or into, a control wrapper by a merge |

## 3. Load-time state

Protection state (`w:edit`, `w:enforcement`) is read once at document load from
`word/settings.xml` and surfaced: banner (spec-projection §7), fields-ledger header,
and gate errors. Adeu never verifies or cracks `w:hash` — enforcement is honored as
intent (Word's own enforcement is equally advisory at the XML level); the override
params are the sanctioned bypass and reports disclose their use.

## 4. Interactions with existing machinery

- **Sequential batches:** gates evaluate against the current mid-batch state like every
  other validation; a gate rejection is transactional for the whole batch (`partial=true`
  reports it per-edit without saving that edit, per the existing partial contract).
- **Context widening** (`make_edits_self_contained`): widened context MUST NOT cross a
  content-locked control boundary or group wall (part-boundary clamp precedent) —
  otherwise diff output stops being closed under apply.
- **accept-all / finalize / sanitize:** deliberately NOT gated (owner finalization acts;
  the B2 doctrine). `accept_all_revisions` treats revisions inside locked controls as
  ordinary revisions. Sanitize keeps its existing dataBinding scrub when ejecting custom
  XML parts.
- **Live Word COM:** gates apply in the snapshot-engine pre-resolution layer, so Live
  Word batches fail with the same errors before any COM mutation.

## 5. Report disclosures

When an override is used, the batch report header notes it:
`Overrides: ignore_control_locks (CC:7, CC:12 edited inside locked controls)`.
When G14 auto-segments, the per-edit report notes the segmentation exactly as the
cell-splitter does today.
