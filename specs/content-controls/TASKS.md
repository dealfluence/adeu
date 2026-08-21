# Task Board â€” Content Controls

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

## CC-0 â€” Python parity: SDT-wrapped table rows/cells are invisible (P0, data loss)

- Status: `done (d4e967f + 257a5bd)` (agent: opencode-osx, 2026-08-21) â€” A0.1-A0.4 green
  in both engines. A0.5 moved to A5.0 with Mikko's sign-off (circular dependency on
  CC-3's `corpus_path()`); the test ships here until CC-3 adopts it.
- Depends on: â€”
- Acceptance: [A0](acceptance/A0-table-sdt-visibility.md) (all examples). **Met:**
  engine fix `61bc00a`+`d4e967f` (merged `845afb3`); acceptance closed by `257a5bd`
  (A0.3 apply half in both engines, fixture aligned to fixture-standard.md) and A0.5.
- Scope: Python traversal must descend into row-level (`sdtContent > w:tr`) and
  cell-level (`sdtContent > w:tc`) content controls in ingest AND mapper (Virtual Text
  contract); Node already behaves correctly â€” pin it with a test. Include nested
  SDT-in-SDT rows and `w15:repeatingSectionItem`-wrapped rows. Visibility only â€” no
  edit-semantics changes. Regression file: `python/tests/test_repro_sdt_table_row_cell_invisibility.py`.
  **Correction:** "Node already behaves correctly" held only for row/cell sdts â€”
  `docx/primitives.ts` leaked nested-table rows/cells via recursive
  `getElementsByTagName`; fixed in the same merge. A0.3 also required apply semantics,
  not visibility alone.
- Note: a prepared task chip with the full repro exists (filed 2026-08-21); this row is
  the authoritative tracker. Ship independently of P1 â€” this is a straight bug.
- Downstream must-reads: CC-1 â€” Node's traversal was *not* sound before this work, don't
  assume it; Python and Node still disagree on emphasis-marker coalescing. CC-3 â€” three
  non-sdt corpus parity gaps will fail A5.1's identical-counts assertion, incl. Python
  emitting literal `<w:br w:type="page"/>` into the projection (PROGRESS.md).

## CC-1 â€” Projection: sdt events, anchors, flags, checkboxes, placeholder bubbles (P1)

- Status: `pending`
- Depends on: CC-0
- Acceptance: [A1](acceptance/A1-projection.md) (all examples), golden fixture in
  [acceptance/fixture-standard.md](acceptance/fixture-standard.md)
- Scope: `sdt_start`/`sdt_end` events in both engines' traversal state machines
  (`utils/docx.py` `traverse_node` + `_iter_block_children`; `utils/docx.ts` twins);
  ingest and mapper emit identical virtual spans (`{#cc:N}` / `{#/cc:N}` tokens, flags,
  `{>>placeholder: â€¦<<}` bubbles, `[x]`/`[ ]` checkbox tokens) per spec-projection.md.
  Extend the anchor-protection passes (outline `_strip_inline_formatting`, search
  `_emphasizedSnippet`) so the new tokens survive marker stripping (QA F4/F22b class).
  Ordinal assignment, empty-pair matchability, clean-view behavior per spec.

## CC-2 â€” Fields ledger, appendix summary, banner (P1)

- Status: `pending`
- Depends on: CC-1
- Acceptance: [A2](acceptance/A2-fields-ledger.md) (all examples)
- Scope: `read_docx(mode="fields")` + `fields_offset` pagination (MCP, both servers),
  `adeu extract --mode fields` (CLI), appendix "Content Controls" summary block,
  protection/fields banner on full view. Line format per spec-fields-ledger.md, exact.

## CC-3 â€” Corpus fetch mechanism + corpus validation tests (P1)

