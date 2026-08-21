# Progress Log — Content Controls

Append-only. Newest entries at the bottom. Every entry: date, author (human or agent),
what happened, and any decision/deviation with its justification.

---

## 2026-08-21 — Initiative bootstrap (Claude, with Mikko)

- Field research across 24 US/Canada public-sector .docx/.dotx (10 SDT-positive).
  Headline facts driving the design (full evidence in the proposal artifact):
  - FedRAMP SSP rev4: 5,007 SDTs — 3,881 checkboxes (plain `☒/☐` text runs, not
    `w:sym`), 459 text, 334 date, 27 combo, 21 dropdown, 94 data-bound, 718 showing
    placeholders, 371 cell-level SDTs, 3 `w:temporary`.
  - DAU Acquisition Plan: 162 SDTs, 48 locked (27 `sdtContentLocked` + 21
    `contentLocked`), 40 placeholders, **zero tags/aliases** (anonymous controls are
    the real-world norm; ordinals must be the primary identity).
  - US District Court (W.D. Wash.) model ESI agreement: every field data-bound;
    tags contain spaces and `#` ("Case #").
  - Ontario Juries Form 1: zero SDTs but `w:documentProtection edit="forms"
    w:enforcement="1"` with a real password hash — protection gates matter even for
    legacy-field documents.
  - Placeholder text is custom prose ("[Plaintiff]", "Program Name"), not the stock
    Word string. A dropdown's first `listItem` can be its own prompt ("Choose a type.").
  - `w:sdt` ids appear negative in the wild (signed int32 — consistent with the
    ST_LongHexNumber lesson in AI_CONTEXT §8, though sdt `w:id` is ST_DecimalNumber).
- Behavior audit of v2.4.1 (probes preserved in the proposal artifact):
  - Both engines flatten SDT structure entirely; placeholder ghosts read as body text.
  - Tracked edits are written inside `sdtContentLocked` controls and into
    forms-protected documents without complaint.
  - A ModifyText "fill" of an empty field leaves `w:showingPlcHdr` set and inherits the
    gray `PlaceholderText` style — the field remains empty to Word.
  - **P0 found:** Python drops row-level and cell-level SDT-wrapped table content
    (and misaligns the row); Node handles both. → CC-0.
- Specs v1 written and frozen; acceptance examples authored; corpus manifest +
  fetch mechanism committed. Corpus documents are fetch-on-demand and gitignored.
- Open items deliberately parked: bound-field reject desync policy, review actions
  inside locked controls, anchor-density escape hatch (`[COM-PENDING]` / CC-6, CC-9).
