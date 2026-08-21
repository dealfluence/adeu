# Task Board — Content Controls

Status legend: `pending` / `in-progress` / `review` / `done` / `blocked`
(matches the repo's task-plan convention).

Claim protocol: set `in-progress (agent: <name>, since: <YYYY-MM-DD>, branch: <branch>)`
in your first commit. Mark `done (<short commit hash>)` in the PR that completes the
acceptance examples. Never claim two tasks at once; never work a task whose dependencies
aren't `done`.

Verification bar for every task: referenced acceptance examples pass in **both engines**
(+ marked surfaces); `uv run pytest` + `uv run ruff check .` + `uv run mypy src` green in
`python/`; `npm run build && npm run test && npm run lint` green in `node/`
(see AGENTS.md for exact workflow).

---

## CC-0 — Python parity: SDT-wrapped table rows/cells are invisible (P0, data loss)

- Status: `pending`
- Depends on: —
- Acceptance: [A0](acceptance/A0-table-sdt-visibility.md) (all examples)
- Scope: Python traversal must descend into row-level (`sdtContent > w:tr`) and
  cell-level (`sdtContent > w:tc`) content controls in ingest AND mapper (Virtual Text
  contract); Node already behaves correctly — pin it with a test. Include nested
  SDT-in-SDT rows and `w15:repeatingSectionItem`-wrapped rows. Visibility only — no
  edit-semantics changes. Regression file: `python/tests/test_repro_sdt_table_row_cell_invisibility.py`.
- Note: a prepared task chip with the full repro exists (filed 2026-08-21); this row is
  the authoritative tracker. Ship independently of P1 — this is a straight bug.

## CC-1 — Projection: sdt events, anchors, flags, checkboxes, placeholder bubbles (P1)

- Status: `pending`
- Depends on: CC-0
- Acceptance: [A1](acceptance/A1-projection.md) (all examples), golden fixture in
  [acceptance/fixture-standard.md](acceptance/fixture-standard.md)
- Scope: `sdt_start`/`sdt_end` events in both engines' traversal state machines
  (`utils/docx.py` `traverse_node` + `_iter_block_children`; `utils/docx.ts` twins);
  ingest and mapper emit identical virtual spans (`{#cc:N}` / `{#/cc:N}` tokens, flags,
  `{>>placeholder: …<<}` bubbles, `[x]`/`[ ]` checkbox tokens) per spec-projection.md.
  Extend the anchor-protection passes (outline `_strip_inline_formatting`, search
  `_emphasizedSnippet`) so the new tokens survive marker stripping (QA F4/F22b class).
  Ordinal assignment, empty-pair matchability, clean-view behavior per spec.

## CC-2 — Fields ledger, appendix summary, banner (P1)

- Status: `pending`
- Depends on: CC-1
- Acceptance: [A2](acceptance/A2-fields-ledger.md) (all examples)
- Scope: `read_docx(mode="fields")` + `fields_offset` pagination (MCP, both servers),
  `adeu extract --mode fields` (CLI), appendix "Content Controls" summary block,
  protection/fields banner on full view. Line format per spec-fields-ledger.md, exact.

## CC-3 — Corpus fetch mechanism + corpus validation tests (P1)

- Status: `pending`
- Depends on: CC-0 (cell-level counts require Python parity)
- Acceptance: [A5](acceptance/A5-corpus-validation.md) (all examples)
- Scope: `scripts/fetch_corpus.py` + `shared/corpus/manifest.json` are committed by this
  initiative's bootstrap; this task wires test helpers (`corpus_path()` skip-if-missing
  fixture in `python/tests/utils.py`; `corpusPath()` in `node/packages/core/src/test-utils.ts`),
  implements the A5 invariant tests, and adds an optional CI job (manual trigger /
  `ADEU_FETCH_CORPUS=1`) that fetches and runs them.

## CC-4 — Write gates: locks, groups, document protection, placeholder targets (P2)