- Status: `review` (agent: opencode-windows, 2026-08-21) â€” mechanism, helpers, CI job and
  the **pre-CC-1 subset** of A5 are done and green in both engines. The remaining A5
  examples need CC-1/CC-2/CC-4/CC-5 and are listed per-blocker in PROGRESS.md; this row
  goes `done` when the last of them lands (or Mikko splits the tail into CC-3b).
  Spun out: **CC-11** (Python cannot open a .dotx â€” A5.7 is a strict xfail until fixed).
- Depends on: CC-0 (cell-level counts require Python parity) â€” `review`, engine fix merged
- Acceptance: [A5](acceptance/A5-corpus-validation.md) â€” A5.9 done; A5.1/A5.7/A5.8 done
  for their pre-CC-1 halves; A5.2/A5.3/A5.4/A5.5/A5.6 blocked on ledger, gates, set_field
  and anchors respectively
- Scope: `scripts/fetch_corpus.py` + `shared/corpus/manifest.json` are committed by this
  initiative's bootstrap; this task wires test helpers (`corpus_path()` skip-if-missing
  fixture in `python/tests/utils.py`; `corpusPath()` in `node/packages/core/src/test-utils.ts`),
  implements the A5 invariant tests, and adds an optional CI job (manual trigger /
  `ADEU_FETCH_CORPUS=1`) that fetches and runs them.

## CC-4 â€” Write gates: locks, groups, document protection, placeholder targets (P2)

- Status: `pending`
- Depends on: CC-1
- Acceptance: [A3](acceptance/A3-gates.md) (all examples)
- Scope: load-time protection state; gate matrix + teaching-error contracts per
  spec-gates.md; `ignore_control_locks` / `ignore_document_protection` batch params
  (MCP both servers, CLI flags `--ignore-control-locks` / `--ignore-document-protection`);
  boundary auto-segmentation at control walls; block-merge refusal across SDT boundaries;
  review-action gating per spec â€” resolved by CC-6: G9 is now *allow* (Word permits
  Accept/Reject inside locked controls), G7 confirmed. One open question for G5, in
  PROGRESS.md, needs Mikko's answer before this task's error contracts are final.

## CC-5 â€” `set_field` change type + fill semantics (P2)

- Status: `pending`
- Depends on: CC-4; CC-6 findings must be recorded before this task's PR merges
- Acceptance: [A4](acceptance/A4-set-field.md) (all examples)
- Scope: `set_field` in the DocumentChange union + flat MCP schema (both engines),
  resolution order CC-ordinal â†’ tag â†’ alias, `match_mode` reuse; per-type semantics
  (text/richtext/dropdown/combobox/date/checkbox), placeholder-clearing fill,
  `w:temporary` unwrap, bound-field policy per spec-set-field.md; CLI changes-file
  support; per-edit report `field:` notes.

## CC-6 â€” Word COM verification battery (P2, informs CC-4/CC-5)

- Status: `review` (agent: opencode-windows, 2026-08-21) â€” findings landed, tests green;
  two items need Mikko's sign-off before `done`: the G5 forms-protection question and
  the CC-9 bound-store reject-resync scope (both in PROGRESS.md)
- Depends on: CC-1 (fixture builders reused) â€” started ahead of it; deviation disclosed
  in PROGRESS.md. Ran on Windows + real Word (16.0)
  (`python/tests/word_com.py` harness, `xdist_group("live_word")`)
- Acceptance: findings appended to PROGRESS.md and every `[COM-PENDING]` spec section
  resolved (amended or confirmed) â€” this task's deliverable is *knowledge*, pinned as
  COM-backed tests where feasible. **Met:** all `[COM-PENDING]` tags removed from
  spec-gates.md, spec-set-field.md and A4; 15 COM-backed tests in
  `python/tests/test_live_word_content_controls.py`.
- Downstream must-reads: CC-1 â€” `w:showingPlcHdr` is the only reliable placeholder
  signal and placeholder prose resolves through the glossary part. CC-4 â€” G9 is now
  *allow*, G7 confirmed, G5 has an open sign-off question. CC-5 â€” checkbox is ins-then-
  del, `w:temporary` unwraps on any edit, bound controls are store-authoritative.
