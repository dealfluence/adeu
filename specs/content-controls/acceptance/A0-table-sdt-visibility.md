# A0 — Table Row/Cell SDT Visibility (CC-0, P0 data loss)

Runs BEFORE the projection feature exists: these examples assert plain visibility in
today's projection (no `{#cc:}` anchors yet — CC-1 later upgrades these regions like any
other). Build documents per [fixture-standard.md](fixture-standard.md) (the table
portion alone is sufficient for A0.1–A0.4).

### A0.1 — Cell-level SDT content is visible and the row keeps its geometry
- **Given** a table row: plain cell `Role` + a cell-level control (`sdtContent > w:tc`)
  containing `Contracting Officer` (fixture CC:14 row).
- **When** the raw or clean view is extracted (python `extract_text_from_stream`, node
  ingest; both views).
- **Then** the row projects as `Role | Contracting Officer` — the wrapped cell's text is
  present AND the cell separator count matches the grid (2 cells ⇒ 1 pipe). A row
  rendering as bare `Role` is the data-loss regression.
- Surfaces: both engines (Node: pin existing correct behavior; Python: currently FAILS).

### A0.2 — Row-level SDT rows are visible
- **Given** a row wrapped whole in a control (`sdtContent > w:tr`) with cells
  `Approver` | `Jane Roe` (fixture CC:15 row).
- **When** raw and clean views are extracted.
- **Then** `Approver | Jane Roe` appears as its own table row line, in document order
  (between the `Role` row and the `Notes` row).
- Surfaces: both engines.

### A0.3 — Mapper stays synchronized (Virtual Text contract)
- **Given** the full standard fixture table.
- **When** ingest text and mapper text are produced for the same document.
- **Then** they are byte-identical (the existing contract test pattern), and a
  `ModifyText` targeting `Jane Roe` → `John Roe` inside the row-level control resolves
  and applies (tracked change lands inside the wrapped row; row count unchanged).
- Surfaces: both engines.

### A0.4 — Nested wrappers traverse
- **Given** a `w15:repeatingSectionItem` control wrapping a `w:tr` inside a table
  (FedRAMP-style), nested one level inside another sdt.
- **When** raw view is extracted.
- **Then** the row's cell text appears exactly once (no duplication from double
  traversal, no loss).
- Surfaces: both engines.

### A0.5 — Corpus scale check (skip-if-missing)
- **Given** corpus `fedramp_ssp_rev4` is present (`corpus_path` helper, else skip).
- **When** the clean view is extracted.
- **Then** total projected character count exceeds 400,000 (the document holds ~462k
  chars of `w:t` text; pre-fix Python projects a fraction of it via 45 paginated pages —
  assert on the unpaginated engine-level extraction).
- Surfaces: python (node optional).
