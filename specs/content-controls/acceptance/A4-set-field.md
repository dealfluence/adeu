# A4 — `set_field` (CC-5)

All against the standard fixture unless noted. XML assertions read the saved package
(lxml/xmldom over the zip — the "mathematical scrub verification" idiom).

### A4.1 — Fill an empty text field by tag
- **When** `{"type":"set_field","field":"client_name","value":"Acme Legal Services Ltd."}`.
- **Then** in the saved XML for CC:2: `w:showingPlcHdr` is ABSENT; no run carries
  `rStyle PlaceholderText`; the ghost run is gone WITHOUT a `w:del` wrapping it
  (CONFIRMED CC-6(a)); a `w:ins` contains `Acme Legal Services Ltd.`.
  Raw view: `{#cc:2}{++Acme Legal Services Ltd.++}{>>…<<}{#/cc:2}`. Clean view:
  `{#cc:2}Acme Legal Services Ltd.{#/cc:2}`. Report block carries
  `field: CC:2 "Client Name" (tag: client_name)` and the old→new preview.
- Surfaces: both engines, +MCP, +CLI changes file.

### A4.2 — Field resolution order and ambiguity
- **Given** a fixture where tag `item_name` appears on TWO controls and alias
  `Item Name` on a third.
- **When** (a) `field:"CC:2"`; (b) `field:"item_name"` with default `match_mode`
  ("strict"); (c) `field:"item_name","match_mode":"all"`; (d) `field:"nonexistent"`.
- **Then** (a) resolves by ordinal; (b) rejected listing the two candidate `CC:` ids;
  (c) fills both, `occurrences_modified: 2`; (d) rejected with the self-service error
  listing available tags/aliases and pointing at `mode="fields"`.
- Surfaces: both engines.

### A4.3 — Dropdown validation (G10)
- **When** (a) `field:"governing_law","value":"British Columbia"`;
  (b) same with `value:"BC"`; (c) same with `value:"Manitoba"`.
- **Then** (a) replaces content `Ontario` → `British Columbia` (tracked) and updates
  `w:dropDownList w:lastValue`; (b) resolves via the `w:value` attr and writes the
  displayText `British Columbia`; (c) rejected, error lists
  `Ontario | British Columbia | Federal`.
- Surfaces: both engines, +MCP.

### A4.4 — Combobox free text
- **Given** a combobox variant of CC:4 (`w:comboBox`, same options).
- **When** `value:"Nunavut"`.
- **Then** applies; report notes the value is not in the option list.
- Surfaces: both engines.

### A4.5 — Date handling (G12)
- **When** (a) `field:"effective_date","value":"2026-03-01"`; (b) `value:"01.03.2026"`.
- **Then** (a) content becomes `2026-03-01` (tracked), and `w:date/@w:fullDate` becomes
  `2026-03-01T00:00:00Z` with NO redline for the attribute change; (b) rejected naming
  expected format `yyyy-MM-dd` (and ISO).
- Surfaces: both engines.

### A4.6 — Checkbox via set_field
- **When** `field:"confidential","value":"false"`.
- **Then** `w14:checked w14:val` → `0`; content glyph swaps to `☐` with the
  `w14:uncheckedState` font, as ONE tracked pair with the `w:ins` BEFORE the `w:del`
  (CC-6(b): Word's order), and `w14:checked` changing with no revision of its own;
  raw view shows the pending toggle, clean view shows `[ ]`.
- Surfaces: both engines.

### A4.7 — Plain-text control rejects structure
- **When** (a) `field:"counterparty","value":"Line1\n\nLine2"`;
  (b) same against a `richtext` control (CC:1's tag `indemnity`).
- **Then** (a) rejected (`w:text` control, no `w:multiLine`); (b) applies as a tracked
  multi-paragraph replacement (standard insertion rules).
- Surfaces: both engines.

### A4.8 — Bound field dual-write (spec-set-field §6)
- **Given** a fixture variant where CC:10's binding RESOLVES (package carries the
  customXml item with `/root/matter`).
- **When** `field:"matter_number","value":"M-2026-002"`.
- **Then** sdtContent change is tracked; the customXml node text equals `M-2026-002`
  (silent write); the report carries `note: bound store /root[1]/matter[1] updated to
  match`. With the standard fixture (dangling binding), the same call applies
  content-only and the report carries a dangling-binding WARNING.
- Surfaces: both engines.

### A4.9 — Temporary control unwraps on fill
- **Given** a control with `<w:temporary/>` and placeholder state.
- **When** `set_field` fills it.
- **Then** the saved XML has NO `w:sdt` wrapper at that location; the inserted text
  stands as a tracked insertion in the paragraph (CONFIRMED CC-6(c)); the ledger no
  longer lists it on re-read. Word unwraps on ANY content edit, so an already-filled
  temporary control unwraps on replacement too — and the unwrap is not undone by
  rejecting the revision.
- Surfaces: both engines.

### A4.10 — Text-first fill parity (`apply_text_revision` / empty-pair insertion)
- **Given** the standard fixture.
- **When** `apply_text_revision` receives the full clean text with
  `{#cc:2}Acme Legal Services Ltd.{#/cc:2}` substituted for the empty pair.
- **Then** the computed edit routes through fill semantics — identical XML outcome to
  A4.1 (showingPlcHdr cleared, no ghost style).
- Surfaces: both engines, +MCP (`apply_text_revision` tool).

### A4.11 — Non-value classes refuse set_field
- **When** `field:"std_terms","value":"anything"` (a group) and
  `field:"deliverables","value":"x"` (a repeating section).
- **Then** each rejected: "not a value-bearing field" naming the class; suggests
  editing nested fields (group) / notes repeating-section ops are not yet supported.
- Surfaces: both engines.

### A4.12 — set_field respects gates
- **Given** the standard fixture.
- **When** `field:"fixed_clause","value":"Net 90"` without overrides.
- **Then** rejected by G1 exactly as A3.1 (same error contract; `set_field` gets no
  special pass).
- Surfaces: both engines.
