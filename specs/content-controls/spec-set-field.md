# Spec: `set_field` Change Type

Status: frozen v1 · Task: CC-5 · Acceptance: [A4](acceptance/A4-set-field.md)
`[COM-PENDING]` items resolved by CC-6 on 2026-08-21: (a) and (c) confirmed,
(b) amended (Word writes ins-before-del), (e) and (f) amended — see PROGRESS.md
and `python/tests/test_live_word_content_controls.py`, which pins each finding.

One new member of the `DocumentChange` union (both engines), designed to fill a content
control the way Word fills it. Text-first editing (ModifyText at the sanctioned
surfaces) routes into the same semantics — `set_field` is the explicit, batchable form.

## 1. Shape

```jsonc
{
  "type": "set_field",
  "field": "CC:4" | "governing_law" | "Governing Law",  // required
  "value": "Ontario",                                    // required (string; see §5)
  "match_mode": "strict" | "first" | "all",              // optional, default "strict"
  "comment": "…"                                         // optional, wraps the change
}
```

- `field` resolution order: (1) `CC:<N>` ordinal; (2) exact `w:tag`; (3) exact
  `w:alias`. Tags/aliases are matched case-sensitively and may contain spaces/`#`.
  Ambiguity (a tag matching multiple controls — the repeating-section reality) obeys
  `match_mode`: `strict` errors listing the candidates as `CC:` ids; `first` takes
  projection order; `all` fans out (each occurrence reported, `occurrences_modified`).
  An unresolvable `field` gets the self-service error listing available tags/aliases
  (capped) and pointing at `mode="fields"` — the invalid-action-id error class.
- Schema constraints (AI_CONTEXT §7a): all properties primitive (`string`); no unions;
  the flat MCP schema (`FlatDocumentChange`) gains `field`/`value` as optional strings —
  requiredness is enforced at runtime with clean errors ("set_field requires 'field'"),
  never a Zod dump, because clients drop primitive `required[]` entries.
- CLI: valid in `apply`/`markup` changes files via `StrictBatchChanges` (missing `type`
  stays a hard error there, per surface-specific requiredness).

## 2. Gate interaction

`set_field` passes through the same gates as any edit (spec-gates.md): locked controls
G1 (override applies), forms protection G5 (set_field is exactly what stays allowed),
G10/G12 value validation. `set_field` on class `picture`, `building-block`, `group`,
`repeating`, `repeating-item` is rejected in v1 with a "not a value-bearing field" error
naming the class.

## 3. Tracked-change author & atomicity

The fill emits ONE atomic del+ins pair (commented-modify atomicity rule §6 of
AI_CONTEXT: `comment` present ⇒ never word-split; for uniformity `set_field` is always
atomic, comment or not). Author resolution identical to ModifyText.

## 4. Fill semantics (empty / placeholder state)

When the target control is in placeholder state (`w:showingPlcHdr`):

1. Remove `w:showingPlcHdr` from `sdtPr`.
2. Remove the ghost run(s) UNTRACKED — Word does not redline placeholder removal
   (CONFIRMED, CC-6(a): filling an empty control produces exactly one revision, the
   insertion; `w:showingPlcHdr` and the ghost run simply vanish).
3. Insert `value` as a tracked `w:ins` whose `rPr` derives from `sdtPr/w:rPr` when
   present, else from the ghost run's rPr MINUS `rStyle PlaceholderText`, else from
   paragraph context (Formatting Inheritance rule). The stripping is not optional:
   CC-6(a) shows Word's own fill carries no `rStyle PlaceholderText` at all.
4. `w:temporary` controls: unwrap the sdt shell (content stays, wrapper goes).
   CONFIRMED and BROADENED by CC-6(c): Word unwraps on ANY content edit — tracked or
   untracked, placeholder or already-filled — and an untouched temporary control
   survives a round trip intact. The revision outlives the wrapper, so rejecting a
   `set_field` on a temporary control restores the old text but NOT the control; the
   unwrap is one-way. Adeu matches Word (unwrap on the edit, do not restore on reject);
   the per-edit report note is the disclosure.

When the control already has content: standard atomic tracked replacement of the full
current content with `value` (affix trimming applies; `restore_matched_typography`
applies).

Clearing: `value: ""` deletes content (tracked) and leaves the empty pair; placeholder
is NOT re-instated in v1 — which CC-6(f) CONFIRMS for the state Adeu produces: while a
deletion is still pending Word leaves `w:showingPlcHdr` off too, across save and reopen,
because a control full of `w:delText` is not yet empty.

