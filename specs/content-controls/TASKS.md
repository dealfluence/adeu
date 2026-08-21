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

- Status: `done (d4e967f + 257a5bd)` (agent: opencode-osx, 2026-08-21) — A0.1-A0.4 green
  in both engines. A0.5 moved to A5.0 with Mikko's sign-off (circular dependency on
  CC-3's `corpus_path()`); the test ships here until CC-3 adopts it.
- Depends on: —
- Acceptance: [A0](acceptance/A0-table-sdt-visibility.md) (all examples). **Met:**
  engine fix `61bc00a`+`d4e967f` (merged `845afb3`); acceptance closed by `257a5bd`
  (A0.3 apply half in both engines, fixture aligned to fixture-standard.md) and A0.5.
- Scope: Python traversal must descend into row-level (`sdtContent > w:tr`) and
  cell-level (`sdtContent > w:tc`) content controls in ingest AND mapper (Virtual Text
  contract); Node already behaves correctly — pin it with a test. Include nested
  SDT-in-SDT rows and `w15:repeatingSectionItem`-wrapped rows. Visibility only — no
  edit-semantics changes. Regression file: `python/tests/test_repro_sdt_table_row_cell_invisibility.py`.
  **Correction:** "Node already behaves correctly" held only for row/cell sdts —
  `docx/primitives.ts` leaked nested-table rows/cells via recursive
  `getElementsByTagName`; fixed in the same merge. A0.3 also required apply semantics,
  not visibility alone.
- Note: a prepared task chip with the full repro exists (filed 2026-08-21); this row is
  the authoritative tracker. Ship independently of P1 — this is a straight bug.
- Downstream must-reads: CC-1 — Node's traversal was *not* sound before this work, don't
  assume it; Python and Node still disagree on emphasis-marker coalescing. CC-3 — three
  non-sdt corpus parity gaps will fail A5.1's identical-counts assertion, incl. Python
  emitting literal `<w:br w:type="page"/>` into the projection (PROGRESS.md).

## CC-1 — Projection: sdt events, anchors, flags, checkboxes, placeholder bubbles (P1)

- Status: `done` (2026-08-21) — all five in-scope sub-items landed across both agents;
  1f reassigned to CC-2 by Mikko. See the completion note under the decomposition.
