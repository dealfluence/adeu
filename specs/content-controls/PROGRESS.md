# Progress Log â€” Content Controls

Append-only. Newest entries at the bottom. Every entry: date, author (human or agent),
what happened, and any decision/deviation with its justification.

---

## 2026-08-21 â€” Initiative bootstrap (Claude, with Mikko)

- Field research across 24 US/Canada public-sector .docx/.dotx (10 SDT-positive).
  Headline facts driving the design (full evidence in the proposal artifact):
  - FedRAMP SSP rev4: 5,007 SDTs â€” 3,881 checkboxes (plain `â˜’/â˜` text runs, not
    `w:sym`), 459 text, 334 date, 27 combo, 21 dropdown, 94 data-bound, 718 showing
    placeholders, 371 cell-level SDTs, 3 `w:temporary`.
  - DAU Acquisition Plan: 162 SDTs, 48 locked (27 `sdtContentLocked` + 21
    `contentLocked`), 40 placeholders, **zero tags/aliases** (anonymous controls are
    the real-world norm; ordinals must be the primary identity).
  - US District Court (W.D. Wash.) model ESI agreement: every field data-bound;
    tags contain spaces and `#` ("Case #").
  - Ontario Juries Form 1: zero SDTs but `w:documentProtection edit="forms"
    w:enforcement="1"` with a real password hash â€” protection gates matter even for
    legacy-field documents.
  - Placeholder text is custom prose ("[Plaintiff]", "Program Name"), not the stock
    Word string. A dropdown's first `listItem` can be its own prompt ("Choose a type.").
  - `w:sdt` ids appear negative in the wild (signed int32 â€” consistent with the
    ST_LongHexNumber lesson in AI_CONTEXT Â§8, though sdt `w:id` is ST_DecimalNumber).
- Behavior audit of v2.4.1 (probes preserved in the proposal artifact):
  - Both engines flatten SDT structure entirely; placeholder ghosts read as body text.
  - Tracked edits are written inside `sdtContentLocked` controls and into
    forms-protected documents without complaint.
  - A ModifyText "fill" of an empty field leaves `w:showingPlcHdr` set and inherits the
    gray `PlaceholderText` style â€” the field remains empty to Word.
  - **P0 found:** Python drops row-level and cell-level SDT-wrapped table content
    (and misaligns the row); Node handles both. â†’ CC-0.
- Specs v1 written and frozen; acceptance examples authored; corpus manifest +
  fetch mechanism committed. Corpus documents are fetch-on-demand and gitignored.
- Open items deliberately parked: bound-field reject desync policy, review actions
  inside locked controls, anchor-density escape hatch (`[COM-PENDING]` / CC-6, CC-9).

## 2026-08-21 â€” CC-6 Word COM verification battery (opencode-windows)

Word 16.0 (Microsoft 365, desktop), Windows 11, driven through `pywin32`. Every finding
below is pinned as a test in `python/tests/test_live_word_content_controls.py`; fixture
builders in `python/tests/sdt_fixtures.py`, read-write COM oracle in
`tests/word_com.edit_and_save`. Method: build a minimal package, open it in real Word,
perform the operation a user would perform, save, read the XML back.