- Verify: (a) placeholder clearing under track changes â€” is ghost removal redlined?
  (b) checkbox toggle redline shape under track changes; (c) `w:temporary` unwrap
  behavior; (d) can Word's review UI accept/reject inside `sdtContentLocked`?
  (e) data-bound control: store sync on open; reject-then-reopen behavior;
  (f) does Word re-show placeholder when a control's content is fully deleted?

## CC-7 â€” MCP/CLI surface: schemas, description budget, client-compat pins (P2)

- Status: `pending`
- Depends on: CC-2, CC-4, CC-5
- Acceptance: examples marked `[surface]` in A2/A3/A4; plus
  `node/packages/mcp-server/src/repro.qa_2026_07_23.client-compat.test.ts` extended:
  every published description â‰¤ 2048 chars INCLUDING build tag, zero property-level
  `anyOf`/`oneOf`, `set_field` params all-primitive, missing-`field` runtime error is
  clean (client drops primitive `required[]` entries).
- Scope: `read_docx` mode enum + `fields_offset`; `process_document_batch` description
  rebalance to fit `set_field` + override params; n8n node operation descriptions +
  `langchain/` toolkit param docs updated; Smithery/MCPB schema patch re-verified.

## CC-8 â€” Docs: FIDELITY.md + AI_CONTEXT Â§13 + GEMINI.md (P1â†’P2, rolling)

- Status: `pending`
- Depends on: lands with CC-1/CC-2 (projection docs) and again with CC-4/CC-5 (gates/ops)
- Acceptance: FIDELITY.md gains a "Content controls" row set (preserved / normalized /
  omitted per element); AI_CONTEXT.md Â§13 documents the new tokens next to the existing
  anchor family; GEMINI.md documents `mode="fields"`, override params, `set_field`.

## CC-10 â€” Python leaks raw OOXML into the text projection (P1, parity + output quality)

- Status: `done` (agent: opencode-osx, 2026-08-21) — option **B** chosen by Mikko and
  implemented: both engines project U+000C for a page break, python's pagination splits
  on it, no markup left in either projection. Regression files:
  `python/tests/test_repro_raw_ooxml_in_projection.py` + node twin. FIDELITY.md amended.
- Depends on: —
- Found by: CC-0 corpus measurement, 2026-08-21 (PROGRESS.md)
- Acceptance: no `<w:...>` markup appears in any projected view in either engine; python
  and node agree on `fedramp_ssp_rev4`'s clean-view character count (unblocks A5.1's
  parity assertion). Regression file:
  `python/tests/test_repro_raw_ooxml_in_projection.py` + node twin.
- Scope: python projects a page break as the literal text `<w:br w:type="page"/>`.
  Repro: a `w:p` containing `<w:r><w:t>A</w:t><w:br w:type="page"/><w:t>B</w:t></w:r>`
  projects as `A<w:br w:type="page"/>B`; node projects `\n`. 17 occurrences in
  `fedramp_ssp_rev4`. Ingest and mapper agree, so the Virtual Text contract holds and
  offsets are intact â€” this is an output-quality and parity defect, not corruption, but
  an LLM reads the markup as prose and a `target_text` spanning the break must include
  the XML.
- **Corrected diagnosis (2026-08-21, was mis-scoped when filed as "a leak").** This is
  not accidental serialization. `_PAGE_BREAK_TOKEN` (`utils/docx.py:95`) is a deliberate
  **in-band sentinel**, and it is load-bearing: `pagination.py:262-272` splits blocks on
  it to honour manual page breaks. Node has no equivalent â€” `pagination.ts:131` is
  density-only and ignores manual breaks. So the engines diverge in two coupled places,
  and deleting the token alone would regress
  `test_cli_bug_repro.py::test_manual_page_breaks_pagination` / `_outline` and lose a
  real capability. `docs/FIDELITY.md:36` already settles the projection question â€”
  "Both project as a newline" â€” so **python violates its own documented contract** and
  node is correct. Fix = emit `\n` and route the page-break signal **out of band**
  (offsets alongside the text), preserving python's pagination.