- **Decomposition** (osx, 2026-08-21). CC-1 is multi-session; slicing it so the two
  agents can work in parallel without fighting over the same files. I am taking
  **1a then 1b** (they are one design and splitting them would mean writing the event
  payload twice). The rest are independently claimable once 1a lands — claim by
  editing the sub-item, not this Status line:
  - **1a — foundation** (`done (38444d2)`, osx): classification + ordinal pre-pass
    landed in both engines, 26 tests each against the fixture-standard table; the
    16-control fixture body is now ONE file (`shared/fixtures/cc_fixture.body.xml`)
    read by the script and both suites. Events/plumbing are 1b. Original scope: shared `classify_sdt` helper (class, flags,
    alias, tag, placeholder, options) + the ordinal pre-pass + `sdt_start`/`sdt_end`
    events from `traverse_node` and the block iterators, both engines. Per
    spec-projection.md §9 the ordinal pre-pass MUST be one shared helper consumed by
    ingest AND mapper, so the Virtual Text contract holds by construction rather than
    by two implementations agreeing.
  - **1b — anchored leaves + groups + clean view** (`done (eb0a141)`, osx): A1.1-A1.5,
    A1.10. Inline half `5b1454a`, block/group/table half `eb0a141`. `sdt_start`/`sdt_end`
    events through both engines' traversal; block-level controls surfaced as one
    undescended `BlockSdt` from `iter_block_items(..., emit_sdt=True)` (opt-in, so
    outline/domain/sanitize keep the sdt-transparent behaviour); row/cell controls
    resolved with `wrapping_sdt` rather than by changing the CC-0 iterators.
    A1.1/A1.2 now assert the full golden read out of `fixture-standard.md`, exact but
    for one explicit substitution: the CC:6 checkbox glyph, which is **1c's** to
    remove. Verified 14/14 projections byte-identical py↔node with zero mapper drift.
    Two node-only defects found by that comparison and fixed here (hyperlinks losing
    their OPC part inside block controls; see PROGRESS.md).
  - **1c — checkboxes** (`done (7ab331f)`, agent: opencode-windows, 2026-08-21): A1.8.
    Both engines, ingest and mapper, both views; 9 tests each side asserting the same
    strings; the CC-1b golden shim for CC:6 is deleted, so A1.1/A1.2 are now exact for
    all 16 controls. Three findings worth carrying forward, all in PROGRESS.md and
    folded into spec-projection.md §4 with Mikko's sign-off:
    **(i)** a checkbox is not always inline — 11 of `odot_uic_drywell`'s 19 are
    cell-level (`w:sdt` parented by `w:tr`, wrapping a whole `w:tc`) and never reach the
    sdt branch, so both engines substitute at **run emission**, the one point every path
    passes through;
    **(ii)** the corpus sizes moved and one moved DOWN — 13 of those 19 control glyphs
    arrived wrapped in `**`, and dropping those markers outweighs the token widening,
    net −14 on that document; attributed to the character in both parity tables;
    **(iii)** the node `golden()` helper was broken on every Windows checkout (CRLF
    against a `\n```\n` fence match), so A1.1/A1.2 were unverifiable here while staying
    green on macOS — fixed in the same commit, and `ccFixtureBodyXml()` hardened against
    the latent form of it. Original scope follows.
    Needs 1a only. Taking the COM + corpus reconnaissance half FIRST, while 1a is in
    flight: what glyph/font pairs Word actually writes, whether `w14:checked` and the
    glyph can disagree, and whether legacy `FORMCHECKBOX` (`w:fldChar`+`w:ffData`) is in
    the corpus at all. §4 of spec-projection.md assumes `w14:checkbox`; if the wild is
    mostly legacy form fields then A1.8 as written misses most real checkboxes, and that
    is worth knowing BEFORE the projection is wired, not after. Touches tests + spec
    only until 1a lands, so it cannot collide with osx's `classify_sdt` work.
    **Recon done** (2026-08-21, PROGRESS.md): §4 confirmed implementable as written,
    no amendment needed. Corpus is 100% `w14:checkbox`, zero legacy form fields, all
    `MS Gothic` 2612/2610, and **not one of ~7,700 is ticked** — the corpus cannot
    test `[x]` at all. Word rejects-restore `w14:checked` (licenses projecting from
    the attribute), writes the glyph as literal `w:t` not `w:sym` (licenses the
    mapper's width accounting), and *refuses* prose inside a checkbox with no lock
    set (so A3.8 reproduces Word rather than inventing policy). Trap for the wiring:
    `odot_uic_drywell` has 2 bare `☐` in prose outside any control that must stay
    `☐` — substitute on the character and you invent two checkboxes. Still to do:
    the projection itself, both engines, once 1a lands.
  - **1d — chrome-stripping protection** (`done (a576f34)`, osx): A1.6. The marker
    STRIPPERS already protected `{#...}` (QA 2026-07-23 F4) and that covered `{#cc:N}`
    unchanged — pinned, not rewritten. The real gap was the two passes that CUT text
    and knew nothing about tokens: outline truncation at 200 chars and the search
    radius ladder both sliced through an anchor and emitted `{#cc:`. Fixed in both
    engines (outline drops a token that will not fit; snippet windows widen to the
    token edge). Pre-existing for `{#_Ref…}` bookmark anchors too — CC-1 only made it
    likely. Suites: `test_cc_anchor_chrome_protection.py` (38),
    `cc_anchor_chrome_protection.test.ts` in core (9, outline) and mcp-server
    (10, search); each verified to FAIL against the unfixed code.
  - **1e — anchor fabrication refusal** (`done (c532d5b)`, osx): A1.7. Two of the three
    named cases were ALREADY refused by VAL-OBS-9 (fabrication and flag-rewriting), which
    counts anchors that gained copies. The hole was deletion: that loop iterates
    `new_text`'s anchors, so a target covering `{#/cc:3}` whose `new_text` omits it had
    nothing to iterate and passed, unbalancing the pair. Now an ORDERED comparison of the
    `cc` anchors in target vs new_text, scoped to `cc` so the two deliberate targeting
    surfaces stay open — `{#cell:paraId}` empty-cell writes and the empty pair
    `{#cc:N}{#/cc:N}` (spec-projection §3 surface #1, which CC-4/CC-5 build the
    text-first fill on). Both carve-outs are pinned. Suites:
    `test_cc_anchor_fabrication_refusal.py` (17) and its node twin (17); both fail 10/17
    against the unfixed code.
  - **1f — banner** (`moved to CC-2`, Mikko 2026-08-21): A1.9. Confirmed: it touches the
    CLI and both MCP servers, which is CC-2's surface exactly, and doing it here would
    mean visiting those three surfaces twice. **A1.9 is now CC-2's acceptance, not
    CC-1's** — CC-1 does not wait on it.
- **CC-1 is complete** (`done`, 2026-08-21): 1a `38444d2`, 1b `eb0a141`, 1c `7ab331f`,
  1d `a576f34`, 1e `c532d5b`; 1f reassigned to CC-2 above. This unblocks CC-2, CC-4,
  CC-5 and the CC-1-dependent half of CC-3.
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

- Status: `done (224aefc)` (agent: opencode-osx, 2026-08-21) — A2.1-A2.7 and A1.9 all
  met, both engines, all surfaces. **Two frozen-spec deviations, both recorded below
  and needing Mikko's ruling** (README rule 4); neither blocks the row, and both were
  resolved toward the implemented convention rather than the literal text.
  - **§1 `--json` shape.** The spec says the CLI wraps the ledger as `{"content": …}`
    "per CLI stream conventions". There is no `{"content": …}` anywhere in the
    codebase: every CLI mode emits `{markdown, title, file_path}`, pinned by
    `test_cli_features.py:569`. Followed the real convention — obeying the literal
    text would make `--mode fields` the only mode with a different JSON shape, which
    hurts exactly the batch surfaces §7 exists to serve.
  - **A2.7 `fields_offset` type.** §1 says it "mirrors `changes_offset`" AND that it
    "publishes `type: number`". It cannot do both: `changes_offset` publishes
    `integer`. Kept parity, and wrote the assertion AS parity so it tracks
    `changes_offset` if that ever moves. What A2.7 actually guards — no `anyOf`/
    `oneOf` union — is asserted separately.
- Depends on: CC-1 (`done`)
- Acceptance: [A2](acceptance/A2-fields-ledger.md) (all examples), **plus A1.9 (the
  protection/fields banner), reassigned here from CC-1f by Mikko 2026-08-21** — it
  touches the CLI and both MCP servers, which is this task's surface exactly.
- Scope: `read_docx(mode="fields")` + `fields_offset` pagination (MCP, both servers),
  `adeu extract --mode fields` (CLI), appendix "Content Controls" summary block,
  protection/fields banner on full view. Line format per spec-fields-ledger.md, exact.

## CC-3 — Corpus fetch mechanism + corpus validation tests (P1)

- Status: `done` (agent: opencode-windows, 2026-08-21) — mechanism, helpers, CI job and
  the **pre-CC-1 subset** of A5 are done and green in both engines. **Split by Mikko,
  2026-08-21:** the dependent tail is now **CC-3b** below rather than holding this row
  open for the whole initiative. A row that sits in `review` until every other task
  lands is a poor signal — the mechanism this task was actually about is finished.
  Spun out: **CC-11** (Python cannot open a .dotx — A5.7 is a strict xfail until fixed).
- Depends on: CC-0 (cell-level counts require Python parity) — `review`, engine fix merged
- Acceptance: [A5](acceptance/A5-corpus-validation.md) — A5.9 done; A5.1/A5.7/A5.8 done
  for their pre-CC-1 halves; A5.2/A5.3/A5.4/A5.5/A5.6 blocked on ledger, gates, set_field
  and anchors respectively
- Scope: `scripts/fetch_corpus.py` + `shared/corpus/manifest.json` are committed by this
  initiative's bootstrap; this task wires test helpers (`corpus_path()` skip-if-missing
  fixture in `python/tests/utils.py`; `corpusPath()` in `node/packages/core/src/test-utils.ts`),
  implements the A5 invariant tests, and adds an optional CI job (manual trigger /
  `ADEU_FETCH_CORPUS=1`) that fetches and runs them.

## CC-3b — The dependency-blocked tail of A5 corpus validation (P1)

- Status: `pending` — split out of CC-3 by Mikko, 2026-08-21, so CC-3's finished
  mechanism could close instead of tracking other people's work.
- Depends on: CC-2 (A5.2 ledger), CC-4 (A5.3 gates), CC-5 (A5.4 `set_field`),
  CC-1 (A5.5 anchors — now `done`, so A5.5 is claimable immediately), A5.6
- Acceptance: the A5 examples CC-3 could not reach — A5.2, A5.3, A5.4, A5.5, A5.6.
  Per-blocker detail is in PROGRESS.md under CC-3.
- Scope: no new mechanism. `corpus_path()` / `corpusPath()`, the skip-if-missing
  fixtures and the optional CI job all exist; this row is purely the remaining
  invariant tests, each landing as its blocker clears.
- Note: **A5.1's identical-counts assertion is still blocked** on the two known
  python/node divergences (emphasis-marker coalescing, and header lines node projects
  that python omits) — see CC-10's closing note, which measured that closing the page
  break gap alone flipped the sign rather than closing it.

## CC-4 — Write gates: locks, groups, document protection, placeholder targets (P2)

- Status: `done` (agent: opencode-windows, 2026-08-22, branch:
  content-controls-specs). Commits: identity `56aabeb` + `808e829`, protection state
  `34330d9`, python gates `6836cb5`, node gates + surfaces `71d5ce4`, widening +
  COM agreement + spec corrections in the closing commit. Taken on the Windows side
  deliberately: this row was mostly questions about what Word actually permits, and
  the answers are COM-checkable here.
- **G10 and G12 are NOT part of this row** and were not implemented: both gate
  `set_field` values, and `set_field` does not exist in either engine — it is CC-5.
  They are in the spec-gates §2 matrix for completeness but have no operation to
  gate until CC-5 lands. Recorded on A3 and on CC-5 below.
- Three findings that amended the acceptance doc (all written up in A3 and
  PROGRESS.md): **A3.5** contradicted spec-gates §1a and §1a won (the forms-protected
  fills are additionally gated on `allow_untracked_writes`, which A3.5 predated);
  **A3.11** is refused by CC-1e's anchor gate before G15 ever sees it, so G15's real
  job is the *unanchored* walls; **A3.10** was already satisfied by
  `trim_common_context`, so what was actually missing was the disclosure, not the
  segmentation.
- COM agreement is pinned, not assumed: `test_live_word_gate_agreement.py` drives
  real Word over the same document Adeu gates and asserts the two verdicts match,
  including that `ignore_control_locks` lands where Word lands once the lock is
  cleared. Previously CC-6 measured Word and A3 pinned Adeu with nothing connecting
  them, so a gate could be changed into disagreeing with Word and both suites would
  have stayed green.
- Depends on: CC-1 (`done`)
- Acceptance: [A3](acceptance/A3-gates.md) (all examples)
- Scope: load-time protection state; gate matrix + teaching-error contracts per
  spec-gates.md; `ignore_control_locks` / `ignore_document_protection` batch params
  (MCP both servers, CLI flags `--ignore-control-locks` / `--ignore-document-protection`);
  boundary auto-segmentation at control walls; block-merge refusal across SDT boundaries;
  review-action gating per spec — resolved by CC-6: G9 is now *allow* (Word permits
  Accept/Reject inside locked controls), G7 confirmed.
- **G5 resolved by Mikko, 2026-08-21** (spec-gates.md §1a). Under `edit="forms"` Word
  writes the permitted fills untracked and *reading* `TrackRevisions` throws, so Adeu's
  "always tracked" contract is unenforceable there. Decision: **refuse by default, with
  an explicit per-batch opt-in.** A new `allow_untracked_writes` param (schema default
  `false`; CLI `--allow-untracked-writes`) unlocks G5 only; without it the write is
  rejected with a teaching error naming the cause, and with it every untracked write
  carries its own report note. Deliberately NOT folded into
  `ignore_document_protection`: that bypasses a gate the author set, whereas this
  accepts a downgrade of Adeu's own output guarantee, and the G5 writes in question are
  ones Word itself permits — no protection is being ignored. This task's error contracts
  are now final.

## CC-5 — `set_field` change type + fill semantics (P2)

- Status: `in-progress` (agent: opencode-osx, since: 2026-08-22, branch:
  content-controls-specs) — **started while CC-4 is still in flight, deliberately.**
  The dependency is real but narrow: only A4.12 (set_field is refused by G1 like any
  other edit) needs the gate matrix. Everything else — the union member, field
  resolution, fill semantics, the per-class writers, the surfaces — is independent,
  and CC-4 has already landed the two pieces of plumbing this row actually consumes
  (`read_document_protection`, and `sdt_stack` on every run, 56aabeb). Sequencing
  strictly behind CC-4 would idle this side for no gain and leave CC-7, which depends
  on BOTH, waiting on a serial chain.
  Coordination: this row does not touch the gate matrix or `ignore_*` params — those
  are CC-4's. `set_field` calls whatever gate entry point CC-4 publishes; until it
  exists the call site is one function, and A4.12 lands as the last commit of this row.
- Depends on: CC-4 (A4.12 only, see above); CC-6 findings must be recorded before this
  task's PR merges — done, CC-6 is closed and `test_live_word_content_controls.py` pins
  each finding
- **CC-4 closed 2026-08-22** (`b5d6801`), so the A4.12 dependency is now satisfied and
  the gate entry point exists: `RedlineEngine._check_control_gates` in both engines,
  built on `python/src/adeu/redline/gates.py` / `node/packages/core/src/gates.ts`.
- **Inherits G10 and G12 from CC-4.** Both gate `set_field` values (dropdown
  `listItem` membership; date format matching ISO or the control's `w:dateFormat`),
  so they could not be implemented before the operation existed. The gate module and
  its four-part error contract are in place in both engines
  (`python/src/adeu/redline/gates.py`, `node/packages/core/src/gates.ts`); these two
  are new functions in it plus a call from the `set_field` handler.
- **The G13 sibling to be aware of:** CC-4 refuses `ModifyText` on bound controls and
  points the caller at `set_field`. That error is now a promise this row has to keep —
  `set_field` is the sanctioned path for bound content, so its dual-write (or the
  reject-gate below) is what makes CC-4's advice true rather than a dead end.
- **Scope grew, Mikko 2026-08-21: the bound-control reject path is now v1, not CC-9.**
  CC-6(e) found that Word rewrites a bound control's content from its XML store on open,
  and that a headless reject leaves the store holding the *rejected* value — so the next
  open silently re-applies what the user rejected. A reject that undoes itself, with no
  error. That is the same silent-wrong-output class as CC-14, which is treated as P1, so
  parking it in a P3 row was not defensible. Minimum for v1, in this task: either
  dual-write the store on reject, or — cheaper and sufficient — **gate it**, refusing
  `RejectChange` inside a data-bound control with an error that names the reason. The
  richer resync policy and repeating-section work stay in CC-9.
- Acceptance: [A4](acceptance/A4-set-field.md) (all examples)
- Scope: `set_field` in the DocumentChange union + flat MCP schema (both engines),
  resolution order CC-ordinal → tag → alias, `match_mode` reuse; per-type semantics
  (text/richtext/dropdown/combobox/date/checkbox), placeholder-clearing fill,
  `w:temporary` unwrap, bound-field policy per spec-set-field.md; CLI changes-file
  support; per-edit report `field:` notes.

## CC-6 — Word COM verification battery (P2, informs CC-4/CC-5)

- Status: `done` (agent: opencode-windows, 2026-08-21) — findings landed, 18 COM-backed
  tests green, and both sign-off items answered by Mikko on 2026-08-21: **G5** resolved
  as refuse-by-default plus an `allow_untracked_writes` opt-in (recorded on CC-4 and in
  spec-gates.md §1a), and the **bound-store reject-resync** pulled out of CC-9 into CC-5
  as a v1 requirement (recorded on CC-5). Nothing in this row is outstanding.
- Depends on: CC-1 (fixture builders reused) — started ahead of it; deviation disclosed
  in PROGRESS.md. Ran on Windows + real Word (16.0)
  (`python/tests/word_com.py` harness, `xdist_group("live_word")`)
- Acceptance: findings appended to PROGRESS.md and every `[COM-PENDING]` spec section
  resolved (amended or confirmed) — this task's deliverable is *knowledge*, pinned as
  COM-backed tests where feasible. **Met:** all `[COM-PENDING]` tags removed from
  spec-gates.md, spec-set-field.md and A4; 15 COM-backed tests in
  `python/tests/test_live_word_content_controls.py`.
- Downstream must-reads: CC-1 — `w:showingPlcHdr` is the only reliable placeholder
  signal and placeholder prose resolves through the glossary part. CC-4 — G9 is now
  *allow*, G7 confirmed, G5 has an open sign-off question. CC-5 — checkbox is ins-then-
  del, `w:temporary` unwraps on any edit, bound controls are store-authoritative.
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

## CC-10 — Python leaks raw OOXML into the text projection (P1, parity + output quality)

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
  offsets are intact — this is an output-quality and parity defect, not corruption, but
  an LLM reads the markup as prose and a `target_text` spanning the break must include
  the XML.
- **Corrected diagnosis (2026-08-21, was mis-scoped when filed as "a leak").** This is
  not accidental serialization. `_PAGE_BREAK_TOKEN` (`utils/docx.py:95`) is a deliberate
  **in-band sentinel**, and it is load-bearing: `pagination.py:262-272` splits blocks on
  it to honour manual page breaks. Node has no equivalent — `pagination.ts:131` is
  density-only and ignores manual breaks. So the engines diverge in two coupled places,
  and deleting the token alone would regress
  `test_cli_bug_repro.py::test_manual_page_breaks_pagination` / `_outline` and lose a
  real capability. `docs/FIDELITY.md:36` already settles the projection question —
  "Both project as a newline" — so **python violates its own documented contract** and
  node is correct. Fix = emit `\n` and route the page-break signal **out of band**
  (offsets alongside the text), preserving python's pagination.
- Three python sites duplicate the branch chain and must move together
  (`utils/docx.py:944`, `:1179`, `:1204` — the third inlines the literal instead of the
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
    manual-page-break pagination — `test_cli_bug_repro.py` pagination/outline tests go
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

- Status: `done (121f0a6)` (agent: opencode-osx, 2026-08-21) - separator discipline
  ported through `_map_blocks` and the part loop in one change; the 4 corpus
  ingest-vs-mapper guards are un-skipped and green. Scope grew by two divergences
  the corpus could not reach, both now pinned in section 5 of the parity suites:
  node's **ingest** was missing the clean-view skip for a deleted paragraph mark
  (projected `"Alpha\n\n\n\nBeta"` where python projects `"Alpha\n\nBeta"`), and
  node's mapper emitted style markers for empty styled runs (dangling `****`
  after a drawing or footnote reference). Note for later readers: on the
  deleted-mark case node's ingest and mapper were consistently wrong TOGETHER,
  so the contract test agreed with itself and stayed green - only cross-engine
  comparison caught it. 10/10 views byte-identical python<->node.
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

## CC-13 — The live-Word suite is nondeterministic and blocks `git push` (P1, tooling)

- Status: `done` (agent: opencode-windows, 2026-08-21; `e33a615` + `c1f1adf`) — closed by
  Mikko's call on 2026-08-21: **the live-Word suite stays in the pre-push hook and the
  residual flakiness is accepted for now**, as out of scope for the content-controls
  work. Quarantining (option 4) was offered and declined. What landed is option 2 done
  properly: measured 15 consecutive runs of all 43 live-Word tests with a deliberately
  poisoned Word, zero failures, against a baseline that failed >50% of the time. The
  honest residual below stands — roughly one uncharacterised failure in ~21 runs, and a
  4x slowdown when strays accumulate — and options 1 and 3 remain the principled fixes
  for whoever needs them. Recorded, not hidden.
  Found while verifying CC-1c. Taken ahead of CC-1c's
  implementation half for two reasons: 1c's wiring lands in the same traversal code
  osx is editing for 1b right now, and this row is what makes any Windows push
  reliable, mine included.
- Depends on: — (pure test infrastructure; no spec surface)
- Symptom: `python/tests/test_live_word*.py` pass or fail depending on nothing the
  test author controls. Same commit, same machine, same Word 16.0: consecutive runs
  of `test_live_word_structured_insertion.py` gave `5 passed`, then `3 failed`, then
  `5 failed`, then `5 passed`. Because `.githooks/pre-push` runs `uv run pytest`, this
  **blocks pushing** at random, on Windows, for changes that have nothing to do with
  Word.
- **Not caused by the content-controls work.** Verified in a detached worktree at
  `f3aadb7` — before CC-1c's COM tests and before any conftest change — where
  `test_live_word_structured_insertion.py` failed 5/5 and a combined live-Word run
  gave `4 failed, 21 passed` then `25 passed`. Pre-existing.
- Diagnosis so far: the failures are **cross-document contamination**, not wrong
  assertions. A failing test reports text belonging to a different test file
  (`assert '{++Title++}' in 'Initial {==manuscript==}...'`, which is
  `test_live_word.py`'s fixture document). The tools under test resolve Word through
  `GetActiveObject` and then read `app.ActiveDocument`, so every such test is really
  asserting about whichever document Word considers active at tool-call time. The
  `active_word_app` and session-scoped `word_app` fixtures deliberately share ONE Word
  instance, and `word_com.edit_and_save` opens and closes documents in it freely; when
  a document closes, Word re-activates one of its choosing.
- Partial fix already landed (CC-1c commit): `active_word_app` now calls
  `doc.Activate()` — it previously called only `app.Activate()`, which raises the
  *application*, not the *document* — plus a guard that fails with a named error
  listing the open documents when `ActiveDocument` is not the fixture's own. With a
  deliberately poisoned Word (a stray document left open and activated) the suite went
  from failing to `5 passed`. **This is a mitigation, not the fix:** runs still fail
  intermittently and the guard never fires, which means activation is correct at
  fixture setup and is being lost *later*, between setup and the tool's
  `GetActiveObject` call.
- **Progress 2026-08-21 (windows).** Two mitigations landed, one approach tried and
  reverted, root cause still open. Failure rate on
  `test_live_word_structured_insertion.py` went from 5/5 and 3/5 failing to 0-1/5,
  which is better and is not fixed.
  - Landed: `doc.Activate()` (the fixture previously activated only the
    *application*), and `_await_active_document`, which verifies the claim through
    `GetActiveObject` — the production lookup path — instead of through the
    fixture's own `Dispatch` handle, and retries, because activation is
    asynchronous. Checking the `Dispatch` handle verifies the wrong object: with
    two `WINWORD.EXE` processes alive the two calls resolve to *different*
    applications.
  - **Tried and reverted — do not re-attempt without reading this.** Closing every
    document that appeared during a test (to stop the tools' un-closed
    `_get_word_doc` opens from accumulating) makes things *worse*: the tools hand
    back Ranges into those documents, so reaping them produces
    `(-2147417848) The object invoked has disconnected from its clients` and
    `Object has been deleted`. That is a worse failure than the one being fixed,
    because it reads as a COM fault rather than a test-isolation problem.
  - Still open: documents genuinely do accumulate, and the guard has never fired,
    so activation is correct when the fixture yields and is lost afterwards. The
    remaining suspect is that the *tools* change the active document mid-test
    (`_get_word_doc` opens by path) and nothing re-establishes it before the next
    assertion.
- **Measurement 2026-08-21 (windows), 43 tests across all three live-Word files.**
  **15 consecutive runs, zero failures, every one of them with 4 stray documents
  left open and activated in Word** — the poisoned half of the acceptance
  criterion, exceeded. Baseline for comparison: the same suites previously gave
  `5 passed` / `3 failed` / `5 failed` / `5 passed` on an unchanged commit, and a
  poisoned Word failed immediately.
- **The retry loop is doing real work, and the clock proves it.** Cold Word: 29s.
  With 4 strays open: 103s, reproducibly, scaling with the stray count. That 74s is
  `_await_active_document` losing the race and re-activating — i.e. contamination
  is still happening on every run, and is now being *corrected* rather than
  silently mis-measured. This is the useful diagnostic: **document accumulation now
  costs time instead of correctness.** A 4x slowdown in a hook is a fair trade for
  a suite that no longer lies, but it also means the accumulation problem itself
  (the reverted reaping approach above) is unfixed, merely defanged.
- **Residual, stated plainly: one uncharacterised failure in ~21 runs.** It
  occurred mid-streak and was not captured; 12 subsequent runs with output capture
  armed failed to reproduce it. So this is `review`, not `done`: the *observed*
  rate is ~5% of runs, down from >50%, which is the difference between "pushing is
  a coin flip" and "pushing works". Whoever closes this row should either catch
  that failure or run long enough to argue it away. Options 1 and 3 below remain
  the principled fixes; what landed is option 2, done properly.
- Scope: make the live-Word suite deterministic. Options worth weighing, roughly in
  order of appeal:
  1. Give the live-Word tools an injectable Word/document handle for tests, so they
     stop resolving through `ActiveDocument` at all. Removes the shared-state race by
     construction; largest change, and it alters production code for testability.
  2. Re-activate the fixture's document immediately before each tool call (a helper
     the tests route through), narrowing the window rather than closing it.
  3. Give the `word_app` battery its OWN Word instance so `Documents.Open` traffic
     cannot disturb the `active_word_app` tests. Note the existing docstring's reason
     for sharing — attaching to a developer's running Word — which this would change.
  4. Failing all that, quarantine: mark them `flaky`/opt-in and take them out of the
     pre-push hook, so a known-nondeterministic suite stops gating unrelated work.
- Acceptance: 10 consecutive full-suite runs on Windows with zero live-Word failures,
  and the same 10 with a deliberately poisoned Word (stray document open and active).
- Note for whoever takes it: `Stop-Process -Name WINWORD -Force` between runs changes
  the outcome, which is itself evidence that the state lives in the Word instance
  rather than in the tests.
## CC-14 - Redline replay silently produces the wrong document (P1, correctness)

- Status: `done (c099de9)` (agent: opencode-osx, 2026-08-21) - TWO independent pre-existing
  defects, not one; the property search had only ever reached the first. Both silent
  (`edits_skipped == 0`, no error, wrong document), both fixed in this row, and the
  falsifying examples pinned as explicit regression tests in both engines because
  `.hypothesis` is gitignored and the 25-example default profile does not rediscover
  them. Verified: hunt profile (300 examples/property) green, and each new suite fails
  against the unfixed engines (6/11 python, 5/12 node).
  - **Defect 1 (python only, so also a parity break).** The rstrip "Smart Fallback" in
    `_resolve_single_match` preserved the target's trailing whitespace by inserting the
    replacement BEFORE it. Right for a separator space inside one paragraph
    ("Section 1 " -> "Section 1 Revised" must not glue the next word on), wrong the
    moment the replacement introduces a paragraph break: the space is then stranded at
    the START of the new paragraph, which is the reported `'0.\n\n 0.'`. Guarded to
    fall through to the F1 rule, which already handles that shape atomically. `@adeu/core`
    has no such branch and was already correct; the correct behaviour is now pinned there.
  - **Defect 2 (both engines).** A paragraph mark shared by target and replacement reached
    the apply layer, which track-deletes a target's trailing mark (a genuine merge
    "A.\n\n" -> "Z." depends on that) but never re-creates the one the replacement asks
    for. Exactly one break vanished. `trim_common_context` is word-boundary aware and
    will not trim across "\n\n", so the span arrived whole. Now normalised by
    `_trim_shared_trailing_paragraph_mark` / `trimSharedTrailingParagraphMark` at BOTH
    entry points to the apply layer - the resolution path and the caller-pinned path,
    which skips resolution entirely. The second is not hypothetical: widening a target
    for uniqueness produces this shape with a pinned index in 129 of 4,000 randomised
    paragraph edits, and only the JSON round trip (which drops the index) was hiding it.
- Found by: opencode-osx, 2026-08-21, during CC-1b verification. **Pre-existing** -
  reproduced on a stashed clean tree, unrelated to content controls. Hypothesis had
  simply never generated this example before and cached it mid-session.
- Reproduce: `cd python && uv run pytest tests/test_property_invariants.py::test_p2_json_text_roundtrip_is_exact_or_loud -n 0`
  Falsifying example: `data=(['0 0.'], ['0.', '0.'])` - i.e. one paragraph `"0 0."`
  edited into two paragraphs `"0."` and `"0."`.
- Symptom: the batch applies cleanly (no `BatchValidationError`, `edits_skipped == 0`)
  and the accepted output is `'0.\n\n 0.'` where the requested text was
  `'0.\n\n0.'` - a stray leading space on the second paragraph.
- Why it matters more than the character count suggests: this is the *silent* failure
  mode. The property is called `..._is_exact_or_loud` precisely because the engine is
  allowed to refuse an edit but is NOT allowed to accept it and produce something
  different. A caller replaying JSON edits gets a document that differs from what it
  asked for, with no error to act on. Every other outcome in that test is a clean
  refusal.
- Likely area: paragraph-split handling in the alignment-generated edits - the split
  point appears to keep the separating space with the tail rather than consuming it.
  See `generate_edits_via_paragraph_alignment` and the split path in
  `redline/engine.py`.
- Note: `.hypothesis` is gitignored, so CI will NOT reproduce this deterministically.
  Whoever takes it should pin the example as an explicit regression test rather than
  relying on the property search to rediscover it.

## CC-9 — P3 seeds: bound dual-write hardening, repeating-section ops, field-labeled diff (P3)

- Status: `blocked` (until CC-6 findings + sample templates)
- Depends on: CC-5, CC-6
- Scope sketch (not yet spec'd — write spec-repeating-sections.md before starting):
  repeating-section `insert_item`/`delete_item` intent ops (InsertTableRow precedent),
  bound-store reject-resync policy, `diff_docx_files` hunks labeled with field context,
  n8n batch-fill recipe example.
- CC-6 sharpened the bound-store item: Word RESYNCS the store on reject, and the store
  wins on open — so the stale-store risk lives entirely in Adeu's headless reject path,
  and a stale store does not just disagree, it re-applies the rejected value. Scope the
  resync policy around reject, not accept.
- **Descoped by Mikko, 2026-08-21: the reject path itself is no longer this row's.** It
  moved to **CC-5** as a v1 requirement (fix or gate), because shipping a silent
  re-application of a rejected value is the CC-14 failure class and does not belong
  behind a P3 gate. What remains here is the richer policy — full dual-write hardening,
  resync on accept, and the repeating-section operations.

## CC-11 — Python cannot open a `.dotx` at all (P1, dual-engine parity)

- Status: `review` (agent: opencode-windows, 2026-08-21) — `.dotx`/`.dotm`/`.docm` open in
  Python, save preserves the flavour, A5.7 un-xfailed, corpus parity table extended to
  the 5th document. See PROGRESS.md 2026-08-21 (CC-11).
- Found by: CC-3 (A5.7), 2026-08-21 — filed rather than fixed in place because the
  repair has a save-path fidelity question that deserves its own decision
- Depends on: —
- Symptom: `python-docx`'s `Document()` rejects the template content type
  `application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml`
  with `ValueError: ... is not a Word file`. Every Python entry point inherits it
  (`ingest`, `RedlineEngine`, `DocumentMapper`, `doc_cache`, `sanitize`,
  `text_revision`), and `adeu extract file.dotx` dies with an **unhandled traceback**
  rather than a teaching error. `@adeu/core` reads the same file fine (7,719 chars on
  `odot_uic_drywell.dotx`), so this is a parity break as well as a product gap —
  templates are precisely what a content-controls initiative is for.
- Scope: normalise the template content type at the single choke point every path
  already shares (`strip_bom_from_docx_bytes` in `utils/docx.py`, which is called
  immediately before every `Document(...)`), and **decide the save side**: a normalised
  content type must not leak into the output and silently convert the user's `.dotx`
  into a `.docx`. Surgical mode copies non-patched parts from the sanitized base, so
  this needs checking, not assuming. Add `.dotm`/`.docm` while there (same class).
  At minimum, the CLI must fail with a teaching error instead of a traceback.
- Acceptance: A5.7's open-path half —
  `python/tests/test_corpus_validation.py::test_a5_7_dotx_template_opens_through_the_standard_path`
  is a **strict xfail** today, so it flips red the moment this lands and must be
  un-marked in the same PR. Add a synthetic .dotx fixture too: the corpus is optional,
  so a corpus-only guard is no guard on a default CI run.
- Resolution: the Scope above proposed the WRONG repair, and the save-side question it
  flagged is what exposed it. Normalising the content type on load does leak: `python-docx`
  serialises `[Content_Types].xml` from the parts' own `content_type`, so the rewrite
  rides along into `save()` and turns the user's template into a document — fidelity
  lost by a *read*. Instead `adeu/utils/opc.py` registers the template and macro-enabled
  content types against `DocumentPart` in `PartFactory.part_type_for`, so the part
  becomes a real `DocumentPart` while keeping its own content type. `.dotx` in, `.dotx`
  out, and the save-side question disappears rather than being traded off. All nine
  `docx.Document` call sites now import `adeu.utils.opc.load_document`.

## CC-15 — Node reports outline headings from a bare `w:outlineLvl`; Python does not (P3)

- Status: `pending` — filed by opencode-osx, 2026-08-21, while building CC-1d's outline
  fixture. **Pre-existing**, unrelated to content controls; found because the two
  engines needed different fixtures to produce the same heading.
- Depends on: —
- Symptom: for a paragraph carrying `<w:outlineLvl w:val="0"/>` and NO heading `pStyle`,
  `@adeu/core` returns one outline node (style `"(outline_level)"`, `outline.ts:538`)
  and `adeu` returns none. Word treats such a paragraph as an outline-level heading, so
  Node is right and the Python outline silently omits real headings.
- Cause: not a logic bug — `python-docx` 1.2 has no `paragraph_format.outline_level`,
  the `AttributeError` is swallowed by the existing try/except, and step 1 of
  `_determine_heading_style` is therefore dead code. This is already documented at
  `python/src/adeu/outline.py:1102-1109` and pinned INTERNALLY by
  `tests/test_outline_fast_equivalence.py` (fast mirror vs original). What is not
  tracked anywhere is the CROSS-ENGINE consequence, which is what this row is for.
- Scope: read `w:outlineLvl` off the `pPr` element directly instead of through the
  missing python-docx property, in both `_determine_heading_style` and its
  cache-backed `_fast` mirror. Note the fast mirror's docstring promises "identical
  observable behavior", so both must move together or that pin fails.
- Acceptance: a paragraph with only `w:outlineLvl` yields the same outline node in both
  engines. Worth a corpus sweep first to size the blast radius: adding headings changes
  every outline consumer, so this is not obviously a safe drive-by.

## CC-16 — The LibreOffice interop harness reports scheduling, not documents (P1, tooling)

- Status: `done (dcd769a)` (agent: opencode-osx, 2026-08-21, branch: content-controls-specs)
  — found while verifying CC-14; filed and fixed rather than left as a note, because
  the quiet half of it means QA C1/H4 interop coverage mostly was not running.
- Depends on: — (pure test infrastructure; no spec surface)
- Symptom: `test_repro_qa_2026_07_18.py::TestC1FooterBoundary::test_applied_output_loads_in_libreoffice`
  and `TestH4FootnoteInterop::test_footnote_edit_output_loads_in_libreoffice` fail
  intermittently under `pytest -n auto` — a different one each run, sometimes neither,
  55/55 serially. Reproduced on a clean tree, so pre-existing.
- Cause: **concurrent `soffice` invocations that share a user profile do not both
  convert.** The second finds the first one's lock, hands its request over, and exits
  **0 having written nothing**. `lo_loads` only checks that the PDF appeared, so a
  clean exit plus a missing file is indistinguishable from "LibreOffice rejected this
  document". `-n auto` on a 28-core machine runs these in separate worker processes,
  all racing on the one default profile. Measured at 8-way: sharing converted a
  known-good file 4/8 times, a private profile per process 8/8.
- **The dangerous face is the silent one.** `soffice_can_convert` caches its result per
  process, so a probe that loses the race marks LibreOffice unavailable for that whole
  worker and every interop assertion on it **skips**. Five full-suite runs, unfixed:
  interop tests skipped in four of them (1, 0, 2, 1, 2 skips). Fixed: zero skips, zero
  failures, identical counts, five of five. The visible flake was the rarer outcome;
  usually the coverage just quietly vanished while the suite reported green.
- Second defect, found while checking the fix had not neutered the tests: with no input
  filter pinned, LibreOffice sniffs content and falls back to a **plain-text** import.
  A `.docx` whose entire content was the bytes `this is definitely not a zip` imported,
  converted to a valid PDF, and was reported as loading correctly — so the interop
  tests could have passed on a file Word would refuse outright. Fixed by pinning
  `--infilter=MS Word 2007 XML`, which accepts the real DOCX and rejects non-zip,
  truncated and malformed-XML inputs.
- Fix: a private `-env:UserInstallation` profile per worker process, created once and
  reused (~3.5s to build, ~1.7s warm); `--infilter` pinned; timeout raised 5s → 120s.
  The timeout was not the root cause — every failing worker exited cleanly well inside
  5s — but at 12-way concurrency fresh profile builds did cross it and failed every
  worker at once, so a private profile alone would have traded one flake for another
  on a wider machine. It is a hang guard, not a performance assertion.
- Tests: `python/tests/test_libreoffice_probe_isolation.py` (9), pinning per-worker
  isolation, harness honesty, and no-silent-skip. 5/9 fail against the unfixed harness,
  including both behavioural ones. The race test spawns real subprocesses — threads
  would not do, since the profile is per-process by design and in-process concurrency
  shares it legitimately; three processes is the cheapest reliable reproduction (2/3
  converted on every trial, where 2-way only failed 2 of 3 times).