**Deviation from ground rule 3, disclosed:** CC-6 lists CC-1 as a dependency ("fixture
builders reused") and CC-1 is not done. Started anyway because the dependency is soft â€”
the bootstrap's `scripts/make_cc_fixture.py` already supplies the fixture vocabulary â€”
and because CC-6 is the only task requiring Windows + real Word, so blocking it behind
CC-1 idles the one machine that can do it while CC-4/CC-5 wait on its findings. No CC-1
code was touched; CC-6 ships knowledge and tests only.

### Answers to the six questions

- **(a) Placeholder clearing is NOT redlined. CONFIRMED.** Filling an empty control
  yields exactly one revision â€” the insertion. `w:showingPlcHdr` and the ghost run
  vanish untracked, and Word's inserted run carries no `rStyle PlaceholderText`. spec
  Â§4.2/Â§4.3 stand as written.
- **(b) Checkbox toggle is a glyph pair â€” but `w:ins` comes FIRST, then `w:del`.**
  Amended in spec Â§5: the frozen text said "del+ins", and the order is not cosmetic
  because the projection reads document order. `w14:checked` flips with no revision of
  its own (attribute sync, URL_RETARGET class).
- **(c) `w:temporary` unwraps on ANY content edit.** Broader than spec Â§4.4, which tied
  it to filling a placeholder: tracked or untracked, placeholder or already-filled, the
  wrapper is gone the instant content changes â€” while an untouched temporary control
  survives a round trip. The revision outlives the wrapper, so a reject restores the
  text but not the control. The unwrap is one-way.
- **(d) Word ALLOWS Accept/Reject inside `sdtContentLocked`.** G9 downgraded to *allow*
  per its own instruction. The lock stops typing ("You are not allowed to edit this
  selection because it is protected"), not review. Also pinned: Word's lock mapping is
  not one-to-one with the XML â€” `sdtContentLocked` sets both `LockContents` and
  `LockContentControl`; `sdtLocked` sets only the latter and leaves content editable.
  What *does* gate review is `w:documentProtection edit="trackedChanges"`, which refuses
  Accept and Reject document-wide ("This command is not available") while still
  permitting tracked edits â€” G7 confirmed.
- **(e) The bound STORE wins on open, and rejecting RESYNCS it.** Two corrections to
  spec Â§6. First, when content and store disagree Word rewrites the content from the
  store at load, silently: a content-only write to a bound control is not inconsistent,
  it is destroyed on next open. That makes the dual-write and G13 load-bearing rather
  than tidy. Second, the frozen text's "known asymmetry" is backwards â€” Word's binding
  engine pushes the restored value back on reject, so Word converges. The asymmetry is
  Adeu's: a headless reject leaves the store holding the rejected value, and since the
  store wins on open, Word then re-applies the rejected value to the content. A reject
  that silently un-rejects itself. CC-9's resync policy must cover the reject path.
  A dangling `w:storeItemID` is not an error (`IsMapped == False`, content edited
  normally, binding preserved) â€” Â§6.3 confirmed.
- **(f) Word re-instates the placeholder as soon as the emptying is REAL.** Not while a
  tracked deletion is pending (`w:showingPlcHdr` stays off across save and reopen â€” v1's
  rule confirmed for the state Adeu produces), but on an untracked delete *or on
  accepting* a tracked one, resolving the prose from the glossary doc part. Consequence:
  `accept_all_changes` over an emptied control diverges from what Word produces. Logged
  for v1.1/CC-9, not fixed here.

### Findings that are not answers to the six questions

- **`w:showingPlcHdr` is the only reliable placeholder signal.** The ghost run Word
  regenerates in (f) carries no `rStyle PlaceholderText`. Detecting placeholder state by
  ghost style â€” the obvious implementation â€” would miss every control Word itself
  emptied. Binding on CC-1.
- **Placeholder prose lives in the glossary part**, not in the control:
  `w:placeholder/w:docPart` â†’ `word/glossary/document.xml`. A `w:placeholder` reference
  with no glossary is inert; Word substitutes whitespace rather than the intended text.
  CC-1's `{>>placeholder: â€¦<<}` bubble has to resolve through the glossary to show the
  prose a user sees. (Cost an initial wrong reading of (f) before the fixture grew a
  real glossary part.)
- **Under `edit="forms"`, reading `Document.TrackRevisions` throws** â€” not just assigning
  it. Any probe or live-Word code path that reads-then-restores the flag dies before it
  does anything. Fills of unlocked controls are permitted there and are written
  **untracked**. G5 says "allow set_field" and is silent on the tracking consequence.
  *Needs Mikko's sign-off* (G5 carries no `[COM-PENDING]` tag, so CC-6 may not amend it):
  should Adeu refuse to write into a forms-protected document because it cannot honour
  its "always tracked" contract there, or write untracked with a loud report note?
- **Word manufactures format revisions on documents that omit `w:lang`.** With tracking
  on, Word stamps a proofing language onto every run lacking one, as `w:rPrChange`
  revisions â€” so a fixture without `w:docDefaults` arrives already carrying revisions.
  It only does this once another document has taught the instance a language, which made
  the CC-6 suite pass alone and fail after any other live-Word test. Fixed twice over:
  `sdt_fixtures` pins `w:lang`, and the tests count insertions/deletions rather than
  `Document.Revisions.Count`. Anyone writing live-Word tests should assume the raw count
  includes Word's own housekeeping.
- **Fixtures need a unique `w14:docId`** or Word treats them as one document
  (the `word_com.py` `_stage` lesson, which can only randomise an id that exists).

### Coordination

- CC-0's engine fix (`d4e967f`, merged `845afb3`) was rebased under this work; full
  Python suite green on top of it (1532 passed, 7 skipped) plus ruff and mypy.
- Not verified, deliberately out of CC-6 scope: repeating-section item operations and
  Word's behaviour on `w:sdt` in headers/footers. Flagged for CC-9 if it needs them.

---

## 2026-08-21 â€” CC-0 Python SDT table row/cell parity (opencode-osx)

Engine fix landed on `main` before the board existed (`61bc00a`, `d4e967f`; merged here
as `845afb3`) and converged independently on the same diagnosis the bootstrap filed as
CC-0. Python's row/cell walk now descends sdt wrappers via element-level
`iter_table_row_elements` / `iter_row_cell_elements` in `utils/docx.py`, retargeted in
both `ingest.py` and `redline/mapper.py`. This entry covers closing the acceptance gap.

### What A0 now pins

A0.1, A0.2, A0.4 were already covered as visibility tests in both engines. Added here:

- **A0.3's apply half**, which nothing asserted â€” both repro files were explicitly
  "visibility only". A `ModifyText` of `Jane Roe` â†’ `John Roe` inside the CC:15
  row-level control must resolve, and its `w:ins`/`w:del` must be *descendants of the
  sdt-wrapped `w:tr`* rather than hoisted out of the control; `w:tr` count unchanged;
  `accept_all_revisions` keeps the control. Mirrored in both engines. Both redline only
  the differing token (`{--Jane--}{++John++} Roe`), so assertions target that, not the
  whole phrase.
- **A0.5**, against the real template (see the caveat below).
- Both engines' table fixtures now reproduce the normative fixture-standard.md table
  verbatim (tags `cell_role`/`row_approver`/`cell_notes`, `w:id` 201-203, CC:16 text
  "Approved without conditions.") so CC-1 can layer `{#cc:N}` onto the same shape. The
  nested `w15:repeatingSectionItem` row A0.4 demands has no counterpart in the fixture
  (which carries repeating sections only at block level, CC:11-13) and stays appended as
  row 4 rather than mutating the frozen fixture.

### Deviation: A0.5 needs a CC-3 deliverable (needs a decision)

A0.5 calls `corpus_path()`, which CC-3 owns â€” but **CC-3 depends on CC-0**, so it cannot
ship first, and A5's preamble says corpus tests "run after CC-0". The graph is circular.
Resolved locally with a private `_corpus_path()` in the CC-0 repro file, marked for CC-3
to delete in favour of the shared helper. Cleaner alternative, for Mikko: **move A0.5 out
of A0 and into A5**, where the corpus machinery and every other corpus example already
live. Nothing else in A0 needs a downloaded document.

### A0.5 does not discriminate the bug it guards (spec weakness)

Measured on the real `fedramp_ssp_rev4` template, clean view, unpaginated:

| | chars |
| --- | --- |
| Python, sdt descent enabled (post-fix) | 498,800 |
| Python, row/cell sdt descent disabled (simulated pre-fix) | 490,345 |
| Node | 498,662 |

The fix recovers ~8,455 chars (1.7%) â€” real, and consistent with 371 cell-level SDTs of
short field values, but **both numbers clear A0.5's 400,000 floor**, so the example
passes with the bug present. A0.5's premise that "pre-fix Python projects a fraction of
it" holds for the *paginated* `read_docx` path it cites (45 pages), not for the
unpaginated engine-level extraction it actually asserts on. The floor cannot fail for
this bug. Recommend CC-3 replace it with the cross-engine parity assertion A5.1 already
implies (identical counts) â€” that one *is* discriminating: it separates 490,345 from
498,662 immediately. Not amended here; A0 is frozen (rule 7).

### Cross-engine parity gaps on the corpus â€” blocking for CC-3, not CC-0

Running the real template surfaced a 138-char Python/Node divergence, 78 differing
lines, **none of them sdt-related**. CC-3's A5.1 parity assertion will fail on these:

1. **Python leaks raw OOXML into the text projection.** The template has 17 real
   `<w:br w:type="page"/>` elements and zero literal occurrences in any `w:t`; Python's
   projection emits the literal string `<w:br w:type="page"/>` 17 times. Node emits
   blank lines. Three-line repro: a `w:p` containing
   `<w:r><w:t>A</w:t><w:br w:type="page"/><w:t>B</w:t></w:r>` projects as
   `A<w:br w:type="page"/>B`. Ingest and mapper *agree*, so the Virtual Text contract
   holds and offsets are not corrupted â€” but an LLM reads markup as prose, and any
   `target_text` spanning the break would have to include the XML. Not CC-0 scope;
   needs its own board row.
2. **Emphasis-marker coalescing differs.** Python merges adjacent italic runs into one
   span (`_Version #.#,  Date_`); Node marks each run (`_Version_ _#.#,_  _Date_`).
   Affects CC-1, which extends the same marker-stripping passes.
3. **Header/footer block ordering differs** â€” Node projects header lines Python omits at
   the top of the document.

### Board correction

CC-0's scope line says "Node already behaves correctly â€” pin it with a test". True for
row/cell sdts; **false in general**. `Table`/`Row` in `node/packages/core/src/docx/
primitives.ts` enumerated with recursive `getElementsByTagName`, so nested-table rows and
cells leaked into the outer table. Fixed in the same merge via `findChildrenSdtTransparent`
(`docx/dom.ts`); reverting it turns 6 of 24 guards red. CC-1 builds `sdt_start`/`sdt_end`
on this traversal and should not assume the Node side was sound.

### Verification

`python/`: ruff + `ruff format --check` (202 files) clean, mypy clean (36 files), pytest
1474 passed / 67 skipped. The single failure,
`test_repro_qa_2026_07_18.py::TestC1FooterBoundary::test_applied_output_loads_in_libreoffice`,
is a pre-existing environment flake reproduced on unmodified `origin/main` in an isolated
worktree before this work started. `node/`: build clean, 717 + 296 + 42 tests pass, lint
clean.

### 2026-08-21 â€” CC-0 closed; A0.5 moved (Mikko's sign-off)

Mikko approved the recommendation. A0.5 is now **A5.0** in A5-corpus-validation.md, with
the non-discrimination caveat attached so nobody reads a green run as proof that sdt
traversal works. A0 no longer requires a downloaded document â€” every remaining example
runs on the synthetic fixture. The test itself stays in the CC-0 repro file (with its
private `_corpus_path`) until CC-3 builds the A5 suite and adopts both. CC-0 â†’ `done`.

The three corpus parity divergences are filed as **CC-10** (P1) rather than left as prose
in this log, since they block CC-3's A5.1 identical-counts assertion. Scoped to sweep the
whole class of run-level elements that take the same path (`w:tab`, `w:cr`,
`w:noBreakHyphen`, `w:softHyphen`, `w:sym`) instead of special-casing `w:br` â€” the
projected form for each needs deciding once and pinning in both engines. A5.1 now carries
a pointer to the blockers.

### 2026-08-21 â€” CC-10 diagnosis corrected, blocked on a design decision (opencode-osx)

I filed CC-10 as "python leaks raw OOXML". Wrong, and it would have sent the next agent
hunting a bad `tostring()` call. `_PAGE_BREAK_TOKEN` (`utils/docx.py:95`) is a deliberate
in-band sentinel and it is load-bearing: `pagination.py:262-272` splits on it to honour
manual page breaks, a capability node does not have at all (`pagination.ts:131` is
density-only). `docs/FIDELITY.md:36` says both engines project a break as a newline, so
python violates its own documented contract while node satisfies it â€” but simply
conforming would delete a real, tested feature.

Three options are costed on the board. The blocker is that `paginate()` takes only a
string and has 15 call sites (engine, doc_cache, response builders, CLI), so routing the
signal out of band (option A) is a genuine refactor of core read paths, not a local fix.
Option B (project `\f` in both engines, split on `\f`) buys parity and keeps the
capability for a one-line pagination change, at the price of a non-printing character in
the output and an amendment to FIDELITY.md. Recommending B; not proceeding until Mikko
picks, because A and C have very different blast radii and B changes node's output for
every document with a page break.

**Arithmetic that changes CC-3's plan:** fixing page breaks alone does not unblock A5.1.
On `fedramp_ssp_rev4` the three divergences partially cancel â€” page breaks are worth
python +340 chars (17 Ã— 21), emphasis coalescing and the missing header lines are worth
node +202, netting the measured python +138. Closing only the break gap flips the sign to
node +202. A5.1's identical-counts assertion needs all three fixed, so CC-3 should not
plan around CC-10 alone.

### 2026-08-21 — CC-10 done: page breaks project as U+000C (opencode-osx)

Mikko chose option B. Both engines now project a manual page break as U+000C FORM FEED
instead of python's 22-character `<w:br w:type="page"/>` sentinel and node's `"\n"`.
Python's paginator splits on the character, so manual page breaks still start new virtual
pages — `test_cli_bug_repro.py`'s pagination and outline tests pass untouched, which was
the capability at risk.

Shape of the change: `_PAGE_BREAK_TOKEN` became `"\f"` with a public `PAGE_BREAK_TOKEN`
alias that `pagination.py` imports, so producer and consumer cannot drift; the third
python site (`get_run_text`) inlined the literal markup independently of the constant and
now uses it; node's `get_run_text` learned to distinguish `w:type="page"` at all, which it
previously did not, with a matching exported `PAGE_BREAK_TOKEN`. Only page breaks changed —
a soft `w:br` is still `"\n"` in both engines. `test_run_fusion_equivalence.py`'s oracle
hardcoded the old markup and was updated in the same commit; `docs/FIDELITY.md` amended,
since its "both project as a newline" line was the contract python had been violating.

Node's character count is unchanged (a 1-for-1 `\n`→`\f` swap); python's dropped 396 chars
on the corpus. Both engines now contain zero `<w:` in the projection.

**A5.1 is still blocked, and the measurement says so precisely.** Closing this gap flipped
the sign rather than closing it, exactly as forecast: `fedramp_ssp_rev4` clean view moved
from python +138 to **node +258** (python 498,404, node 498,662), differing lines 78 → 27.
What remains is the emphasis-marker coalescing difference and the header lines node
projects that python omits. CC-3 needs both fixed before asserting identical counts.

Left deliberately undone: node's paginator still ignores manual page breaks (it is
density-only). That is now a one-line change rather than a design problem, because the
signal exists in node's text — but it changes node's page numbering, so it wants its own
row and its own decision.

Verification: python ruff + format (203 files) + mypy clean, 1482 passed / 67 skipped;
node build clean, 722 + 296 + 42 pass, lint clean. The one red test in the full python run
(`test_repro_qa_2026_07_18.py` LibreOffice interop) is the known environment flake — it
passes on a serial re-run and was reproduced on unmodified `origin/main` before this work.
## 2026-08-21 â€” CC-3 corpus mechanism + A5 validation, pre-CC-1 subset (opencode-windows)

Shipped: `corpus_path()` (python/tests/utils.py) and `corpusPath()` / `corpusBuffer()` /
`corpusSkipReason()` (node/packages/core/src/test-utils.ts); `python/tests/
test_corpus_validation.py` and `node/packages/core/src/corpus_validation.test.ts`; the
optional `corpus-validation` CI job (`workflow_dispatch`, or repo variable
`ADEU_FETCH_CORPUS=1`). CC-0's local `_corpus_path` shim is gone, as its author asked.

### Helper design: absent â‰  unknown

Both helpers draw a line the obvious implementation blurs. An **absent document** skips,
because the corpus is fetch-on-demand and CI must be green without downloading. An
**unknown key raises**, listing the valid keys. Collapsing the two â€” skip on anything
that does not resolve â€” would turn every typo into a permanently green test, which is
this repo's most expensive bug class wearing a green tick. `fetch_corpus.py --only` has
the matching behaviour (exit 2 on an unknown key), and both are pinned by tests.

The Node twin returns `null` rather than skipping: vitest's `ctx.skip()` is only
reachable inside a test body, while the decision to emit a test at all often has to
happen at collection time.

### What is implemented, and what is deliberately not

A5 says to structure the suite so the pre-CC-1 subset is green on CC-0 alone. It is:

- **A5.9** â€” fetch mechanism: `--list` reports every manifest key with on-disk status;
  `--only <typo>` exits 2 with the known keys. No network in either.
- **A5.1 (partial)** â€” cell-level SDT visibility at production scale, both engines.
- **A5.7 (partial)** â€” the `.dotx` opens. Node passes; **Python fails** (see below).
- **A5.8 (partial)** â€” the negative `w:sdt` id survives a no-op round trip.

Deferred, each against the task that unblocks it: A5.2/A5.3/A5.5 and the ledger halves of
A5.1/A5.7 need the fields ledger (CC-2); A5.4's banner needs CC-2 and its gate needs
CC-4; A5.3/A5.5's fills need CC-5; A5.6's token-cost bound needs CC-1's anchors. These
are NOT stubbed as skips: a test that can never run is indistinguishable from a passing
one in a summary line, which is the same vacuous-green failure the helpers guard against.

### Two findings

**1. Python cannot open a `.dotx` at all â€” filed as CC-11 (P1).** `python-docx`'s
`Document()` rejects `...wordprocessingml.template.main+xml`, and every Python entry
point inherits it; `adeu extract file.dotx` dies with an unhandled traceback, not a
teaching error. `@adeu/core` reads the same file fine. A dual-engine parity break and a
product gap â€” templates are what a content-controls initiative is for. Not fixed inside
CC-3: the repair is a one-line normalisation at the shared `strip_bom_from_docx_bytes`
choke point, but the save side needs a decision (a normalised content type must not leak
into the output and convert the user's template into a document), and that is a design
call, not a drive-by. A5.7 is a **strict xfail** so it turns red the moment CC-11 lands
instead of sitting there as a permanent skip.

**2. A0.5's successor is now discriminating.** CC-0's own PROGRESS entry recorded that
the 400,000-char floor passes with the bug present (490,345 vs 498,800). CC-3's A5.1
replacement derives the cell-level SDT texts FROM the document â€” every `w:t` of >= 20
chars under an `sdtContent > w:tc` â€” and asserts each appears in the projection. It
fails immediately if row/cell descent regresses, and it does not rot when upstream
revises the template, because nothing is hardcoded. Both engines run the identical
computation. Also added: an assertion that no raw OOXML (`<w:sdt`, `sdtContent`,
`w:sdtPr`, `showingPlcHdr`) leaks into the projection â€” the failure mode opposite to
CC-0, where "fixing" invisibility by stringifying the element puts markup in front of
the model.

Two smaller traps worth recording. A5.8's negative `w:sdt` id lives in
`word/footer1.xml`, not `word/document.xml`; the first version of that test scanned only
the main part, found nothing, and passed vacuously â€” it now scans every part and asserts
the fixture still contains a negative id before asserting the round trip. And cell-level
SDT text must be compared per `w:t` node, never as a join of a control's runs: runs split
at arbitrary points and each engine reassembles them with its own whitespace rules, so a
joined string is not a substring of correct output.

A third, caught only by running the whole Node suite: **corpus tests need an explicit
vitest timeout.** Projecting `fedramp_ssp_rev4` takes ~2s alone and ~6s when the rest of
the suite is competing for the machine, against vitest's 5s default — so the test passed
in isolation and failed in a full run. That is the worst shape of red: it reads as a real
regression and does not reproduce. All three Node corpus tests now carry an explicit
60s timeout. Anyone adding corpus tests should do the same; these documents are two
orders of magnitude larger than every fixture in the suite.

### Not addressed: the cross-engine parity gaps CC-0 flagged

CC-0 recorded three Python/Node divergences on the FedRAMP template (literal
`<w:br w:type="page"/>` leaking into Python's projection, emphasis-marker coalescing,
header/footer ordering) and called them blocking for CC-3's A5.1 parity assertion. They
are not blocking as implemented: A5.1 here asserts each engine sees the cell-level SDT
content, not that the two produce byte-identical output. A true identical-output parity
assertion still needs those three fixed — now tracked as CC-10, which the OSX agent
filed and then corrected (the `<w:br w:type="page"/>` text is a deliberate sentinel, not
a serialization leak) and which is blocked on Mikko's pick between three designs.

Note the ordering that creates: **CC-10 must land before A5.1 can be strengthened into
the identical-counts assertion** that CC-0's entry recommended as the discriminating
replacement for the length floor. CC-3 did not wait for it — the cell-level-text form
implemented here is discriminating for the CC-0 repair without requiring the two engines
to agree byte for byte, so the guard exists now and gets sharper later.

### Verification

`python/`: ruff + `ruff format --check` (203 files) clean, mypy clean, pytest **1542
passed / 7 skipped / 1 xfailed**. `node/`: build clean, **721 + 296 + 42** tests pass,
lint clean. Corpus present locally, so the A5 suite ran for real rather than skipping.

### 2026-08-21 - Byte-identical projection parity achieved (opencode-osx)

Mikko: "Make the Node show the linebreaks and fix all." Done, with one carve-out
recorded as CC-12.

**Result: 16/16 views byte-identical** - 4 fixtures + 4 corpus documents, raw and
clean - with zero python mapper drift. A5.1's identical-counts assertion is
unblocked. Every one of the three remaining divergences turned out to be a
node-side bug; python was correct in all of them, and none was reachable from the
synthetic fixtures either suite used.

1. **Emphasis coalescing.** Node tested `pending_text.endsWith(closing_marker)`
   against the literal tail, but boundary whitespace is hoisted OUT of the marker,
   so the pending group usually ends `"**A** "`. The test always missed and node
   emitted `**A** **B**` where python emitted `**A B**`. Ported python's
   ignore-trailing-whitespace logic to node's ingest and the part-level equivalent
   to node's mapper - which also carried python's second documented fault, popping
   the closing marker without confirming the incoming run opens with the prefix,
   losing marker balance after a whitespace-only same-style run.

2. **Header/footer enumeration.** Node listed every header/footer PART in the
   package; Word renders only what a section references. Implemented python's
   section walk in node, honouring Link-to-Previous, `w:titlePg` and
   `w:evenAndOddHeaders` (`doc.sections` was a dead stub and
   `oddAndEvenPagesHeaderFooter` was hardcoded false - both unread). This exposed
   that three node fixtures built headers by dumping orphan parts into the
   package, unfaithful mirrors of the python builders which go through
   python-docx's `sec.header` and therefore wire part + relationship + sectPr
   reference. Added `attachHeaderFooter()` to test-utils and migrated them.

3. **Cell-anchor double space.** Node padded unconditionally before
   `{#cell:...}`; python pads only when the text does not already end in a space.
   With emphasis hoisting a trailing space, node emitted two. Present
   independently in node's ingest AND mapper.

Also swept the run-level elements both engines dropped silently.
`w:noBreakHyphen` now projects `-` (dropping it merged words: "e-mail" projected
as "email") and `w:ptab` projects a space. `w:softHyphen` stays unprojected -
Word renders it only when the line actually breaks - and **`w:sym` stays dropped
deliberately**: symbol fonts map glyphs into the Unicode private-use area, so the
code point alone does not identify the character. Guessing corrupts text, and
CC-1 owns checkbox glyphs and needs a font-aware decision. Both choices are now
pinned as tests so they read as decisions, not oversights.

Node's paginator honours manual page breaks via the U+000C token, so page numbers
now agree with python's (it was density-only). Parity verified including the
surprising case: a leading break yields TWO pages, the first empty, in both
engines.

**Carve-out, filed as CC-12.** Chasing the last divergence uncovered that node's
DocumentMapper drifts from node's own INGEST on real documents - a Virtual Text
contract violation, hidden until now because no test compared the two on anything
but synthetic fixtures. The cause is systemic: python emits block separators
before each block and rolls them back when the block projects nothing, node
appends after and strips trailing ones. I ported the top-level part loop, watched
it fix the leading stray separator and unmask a trailing one from the notes
sections (2 failing documents became 4), and reverted it: the discipline has to
move through `_map_blocks` in one change, which is a mapper-core refactor rather
than a parity patch. The guards are written and `it.skip`-ed with a pointer, so
CC-12 lands by deleting the skips.

Verification: python ruff + format + mypy clean, 1498 passed / 67 skipped, 0
failed; node build clean, lint clean, 744 passed + 4 skipped (the CC-12 guards) +
296 + 42.

### 2026-08-21 - CC-12 closed: node's block-separator discipline (opencode-osx)

Took CC-12 ahead of CC-1 deliberately: CC-1 introduces sdt anchors, which are
exactly the zero-width anchor spans that trigger the drift, so landing
projection on top of a broken separator discipline would have multiplied the
failures rather than exposed them.

The filed bug fixed as diagnosed. Python emits the block separator BEFORE each
block and rolls it back when the block projects nothing; node appended after and
stripped trailing ones. The task note said not to half-port it and that was
right - moving the whole discipline through `_map_blocks` and the part loop
together fixed two of the four failing documents immediately. Node had a second
fault in the same area worth recording: it used `is_first_para` as the separator
gate, but that is a DIFFERENT flag from python's `emitted_any_block`.
`is_first_para` places the footnote definition label and is flipped by tables and
by empty paragraphs; `emitted_any_block` is the reader's `blocks.length > 0`.
Conflating them makes the separator decision wrong for every container whose
first block projects nothing.

Chasing the last two documents surfaced two divergences the corpus cannot reach,
both of which had been sitting in node since before this initiative.

**Node's ingest was missing the clean-view skip for deleted paragraph marks.**
Accepting a tracked paragraph-mark deletion merges the paragraph away, so when
nothing visible survives inside it the accepted view must render no container -
an empty one costs a whole block separator. Node projected
`"Alpha\n\n\n\nBeta"` where python projects `"Alpha\n\nBeta"`. Ported python's
shared `paragraph_mark_is_deleted` predicate and applied it in both node
producers.

**Empty styled runs emitted their markers.** A bold run whose only child is a
drawing or a footnote reference left a dangling `****`, the exact case python
documents in its own comment. Python's branch is `elif text:`; node's was an
unconditional `else` that pushed prefix and suffix regardless.

The uncomfortable part is worth stating plainly. On the deleted-mark case node's
ingest and mapper were consistently wrong TOGETHER - they agreed with each other,
so the Virtual Text contract test was green while both were wrong. Only
cross-engine comparison caught it, and the corpus could not have: published
documents carry no tracked changes. My "16/16 byte-identical" claim from earlier
today was true for the documents measured and blind to that whole class of shape.
Section 5 of both parity suites now covers tracked-change shapes explicitly, and
the honest lesson is that a contract test between two implementations that share
an author is not independent evidence.

Verification: 10/10 views byte-identical python<->node (4 corpus documents plus
the tracked-change fixture, raw and clean), zero mapper drift in either engine.
python ruff + format + mypy clean, 1517 passed / 70 skipped. node build + lint
clean, 758 passed / 1 skipped, 296, 42.