- Three python sites duplicate the branch chain and must move together
  (`utils/docx.py:944`, `:1179`, `:1204` â€” the third inlines the literal instead of the
  constant). `tests/test_run_fusion_equivalence.py` hardcodes the token in its oracle and
  must be updated in the same commit; `pagination.py:270`'s offset arithmetic assumes the
  token occupies real text space.
- **Decision (2026-08-21): option B.** Three options were costed; B was chosen:
  - **(A) Out-of-band offsets.** `paginate()` grows a page-break-offsets argument.
    Correct and keeps both the contract and the capability, but `paginate()` has 15
    call sites across `redline/engine.py`, `mcp_components/doc_cache.py`,
    `_response_builders.py` and `cli.py`, and each needs the offsets plumbed from
    ingest. Invasive, touches core read paths.
  - **(B) Form feed.** Project `\f` (ASCII FF, the conventional plain-text page break)
    in BOTH engines; pagination splits on `\f` instead of the 22-char token. One-line
    pagination change, parity achieved, capability kept, and node pagination could
    later honour manual breaks nearly free. Costs: a non-printing character enters
    both engines' output, and `docs/FIDELITY.md:36` would need amending from
    "both project as a newline".
  - **(C) Literal newline.** Cheapest, matches FIDELITY.md verbatim, but python loses
    manual-page-break pagination â€” `test_cli_bug_repro.py` pagination/outline tests go
    red and page numbers stop tracking Word's.
  **Chosen: B.** Follow-up row still open: node's pagination does not act on the token
  (density-only), so node still ignores manual page breaks — now a one-line fix, since
  the signal is present in its text.
- **Fixing page breaks alone did NOT unblock A5.1 — confirmed by measurement.** The
  divergences partially offset, so closing this one flipped the sign as predicted:
  `fedramp_ssp_rev4` clean view went from python 498,800 / node 498,662 (python +138) to
  python 498,404 / node 498,662 (**node +258**), and differing lines fell 78 → 27. The
  remaining gap is entirely the other two divergences — emphasis-marker coalescing, and
  the header lines node projects that python omits. **A5.1 stays blocked on those two.**
- Split out, NOT in this task: node pagination ignoring manual page breaks is a separate
  capability gap (own row when someone wants it).
- Also silently dropped by BOTH engines, so not parity-visible but real content loss:
  `w:sym` (a symbol glyph vanishes), `w:noBreakHyphen`, `w:softHyphen`, `w:ptab`. Worth
  its own row; `w:sym` matters most since FedRAMP-class templates use it for checkboxes.
- Also in the same parity gap, not necessarily this task: python coalesces adjacent
  italic runs into one emphasis span where node marks each run; node projects header
  lines python omits. Together 138 chars / 78 lines on `fedramp_ssp_rev4`.

## CC-12 - Node's DocumentMapper drifts from its own ingest on real documents (P1)

- Status: `in-progress` (agent: opencode-osx, since: 2026-08-21, branch: content-controls-specs)
- Depends on: -
- Found by: CC-10 follow-up parity sweep, 2026-08-21 (PROGRESS.md)
- Acceptance: for every corpus document and both views, node's
  `new DocumentMapper(doc, cleanView).full_text === extractTextFromBuffer(...)`.
  The guards already exist and are `it.skip`-ed in
  `node/packages/core/src/repro_projection_parity_gaps.test.ts` - un-skip them.
  Python's equivalent (`python/tests/test_repro_projection_parity_gaps.py`)
  already passes and must stay passing.