- Status: `pending`
- Depends on: CC-1
- Acceptance: [A3](acceptance/A3-gates.md) (all examples)
- Scope: load-time protection state; gate matrix + teaching-error contracts per
  spec-gates.md; `ignore_control_locks` / `ignore_document_protection` batch params
  (MCP both servers, CLI flags `--ignore-control-locks` / `--ignore-document-protection`);
  boundary auto-segmentation at control walls; block-merge refusal across SDT boundaries;
  review-action gating `[COM-PENDING]` per spec.

## CC-5 — `set_field` change type + fill semantics (P2)

- Status: `pending`
- Depends on: CC-4; CC-6 findings must be recorded before this task's PR merges
- Acceptance: [A4](acceptance/A4-set-field.md) (all examples)
- Scope: `set_field` in the DocumentChange union + flat MCP schema (both engines),
  resolution order CC-ordinal → tag → alias, `match_mode` reuse; per-type semantics
  (text/richtext/dropdown/combobox/date/checkbox), placeholder-clearing fill,
  `w:temporary` unwrap, bound-field policy per spec-set-field.md; CLI changes-file
  support; per-edit report `field:` notes.

## CC-6 — Word COM verification battery (P2, informs CC-4/CC-5)

- Status: `in-progress (agent: opencode-windows, since: 2026-08-21, branch: content-controls-specs)`
- Depends on: CC-1 (fixture builders reused); runs on Windows + real Word
  (`python/tests/word_com.py` harness, `xdist_group("live_word")`)
- Acceptance: findings appended to PROGRESS.md and every `[COM-PENDING]` spec section
  resolved (amended or confirmed) — this task's deliverable is *knowledge*, pinned as
  COM-backed tests where feasible.
- Verify: (a) placeholder clearing under track changes — is ghost removal redlined?
  (b) checkbox toggle redline shape under track changes; (c) `w:temporary` unwrap
  behavior; (d) can Word's review UI accept/reject inside `sdtContentLocked`?
  (e) data-bound control: store sync on open; reject-then-reopen behavior;
  (f) does Word re-show placeholder when a control's content is fully deleted?

## CC-7 — MCP/CLI surface: schemas, description budget, client-compat pins (P2)

- Status: `pending`
- Depends on: CC-2, CC-4, CC-5
- Acceptance: examples marked `[surface]` in A2/A3/A4; plus
  `node/packages/mcp-server/src/repro.qa_2026_07_23.client-compat.test.ts` extended:
  every published description ≤ 2048 chars INCLUDING build tag, zero property-level
  `anyOf`/`oneOf`, `set_field` params all-primitive, missing-`field` runtime error is
  clean (client drops primitive `required[]` entries).
- Scope: `read_docx` mode enum + `fields_offset`; `process_document_batch` description
  rebalance to fit `set_field` + override params; n8n node operation descriptions +
  `langchain/` toolkit param docs updated; Smithery/MCPB schema patch re-verified.

## CC-8 — Docs: FIDELITY.md + AI_CONTEXT §13 + GEMINI.md (P1→P2, rolling)

- Status: `pending`
- Depends on: lands with CC-1/CC-2 (projection docs) and again with CC-4/CC-5 (gates/ops)
- Acceptance: FIDELITY.md gains a "Content controls" row set (preserved / normalized /
  omitted per element); AI_CONTEXT.md §13 documents the new tokens next to the existing
  anchor family; GEMINI.md documents `mode="fields"`, override params, `set_field`.

## CC-9 — P3 seeds: bound dual-write hardening, repeating-section ops, field-labeled diff (P3)

- Status: `blocked` (until CC-6 findings + sample templates)
- Depends on: CC-5, CC-6
- Scope sketch (not yet spec'd — write spec-repeating-sections.md before starting):
  repeating-section `insert_item`/`delete_item` intent ops (InsertTableRow precedent),
  bound-store reject-resync policy, `diff_docx_files` hunks labeled with field context,
  n8n batch-fill recipe example.
