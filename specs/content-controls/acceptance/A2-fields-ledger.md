# A2 — Fields Ledger & Discovery (CC-2)

### A2.1 — Ledger golden
- **Given** the standard fixture saved as `cc_fixture.docx`.
- **When** `read_docx(mode="fields")` (MCP, both servers) or `adeu extract cc_fixture.docx --mode fields` (CLI).
- **Then** the rendered text equals GOLDEN-LEDGER exactly.
- Surfaces: +MCP (node & python servers), +CLI.

### A2.2 — Protection line on a zero-control protected document
- **Given** a document with NO content controls and
  `w:documentProtection w:edit="forms" w:enforcement="1"` (the Ontario Juries Form 1
  shape; synthetic equivalent fine).
- **When** `mode="fields"`.
- **Then** output is the two header lines with
  `Protection: fill-in-forms only (enforced)` and `No content controls.`
- Surfaces: +MCP, +CLI.

### A2.3 — Pagination
- **Given** a synthetic document with 250 inline text controls.
- **When** `mode="fields"` with default offset, then `fields_offset=100`, then `200`.
- **Then** responses carry lines CC:1–100, CC:101–200, CC:201–250 respectively; the
  first two end with `… 150 more — pass fields_offset=100 to continue.` /
  `… 50 more — pass fields_offset=200 to continue.`; the last has no continuation line.
- Surfaces: +MCP.

### A2.4 — Appendix summary
- **Given** the standard fixture.
- **When** `mode="appendix"`.
- **Then** the appendix contains a `## Content Controls` section with exactly the two
  ledger header lines + the surface's fields-mode hint, and does NOT contain any
  `CC:` detail lines.
- Surfaces: +MCP, +CLI (`--mode appendix` equivalent if exposed; else MCP only).

### A2.5 — Anonymous controls render without fabricated names
- **Given** a fixture with one control carrying neither alias nor tag (DAU reality).
- **When** `mode="fields"`.
- **Then** its line reads `CC:1  text — p1 — value: "…"` — no empty quotes, no
  `(tag: )`, no invented label.
- Surfaces: +MCP.

### A2.6 — Per-edit reports name the field
- **Given** the standard fixture; a batch with one `ModifyText` changing `ACME Corp`
  → `Acme Corporation`.
- **When** the batch report renders.
- **Then** that edit's report block contains `field: CC:3 "Counterparty" (tag: counterparty)`.
- Surfaces: both engines (+MCP report renderers).

### A2.7 — Schema shape survives client stripping
- **Given** the node MCP server's raw `tools/list`.
- **Then** `read_docx.mode` publishes a single string enum including `"fields"`;
  `fields_offset` publishes `type: number`; no property anywhere in the tool gained an
  `anyOf`/`oneOf`; the tool description (with build tag) is ≤ 2048 chars.
  (Extend `repro.qa_2026_07_23.client-compat.test.ts`.)
- Surfaces: node MCP (python server mirrored by its schema test).
