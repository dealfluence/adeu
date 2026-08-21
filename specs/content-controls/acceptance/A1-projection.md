# A1 — Projection (CC-1)

All goldens from [fixture-standard.md](fixture-standard.md). "Both engines" means
python `extract_text_from_stream`/ingest AND node ingest produce identical text, and
each engine's mapper mirrors it (contract test per engine).

### A1.1 — Full-document raw golden
- **Given** the standard fixture.
- **When** the raw view is produced.
- **Then** it equals GOLDEN-RAW exactly (modulo a single trailing newline).
- Surfaces: both engines.

### A1.2 — Full-document clean golden
- **Given** the standard fixture.
- **When** the clean view is produced.
- **Then** it equals GOLDEN-CLEAN exactly: anchors persist, the CC:2 placeholder bubble
  is gone, `[x]` persists.
- Surfaces: both engines.

### A1.3 — Ordinals are stable and document-ordered
- **Given** the standard fixture read twice (two independent loads).
- **Then** both reads assign identical `CC:N` ordinals, 1…16, in projection order —
  including the un-anchored classes (checkbox CC:6 consumes an ordinal; repeating
  CC:11/12/13 consume ordinals without emitting anchors).
- Surfaces: both engines.

### A1.4 — Placeholder ghost text never projects as body text
- **Given** the standard fixture.
- **When** raw view is produced.
- **Then** the string `Click or tap here to enter text.` occurs ONLY inside the
  `{>>placeholder: …<<}` bubble — never as bare paragraph text. Clean view does not
  contain it at all.
- Surfaces: both engines.

### A1.5 — Tracked changes render inside anchors
- **Given** the standard fixture after `ModifyText` `ACME Corp` → `Acme Corporation`.
- **When** raw view is produced.
- **Then** the CC:3 region renders
  `{#cc:3}{--ACME Corp--}{++Acme Corporation++}{>>…<<}{#/cc:3}` (bubble content per the
  existing change-bubble format; anchors stay outside the CriticMarkup tokens); clean
  view renders `{#cc:3}Acme Corporation{#/cc:3}`.
- Surfaces: both engines.

### A1.6 — Anchor tokens survive chrome-stripping passes
- **Given** the standard fixture.
- **When** (a) search snippets are produced for query `ACME`, (b) outline mode renders a
  version of the fixture whose CC:1 sentence is styled as Heading 1.
- **Then** `{#cc:3}` appears intact in the search snippet (not emphasis-mangled), and
  outline entries never contain broken `{#cc` fragments (either the whole token or —
  for outline heading text — tokens may be omitted entirely, but never split).
- Surfaces: both engines (+MCP search path).

### A1.7 — Anchor fabrication is refused
- **Given** the standard fixture.
- **When** a `ModifyText` attempts (a) inserting new text containing `{#cc:99}`,
  (b) deleting exactly the `{#/cc:3}` token via a target that covers it but not the
  control's content, (c) rewriting `{#cc:7 locked}` to `{#cc:7}`.
- **Then** each is rejected with a `BatchValidationError` naming anchor tokens as
  structural (Strict Refusal class); the document is unchanged.
- Surfaces: both engines.

### A1.8 — Checkbox tokens replace glyphs in both directions
- **Given** a fixture variant with one checked (`☒` + `w14:checked val=1`) and one
  unchecked (`☐` + val=0) checkbox.
- **When** raw view is produced.
- **Then** the view contains `[x]` and `[ ]` respectively and NO `☒`/`☐` characters.
- Surfaces: both engines.

### A1.9 — Banner appears exactly when warranted
- **Given** (a) the standard fixture, (b) the forms-protected variant, (c) a plain
  document with zero controls and no protection.
- **When** the full view is rendered by a surface (CLI extract; MCP read_docx).
- **Then** (a) yields GOLDEN-BANNER; (b) yields the banner with
  `fill-in-forms only (enforced)`; (c) yields NO banner line at all.
- Surfaces: +CLI, +MCP (both servers).

### A1.10 — Headers/footers participate
- **Given** a document whose header contains one inline text control.
- **When** raw view is produced.
- **Then** the header's control is anchored and consumes ordinal `CC:1` (parts project
  headers first), and body controls number after it.
- Surfaces: both engines.
