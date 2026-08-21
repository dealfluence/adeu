# A3 — Write-Path Gates (CC-4)

Error assertions pin FOUR substrings per gate error (the contract, spec-gates §2):
the `CC:N` reference (with alias/tag when present), the rule statement, the sanctioned
alternative, and the override parameter name (when one exists). Canonical texts below
are the recommended wording; tests MUST NOT pin them verbatim beyond the four
components. Every rejection is transactional: output file unchanged (or per-edit
failure under `partial=true` with nothing saved for that edit).

### A3.1 — Content-locked control refuses edits (G1)
- **Given** the standard fixture.
- **When** `ModifyText` targets `Payment terms are Net 30 days.` → `…Net 90 days.`
- **Then** rejected; error carries: `CC:7`, `"Payment Terms"`, content-locked rule,
  `ignore_control_locks`. Canonical:
  `Edit targets content inside CC:7 "Payment Terms" (tag: fixed_clause), which is content-locked (sdtContentLocked) — Word refuses edits inside locked controls. Remove the lock in Word (Developer → Properties) or pass ignore_control_locks=true to override deliberately.`
- **And when** the same batch runs with `ignore_control_locks=true`, the edit applies
  (tracked change inside the control) and the batch report notes the override.
- Surfaces: both engines, +MCP, +CLI (`--ignore-control-locks`).

### A3.2 — Group region refuses non-field edits, permits nested-field edits (G3)
- **Given** the standard fixture.
- **When** (a) `ModifyText` targets `must not be modified` → `may be modified`;
  (b) `ModifyText` targets `123 Main Street, Ottawa` → `1 King Street, Toronto`.
- **Then** (a) rejected naming `CC:8 "Standard Terms"` as a group (locked region),
  override `ignore_control_locks`; (b) applies normally (nested CC:9 is editable).
- Surfaces: both engines.

### A3.3 — Delete-locked wrapper survives (G2)
- **Given** the standard fixture.
- **When** a `ModifyText` deletes the ENTIRE text of CC:9 (`123 Main Street, Ottawa` → ``).
- **Then** the deletion applies as a tracked change but the `w:sdt` wrapper for CC:9
  remains in the XML; after `accept_all_revisions`, the wrapper still exists with empty
  content and the raw view shows `{#cc:9}{#/cc:9}`.
- Surfaces: both engines.

### A3.4 — readOnly protection blocks everything (G4)
- **Given** the standard fixture with `w:documentProtection w:edit="readOnly" w:enforcement="1"`.
- **When** any `ModifyText` / `set_field` / `insert_row` runs.
- **Then** rejected; error names the protection (`read-only, enforced`) and
  `ignore_document_protection`. With `ignore_document_protection=true` it applies and
  the report notes the override.
- Surfaces: both engines, +MCP, +CLI.

### A3.5 — forms protection allows exactly the form surface (G5)
- **Given** the `cc_fixture_forms` variant (fixture-standard.md).
- **When** (a) `ModifyText` targets boilerplate outside any control
  (`approved boilerplate and must not be modified` → anything);
  (b) `set_field` fills CC:2; (c) `ModifyText` edits `ACME Corp` (inside CC:3).
- **Then** (a) rejected naming fill-in-forms protection; (b) and (c) apply.
- Surfaces: both engines.

### A3.6 — trackedChanges protection blocks review actions only (G7)
- **Given** a document with `w:edit="trackedChanges"` protection and one pending change.
- **When** (a) `ModifyText` adds another tracked edit; (b) `AcceptChange` on the pending id.
- **Then** (a) applies; (b) rejected naming the protection ("resolving revisions is what
  this protection forbids") and `ignore_document_protection`.
- Surfaces: both engines.

### A3.7 — Placeholder ghosts are not editable text (G8)
- **Given** the standard fixture.
- **When** `ModifyText` targets `Click or tap here to enter text.` (any replacement).
- **Then** rejected; error names `CC:2 "Client Name"`, states the target is placeholder
  text of an EMPTY field, and points to BOTH sanctioned fills: `set_field` and inserting
  at `{#cc:2}{#/cc:2}`. XML untouched: `w:showingPlcHdr` still present, ghost run intact.
- Surfaces: both engines, +MCP, +CLI.

### A3.8 — Checkbox tokens accept only the toggle (G11)
- **Given** the standard fixture.
- **When** (a) `ModifyText` `[x]` → `[ ]` (with context `Confidentiality applies: `);
  (b) `ModifyText` `[x]` → `yes`.
- **Then** (a) applies as a toggle: `w14:checked` → 0 and the glyph run swaps to `☐`
  (per `w14:uncheckedState`) as one tracked del+ins; (b) rejected pointing at the toggle
  and `set_field`.
- Surfaces: both engines.

### A3.9 — Bound content redirects to set_field (G13)
- **Given** the standard fixture.
- **When** `ModifyText` targets `M-2026-001` → `M-2026-002`.
- **Then** rejected; error names `CC:10 "Matter Number"`, explains the data binding
  (edits that skip the store are reverted by Word on open), and points to `set_field`.
- Surfaces: both engines.

### A3.10 — Boundary auto-segmentation (G14)
- **Given** the standard fixture.
- **When** `ModifyText` targets `Counterparty: ACME Corp.` → `Supplier: ACME Corp.`
  (span starts outside CC:3, ends inside… the changed word is outside; the unchanged
  tail crosses into the control).
- **Then** the edit applies, segmented at the control wall: the tracked change touches
  only text outside CC:3 (`Counterparty:` → `Supplier:`), the control content is
  untouched, and the per-edit report notes the segmentation.
- Surfaces: both engines.

### A3.11 — No merges across block-control walls (G15)
- **Given** the standard fixture.
- **When** a `ModifyText` deletion spans from `…third-party claims.` (inside block
  CC:1) across the paragraph boundary into `This Agreement is made between` (outside).
- **Then** rejected structurally (content may not be hoisted across a control wrapper);
  error suggests two separate edits.
- Surfaces: both engines.

### A3.12 — Context widening never crosses a locked wall
- **Given** a document where a locked control's text is the only disambiguator between
  two identical body strings.
- **When** `diff`-generated edits are made self-contained.
- **Then** widened context stops at the locked-control boundary (edits coalesce or pin
  instead), and the emitted batch replays cleanly through apply.
- Surfaces: both engines (diff → apply closure test).