Word does re-instate the placeholder the moment the emptying becomes real — an untracked
delete, or **accepting** a tracked one — resolving the prose from the `w:placeholder`
doc part in the glossary. Two consequences, both deferred to v1.1/CC-9 rather than
smuggled into v1:

- `accept_all_changes` over a control emptied by tracked deletion yields a document that
  DIFFERS from what Word yields from the same input (Word re-shows the placeholder,
  Adeu leaves an empty control). Divergence, not corruption.
- The regenerated ghost run carries NO `rStyle PlaceholderText`. `w:showingPlcHdr` is
  therefore the *only* reliable placeholder signal — detection by ghost style would miss
  every control Word itself emptied. Binding on CC-1's projection.

## 5. Per-class value handling

| Class | `value` accepted | Writes |
| --- | --- | --- |
| `text` | any single-line string; strings containing `\n` rejected unless `w:multiLine` (then `\n` → `w:br`); `\n\n` always rejected (no paragraphs in plain-text controls) | content runs |
| `richtext` | string; `\n\n` creates tracked paragraphs (standard multi-paragraph insertion rules) | content blocks |
| `dropdown` | must equal a `listItem` `displayText` (preferred) or `w:value` (then the displayText is written) — else G10 error listing options | content run + `w:dropDownList w:lastValue` |
| `combobox` | as dropdown but free text permitted; report notes "not in the option list" when applicable | content run |
| `date` | `YYYY-MM-DD` (canonical) or exact current-format rendering; writes text formatted per `w:dateFormat` (v1 supports the format's `yyyy/MM/dd/d/M` token subset; unsupported patterns fall back to writing the value verbatim + report note) | content run + `w:date w:fullDate` attribute updated SILENTLY (URL_RETARGET precedent — attribute sync is not a redline) |
| `checkbox` | truthy: `true/x/[x]/checked/1` · falsy: `false/[ ]/unchecked/0/""` — else G11 error | `w14:checked w14:val` + glyph run swapped per `w14:checkedState`/`w14:uncheckedState` (char + font), as ONE tracked ins+del pair — **`w:ins` FIRST, `w:del` after** (amended by CC-6(b): that is Word's order, and order is visible, since the projection reads document order and would otherwise render `{--☒--}{++☐++}` where Word renders `{++☐++}{--☒--}`). `w14:checked` flips SILENTLY, with no revision of its own — attribute sync, the URL_RETARGET class |

## 6. Bound controls (`w:dataBinding`)

`set_field` on a bound control dual-writes:

1. The visible `sdtContent` change, tracked (as §4/§5).
2. The bound CustomXML node (resolved via `w:storeItemID` + `w:xpath`), updated to the
   FINAL value, silently — plus a mandatory per-edit report note:
   `note: bound store /root[1]/matter[1] updated to match`.
3. A missing/unresolvable store item downgrades to content-only write + WARNING note
   (dangling bindings exist in the wild; sanitize's scrub is one producer).

Why the dual-write is mandatory rather than merely tidy (CC-6(e)): **the store wins on
open.** When `sdtContent` and the bound node disagree, Word silently rewrites the content
from the store at load time, with no revision. A tracked edit written to the content
alone is not "inconsistent" — it is *destroyed the next time anyone opens the document*.
Content-only writing to a bound control is data loss with extra steps, which is what G13
exists to prevent.

Known asymmetry (accepted for v1, revisit in CC-9) — **amended by CC-6(e), and it points
the other way from the frozen text.** Word does NOT leave a rejected edit's value in the
store: rejecting restores the content and its binding engine pushes the restored value
back, so both converge. The asymmetry is Adeu's, not Word's: a HEADLESS reject
(`reject_all_changes`, no binding engine) leaves the store holding the rejected value —
and because the store wins on open, Word then *re-applies the rejected value to the
content*. A reject that silently un-rejects itself is the expensive shape of this bug,
so CC-9's resync policy must cover the reject path specifically. The report note remains
the v1 disclosure.

## 7. Reports

Per-edit report block: `field: CC:<N> "<alias>" (tag: <tag>)` + class + old→new value
preview (80-char caps) + notes (store sync, option-list warnings, temporary unwrap).
Batch summary counts `fields_set` alongside `edits_applied` (a set_field is one logical
edit; `match_mode:"all"` fan-out counts occurrences separately — the F-21 rule).

## 8. Description budget (CC-7)

`process_document_batch`'s published description documents `set_field` (shape, field
resolution, value rules ref) and the two override params within the ≤ 2048-char budget
including build tag; current text sits at 1,933 chars, so this REQUIRES a rebalance
(compress the row-op prose; keep the `{#cell:}` stability qualifier — QA F9/F10 content
must survive). Client-compat test pins the final size.