- Scope: the engines use different separator disciplines. **Python** emits the
  `"\n\n"` block separator BEFORE each block and rolls it back - spans, text
  chunks and offset - when the block turns out to project nothing, tracking it
  with `emitted_any_part` / `emitted_any_block`, and applies this at every level
  of `_map_blocks` (NotesPart header, FootnoteItem, Paragraph). **Node** appends
  the separator AFTER each block and strips trailing ones at the end. Not
  equivalent for blocks that emit only zero-width anchor spans: such a block is
  empty to ingest (`if (part_text)` skips it) but leaves node's loop believing
  it emitted.
- Observed on `wawd_esi_agreement` (clean view): ingest 15,858 chars, mapper
  15,862 - a stray `"\n\n"` between `## Footnotes` and `## Endnotes` plus a
  trailing one. On `on_juries_form1`: a stray leading `"\n\n"` from an empty
  header part, and a spurious `****` after an image alt-text line.
- **Do not half-port it.** Moving only the top-level part loop fixes the leading
  separator and unmasks the trailing one from the notes sections - 2 failing
  documents becomes 4, verified. The discipline has to move through
  `_map_blocks` in one change.
- Severity: a Virtual Text contract violation in node. Ingest and the mapper
  must be byte-identical, or every offset the redline engine computes against
  mapper text is wrong for documents with these shapes. It stayed hidden because
  no test compared node's mapper against node's ingest on a real document - the
  synthetic fixtures never produce empty parts.

## CC-9 â€” P3 seeds: bound dual-write hardening, repeating-section ops, field-labeled diff (P3)

- Status: `blocked` (until CC-6 findings + sample templates)
- Depends on: CC-5, CC-6
- Scope sketch (not yet spec'd â€” write spec-repeating-sections.md before starting):
  repeating-section `insert_item`/`delete_item` intent ops (InsertTableRow precedent),
  bound-store reject-resync policy, `diff_docx_files` hunks labeled with field context,
  n8n batch-fill recipe example.
- CC-6 sharpened the bound-store item: Word RESYNCS the store on reject, and the store
  wins on open â€” so the stale-store risk lives entirely in Adeu's headless reject path,
  and a stale store does not just disagree, it re-applies the rejected value. Scope the
  resync policy around reject, not accept.

## CC-11 â€” Python cannot open a `.dotx` at all (P1, dual-engine parity)

- Status: `in-progress` (agent: opencode-windows, since: 2026-08-21, branch: content-controls-specs)
- Found by: CC-3 (A5.7), 2026-08-21 â€” filed rather than fixed in place because the
  repair has a save-path fidelity question that deserves its own decision
- Depends on: â€”
- Symptom: `python-docx`'s `Document()` rejects the template content type
  `application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml`
  with `ValueError: ... is not a Word file`. Every Python entry point inherits it
  (`ingest`, `RedlineEngine`, `DocumentMapper`, `doc_cache`, `sanitize`,
  `text_revision`), and `adeu extract file.dotx` dies with an **unhandled traceback**
  rather than a teaching error. `@adeu/core` reads the same file fine (7,719 chars on
  `odot_uic_drywell.dotx`), so this is a parity break as well as a product gap â€”
  templates are precisely what a content-controls initiative is for.
- Scope: normalise the template content type at the single choke point every path
  already shares (`strip_bom_from_docx_bytes` in `utils/docx.py`, which is called
  immediately before every `Document(...)`), and **decide the save side**: a normalised
  content type must not leak into the output and silently convert the user's `.dotx`
  into a `.docx`. Surgical mode copies non-patched parts from the sanitized base, so
  this needs checking, not assuming. Add `.dotm`/`.docm` while there (same class).
  At minimum, the CLI must fail with a teaching error instead of a traceback.
- Acceptance: A5.7's open-path half â€”
  `python/tests/test_corpus_validation.py::test_a5_7_dotx_template_opens_through_the_standard_path`
  is a **strict xfail** today, so it flips red the moment this lands and must be
  un-marked in the same PR. Add a synthetic .dotx fixture too: the corpus is optional,
  so a corpus-only guard is no guard on a default CI run.
