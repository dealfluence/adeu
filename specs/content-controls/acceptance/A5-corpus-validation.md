# A5 — Corpus Validation (CC-3)

Every example: **skip cleanly when the document is absent** (helpers `corpus_path(key)`
/ `corpusPath(key)`); never download inside a test. Floors are ~95% of the 2026-08-21
scan facts (manifest `sdt_facts`) to absorb upstream drift. These run after CC-0; the
ledger/anchor assertions activate with CC-1/CC-2 (structure the tests so the pre-CC-1
subset is green on CC-0 alone).

### A5.0 — fedramp_ssp_rev4: projection scale (pre-CC-1 subset)
*Moved here from A0.5 on 2026-08-21 (Mikko's sign-off, PROGRESS.md): the example needs
`corpus_path()`, a CC-3 deliverable, but CC-3 depends on CC-0 — the graph was circular.*
- **Then** the unpaginated clean view exceeds 400,000 characters
  (measured 2026-08-21: python 498,800, node 498,662).
- **Caveat, and the reason this example is not sufficient on its own:** the floor does
  **not** discriminate the CC-0 bug. With row/cell sdt descent disabled the same
  document still projects 490,345 characters. The discriminating assertion is the
  python/node identical-count parity check in A5.1 — that separates 490,345 from
  498,662 at once. Do not treat a green A5.0 as evidence that sdt traversal works.
- Engines: python + node.

### A5.1 — fedramp_ssp_rev4: scale, classes, geometry
- **Then** ledger lists ≥ 4,750 controls; per-class floors: checkbox ≥ 3,690,
  text ≥ 430, date ≥ 315, richtext ≥ 260, combobox ≥ 25, dropdown ≥ 19, picture ≥ 4;
  BOUND ≥ 89; EMPTY ≥ 680; ≥ 350 controls marked `table cell`; ≥ 2 `TEMPORARY`.
  Full raw view contains ≥ 3,690 `[x]`/`[ ]` tokens and zero `☒`/`☐` characters.
- Engines: python + node (identical counts — parity assertion).
- **Known blockers (measured 2026-08-21, PROGRESS.md).** The parity assertion fails
  today on three non-sdt divergences worth 138 chars / 78 lines: python emits the
  literal string `<w:br w:type="page"/>` into the projection (17 occurrences, CC-10);
  python coalesces adjacent italic runs into one emphasis span where node marks each
  run; node projects header lines python omits. Fix or quarantine these before
  asserting identical counts.

### A5.2 — dau_acquisition_plan: locks and anonymity
- **Then** ledger lists ≥ 154 controls with ≥ 45 `LOCKED (contents)` lines; at least
  150 lines carry NEITHER an alias nor a tag segment; ≥ 38 EMPTY lines whose
  placeholder previews are custom prose (assert at least one placeholder preview does
  NOT equal the stock `Click or tap here to enter text.`).
- **And** a `ModifyText` into any `LOCKED (contents)` control's text is rejected per
  A3.1 (pick the first locked control's value from the ledger programmatically).

### A5.3 — wawd_esi_agreement: bound court fields
- **Then** the ledger contains three `text` lines with tags `Plaintiff`, `Defendant`,
  `Case #` — all `EMPTY` and `BOUND`; `set_field` by tag `Case #` (dangling-or-resolving
  per A4.8 rules) fills it; the raw view renders the caption's placeholder bubbles, and
  `[Plaintiff]` does not appear as bare body text.

### A5.4 — on_juries_form1: enforced forms protection
- **Then** the full-view banner and fields header read
  `fill-in-forms only (enforced)` with `no content controls`; any body `ModifyText` is
  rejected per G5; with `ignore_document_protection=true` it applies.

### A5.5 — ca_talent_recruitment: prompt-as-option
- **Then** the ledger's dropdown line lists options beginning `Choose a type. | Internal | External`;
  `set_field` with `value:"Internal"` succeeds; `value:"External Hire"` is rejected
  listing the options.

### A5.6 — Token-cost bound (spec-projection §8)
- **Given** fedramp_ssp_rev4.
- **Then** (len(raw view with CC tokens) − len(raw view with CC projection disabled or
  computed token overhead)) ÷ len(raw view) ≤ 0.05. Implement as: sum of all emitted
  anchor/flag token lengths ÷ total projection length ≤ 5%.

### A5.7 — odot_uic_drywell: .dotx + cell-level + pictures
- **Then** the .dotx opens through the standard path; ledger lists ≥ 36 controls
  including ≥ 11 `table cell` and ≥ 4 `picture` lines; picture controls carry no inline
  anchors (raw view's image markers unchanged).

### A5.8 — hc_diagnostic_nonlab: chrome + negative id
- **Then** exactly 1 control (building-block) — ledger-only, NO inline anchors anywhere
  in the raw view; the negative `w:id` round-trips untouched through a no-op
  open→save (surgical mode).

### A5.9 — Fetch mechanism smoke (no network in CI default)
- **Given** `scripts/fetch_corpus.py --list`.
- **Then** exits 0 and prints one line per manifest key with its on-disk presence.
  (Full download path is exercised by the optional CI job / manual runs, not unit CI.)
