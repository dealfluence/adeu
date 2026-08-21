# Progress Log — Content Controls

Append-only. Newest entries at the bottom. Every entry: date, author (human or agent),
what happened, and any decision/deviation with its justification.

---

## 2026-08-21 — Initiative bootstrap (Claude, with Mikko)

- Field research across 24 US/Canada public-sector .docx/.dotx (10 SDT-positive).
  Headline facts driving the design (full evidence in the proposal artifact):
  - FedRAMP SSP rev4: 5,007 SDTs — 3,881 checkboxes (plain `â˜’/â˜` text runs, not
    `w:sym`), 459 text, 334 date, 27 combo, 21 dropdown, 94 data-bound, 718 showing
    placeholders, 371 cell-level SDTs, 3 `w:temporary`.
  - DAU Acquisition Plan: 162 SDTs, 48 locked (27 `sdtContentLocked` + 21
    `contentLocked`), 40 placeholders, **zero tags/aliases** (anonymous controls are
    the real-world norm; ordinals must be the primary identity).
  - US District Court (W.D. Wash.) model ESI agreement: every field data-bound;
    tags contain spaces and `#` ("Case #").
  - Ontario Juries Form 1: zero SDTs but `w:documentProtection edit="forms"
    w:enforcement="1"` with a real password hash — protection gates matter even for
    legacy-field documents.
  - Placeholder text is custom prose ("[Plaintiff]", "Program Name"), not the stock
    Word string. A dropdown's first `listItem` can be its own prompt ("Choose a type.").
  - `w:sdt` ids appear negative in the wild (signed int32 — consistent with the
    ST_LongHexNumber lesson in AI_CONTEXT §8, though sdt `w:id` is ST_DecimalNumber).
- Behavior audit of v2.4.1 (probes preserved in the proposal artifact):
  - Both engines flatten SDT structure entirely; placeholder ghosts read as body text.
  - Tracked edits are written inside `sdtContentLocked` controls and into
    forms-protected documents without complaint.
  - A ModifyText "fill" of an empty field leaves `w:showingPlcHdr` set and inherits the
    gray `PlaceholderText` style — the field remains empty to Word.
  - **P0 found:** Python drops row-level and cell-level SDT-wrapped table content
    (and misaligns the row); Node handles both. → CC-0.
- Specs v1 written and frozen; acceptance examples authored; corpus manifest +
  fetch mechanism committed. Corpus documents are fetch-on-demand and gitignored.
- Open items deliberately parked: bound-field reject desync policy, review actions
  inside locked controls, anchor-density escape hatch (`[COM-PENDING]` / CC-6, CC-9).

## 2026-08-21 — CC-6 Word COM verification battery (opencode-windows)

Word 16.0 (Microsoft 365, desktop), Windows 11, driven through `pywin32`. Every finding
below is pinned as a test in `python/tests/test_live_word_content_controls.py`; fixture
builders in `python/tests/sdt_fixtures.py`, read-write COM oracle in
`tests/word_com.edit_and_save`. Method: build a minimal package, open it in real Word,
perform the operation a user would perform, save, read the XML back.

**Deviation from ground rule 3, disclosed:** CC-6 lists CC-1 as a dependency ("fixture
builders reused") and CC-1 is not done. Started anyway because the dependency is soft —
the bootstrap's `scripts/make_cc_fixture.py` already supplies the fixture vocabulary —
and because CC-6 is the only task requiring Windows + real Word, so blocking it behind
CC-1 idles the one machine that can do it while CC-4/CC-5 wait on its findings. No CC-1
code was touched; CC-6 ships knowledge and tests only.

### Answers to the six questions

- **(a) Placeholder clearing is NOT redlined. CONFIRMED.** Filling an empty control
  yields exactly one revision — the insertion. `w:showingPlcHdr` and the ghost run
  vanish untracked, and Word's inserted run carries no `rStyle PlaceholderText`. spec
  §4.2/§4.3 stand as written.
- **(b) Checkbox toggle is a glyph pair — but `w:ins` comes FIRST, then `w:del`.**
  Amended in spec §5: the frozen text said "del+ins", and the order is not cosmetic
  because the projection reads document order. `w14:checked` flips with no revision of
  its own (attribute sync, URL_RETARGET class).
- **(c) `w:temporary` unwraps on ANY content edit.** Broader than spec §4.4, which tied
  it to filling a placeholder: tracked or untracked, placeholder or already-filled, the
  wrapper is gone the instant content changes — while an untouched temporary control
  survives a round trip. The revision outlives the wrapper, so a reject restores the
  text but not the control. The unwrap is one-way.
- **(d) Word ALLOWS Accept/Reject inside `sdtContentLocked`.** G9 downgraded to *allow*
  per its own instruction. The lock stops typing ("You are not allowed to edit this
  selection because it is protected"), not review. Also pinned: Word's lock mapping is
  not one-to-one with the XML — `sdtContentLocked` sets both `LockContents` and
  `LockContentControl`; `sdtLocked` sets only the latter and leaves content editable.
  What *does* gate review is `w:documentProtection edit="trackedChanges"`, which refuses
  Accept and Reject document-wide ("This command is not available") while still
  permitting tracked edits — G7 confirmed.
- **(e) The bound STORE wins on open, and rejecting RESYNCS it.** Two corrections to
  spec §6. First, when content and store disagree Word rewrites the content from the
  store at load, silently: a content-only write to a bound control is not inconsistent,
  it is destroyed on next open. That makes the dual-write and G13 load-bearing rather
  than tidy. Second, the frozen text's "known asymmetry" is backwards — Word's binding
  engine pushes the restored value back on reject, so Word converges. The asymmetry is
  Adeu's: a headless reject leaves the store holding the rejected value, and since the
  store wins on open, Word then re-applies the rejected value to the content. A reject
  that silently un-rejects itself. CC-9's resync policy must cover the reject path.
  A dangling `w:storeItemID` is not an error (`IsMapped == False`, content edited
  normally, binding preserved) — §6.3 confirmed.
- **(f) Word re-instates the placeholder as soon as the emptying is REAL.** Not while a
  tracked deletion is pending (`w:showingPlcHdr` stays off across save and reopen — v1's
  rule confirmed for the state Adeu produces), but on an untracked delete *or on
  accepting* a tracked one, resolving the prose from the glossary doc part. Consequence:
  `accept_all_changes` over an emptied control diverges from what Word produces. Logged
  for v1.1/CC-9, not fixed here.

### Findings that are not answers to the six questions

- **`w:showingPlcHdr` is the only reliable placeholder signal.** The ghost run Word
  regenerates in (f) carries no `rStyle PlaceholderText`. Detecting placeholder state by
  ghost style — the obvious implementation — would miss every control Word itself
  emptied. Binding on CC-1.
- **Placeholder prose lives in the glossary part**, not in the control:
  `w:placeholder/w:docPart` → `word/glossary/document.xml`. A `w:placeholder` reference
  with no glossary is inert; Word substitutes whitespace rather than the intended text.
  CC-1's `{>>placeholder: …<<}` bubble has to resolve through the glossary to show the
  prose a user sees. (Cost an initial wrong reading of (f) before the fixture grew a
  real glossary part.)
- **Under `edit="forms"`, reading `Document.TrackRevisions` throws** — not just assigning
  it. Any probe or live-Word code path that reads-then-restores the flag dies before it
  does anything. Fills of unlocked controls are permitted there and are written
  **untracked**. G5 says "allow set_field" and is silent on the tracking consequence.
  *Needs Mikko's sign-off* (G5 carries no `[COM-PENDING]` tag, so CC-6 may not amend it):
  should Adeu refuse to write into a forms-protected document because it cannot honour
  its "always tracked" contract there, or write untracked with a loud report note?
- **Word manufactures format revisions on documents that omit `w:lang`.** With tracking
  on, Word stamps a proofing language onto every run lacking one, as `w:rPrChange`
  revisions — so a fixture without `w:docDefaults` arrives already carrying revisions.
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

## 2026-08-21 — CC-0 Python SDT table row/cell parity (opencode-osx)

Engine fix landed on `main` before the board existed (`61bc00a`, `d4e967f`; merged here
as `845afb3`) and converged independently on the same diagnosis the bootstrap filed as
CC-0. Python's row/cell walk now descends sdt wrappers via element-level
`iter_table_row_elements` / `iter_row_cell_elements` in `utils/docx.py`, retargeted in
both `ingest.py` and `redline/mapper.py`. This entry covers closing the acceptance gap.

### What A0 now pins

A0.1, A0.2, A0.4 were already covered as visibility tests in both engines. Added here:

- **A0.3's apply half**, which nothing asserted — both repro files were explicitly
  "visibility only". A `ModifyText` of `Jane Roe` → `John Roe` inside the CC:15
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

A0.5 calls `corpus_path()`, which CC-3 owns — but **CC-3 depends on CC-0**, so it cannot
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

The fix recovers ~8,455 chars (1.7%) — real, and consistent with 371 cell-level SDTs of
short field values, but **both numbers clear A0.5's 400,000 floor**, so the example
passes with the bug present. A0.5's premise that "pre-fix Python projects a fraction of
it" holds for the *paginated* `read_docx` path it cites (45 pages), not for the
unpaginated engine-level extraction it actually asserts on. The floor cannot fail for
this bug. Recommend CC-3 replace it with the cross-engine parity assertion A5.1 already
implies (identical counts) — that one *is* discriminating: it separates 490,345 from
498,662 immediately. Not amended here; A0 is frozen (rule 7).

### Cross-engine parity gaps on the corpus — blocking for CC-3, not CC-0

Running the real template surfaced a 138-char Python/Node divergence, 78 differing
lines, **none of them sdt-related**. CC-3's A5.1 parity assertion will fail on these:

1. **Python leaks raw OOXML into the text projection.** The template has 17 real
   `<w:br w:type="page"/>` elements and zero literal occurrences in any `w:t`; Python's
   projection emits the literal string `<w:br w:type="page"/>` 17 times. Node emits
   blank lines. Three-line repro: a `w:p` containing
   `<w:r><w:t>A</w:t><w:br w:type="page"/><w:t>B</w:t></w:r>` projects as
   `A<w:br w:type="page"/>B`. Ingest and mapper *agree*, so the Virtual Text contract
   holds and offsets are not corrupted — but an LLM reads markup as prose, and any
   `target_text` spanning the break would have to include the XML. Not CC-0 scope;
   needs its own board row.
2. **Emphasis-marker coalescing differs.** Python merges adjacent italic runs into one
   span (`_Version #.#,  Date_`); Node marks each run (`_Version_ _#.#,_  _Date_`).
   Affects CC-1, which extends the same marker-stripping passes.
3. **Header/footer block ordering differs** — Node projects header lines Python omits at
   the top of the document.

### Board correction

CC-0's scope line says "Node already behaves correctly — pin it with a test". True for
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

### 2026-08-21 — CC-0 closed; A0.5 moved (Mikko's sign-off)

Mikko approved the recommendation. A0.5 is now **A5.0** in A5-corpus-validation.md, with
the non-discrimination caveat attached so nobody reads a green run as proof that sdt
traversal works. A0 no longer requires a downloaded document — every remaining example
runs on the synthetic fixture. The test itself stays in the CC-0 repro file (with its
private `_corpus_path`) until CC-3 builds the A5 suite and adopts both. CC-0 → `done`.

The three corpus parity divergences are filed as **CC-10** (P1) rather than left as prose
in this log, since they block CC-3's A5.1 identical-counts assertion. Scoped to sweep the
whole class of run-level elements that take the same path (`w:tab`, `w:cr`,
`w:noBreakHyphen`, `w:softHyphen`, `w:sym`) instead of special-casing `w:br` — the
projected form for each needs deciding once and pinning in both engines. A5.1 now carries
a pointer to the blockers.

### 2026-08-21 — CC-10 diagnosis corrected, blocked on a design decision (opencode-osx)

I filed CC-10 as "python leaks raw OOXML". Wrong, and it would have sent the next agent
hunting a bad `tostring()` call. `_PAGE_BREAK_TOKEN` (`utils/docx.py:95`) is a deliberate
in-band sentinel and it is load-bearing: `pagination.py:262-272` splits on it to honour
manual page breaks, a capability node does not have at all (`pagination.ts:131` is
density-only). `docs/FIDELITY.md:36` says both engines project a break as a newline, so
python violates its own documented contract while node satisfies it — but simply
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
On `fedramp_ssp_rev4` the three divergences partially cancel — page breaks are worth
python +340 chars (17 × 21), emphasis coalescing and the missing header lines are worth
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
## 2026-08-21 — CC-3 corpus mechanism + A5 validation, pre-CC-1 subset (opencode-windows)

Shipped: `corpus_path()` (python/tests/utils.py) and `corpusPath()` / `corpusBuffer()` /
`corpusSkipReason()` (node/packages/core/src/test-utils.ts); `python/tests/
test_corpus_validation.py` and `node/packages/core/src/corpus_validation.test.ts`; the
optional `corpus-validation` CI job (`workflow_dispatch`, or repo variable
`ADEU_FETCH_CORPUS=1`). CC-0's local `_corpus_path` shim is gone, as its author asked.

### Helper design: absent ≠ unknown

Both helpers draw a line the obvious implementation blurs. An **absent document** skips,
because the corpus is fetch-on-demand and CI must be green without downloading. An
**unknown key raises**, listing the valid keys. Collapsing the two — skip on anything
that does not resolve — would turn every typo into a permanently green test, which is
this repo's most expensive bug class wearing a green tick. `fetch_corpus.py --only` has
the matching behaviour (exit 2 on an unknown key), and both are pinned by tests.

The Node twin returns `null` rather than skipping: vitest's `ctx.skip()` is only
reachable inside a test body, while the decision to emit a test at all often has to
happen at collection time.

### What is implemented, and what is deliberately not

A5 says to structure the suite so the pre-CC-1 subset is green on CC-0 alone. It is:

- **A5.9** — fetch mechanism: `--list` reports every manifest key with on-disk status;
  `--only <typo>` exits 2 with the known keys. No network in either.
- **A5.1 (partial)** — cell-level SDT visibility at production scale, both engines.
- **A5.7 (partial)** — the `.dotx` opens. Node passes; **Python fails** (see below).
- **A5.8 (partial)** — the negative `w:sdt` id survives a no-op round trip.

Deferred, each against the task that unblocks it: A5.2/A5.3/A5.5 and the ledger halves of
A5.1/A5.7 need the fields ledger (CC-2); A5.4's banner needs CC-2 and its gate needs
CC-4; A5.3/A5.5's fills need CC-5; A5.6's token-cost bound needs CC-1's anchors. These
are NOT stubbed as skips: a test that can never run is indistinguishable from a passing
one in a summary line, which is the same vacuous-green failure the helpers guard against.

### Two findings

**1. Python cannot open a `.dotx` at all — filed as CC-11 (P1).** `python-docx`'s
`Document()` rejects `...wordprocessingml.template.main+xml`, and every Python entry
point inherits it; `adeu extract file.dotx` dies with an unhandled traceback, not a
teaching error. `@adeu/core` reads the same file fine. A dual-engine parity break and a
product gap — templates are what a content-controls initiative is for. Not fixed inside
CC-3: the repair is a one-line normalisation at the shared `strip_bom_from_docx_bytes`
choke point, but the save side needs a decision (a normalised content type must not leak
into the output and convert the user's template into a document), and that is a design
call, not a drive-by. A5.7 is a **strict xfail** so it turns red the moment CC-11 lands
instead of sitting there as a permanent skip.

**2. A0.5's successor is now discriminating.** CC-0's own PROGRESS entry recorded that
the 400,000-char floor passes with the bug present (490,345 vs 498,800). CC-3's A5.1
replacement derives the cell-level SDT texts FROM the document — every `w:t` of >= 20
chars under an `sdtContent > w:tc` — and asserts each appears in the projection. It
fails immediately if row/cell descent regresses, and it does not rot when upstream
revises the template, because nothing is hardcoded. Both engines run the identical
computation. Also added: an assertion that no raw OOXML (`<w:sdt`, `sdtContent`,
`w:sdtPr`, `showingPlcHdr`) leaks into the projection — the failure mode opposite to
CC-0, where "fixing" invisibility by stringifying the element puts markup in front of
the model.

Two smaller traps worth recording. A5.8's negative `w:sdt` id lives in
`word/footer1.xml`, not `word/document.xml`; the first version of that test scanned only
the main part, found nothing, and passed vacuously — it now scans every part and asserts
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

## 2026-08-21 — CC-11: Python opens every WordprocessingML flavour (opencode-windows)

`.dotx`, `.dotm` and `.docm` now open through every Python entry point, and a
`.dotx` saves back as a `.dotx`. Status `review`.

**The task description proposed the wrong fix, and its own open question is what
exposed it.** CC-11 was filed with the scope "normalise the template content type
in `strip_bom_from_docx_bytes`" plus a flagged worry: does the normalised type leak
into the output? It does, unavoidably — `python-docx` serialises
`[Content_Types].xml` from each part's own `content_type` attribute, so a load-time
rewrite rides all the way through `save()` and silently converts the user's template
into a document. A read-only operation would have cost the user their template. The
worry was not a detail to trade off; it was the design telling us it was wrong.

The repair instead teaches `python-docx` the other content types.
`PartFactory.part_type_for` is the map it consults to pick a part's class;
`adeu/utils/opc.py` registers `template.main+xml`,
`document.macroEnabled.main+xml` and `template.macroEnabledTemplate.main+xml`
against `DocumentPart`. The part then *is* a `DocumentPart` while keeping its own
declared content type, so nothing needs rewriting and nothing can leak.
`docx.Document()`'s own content-type equality check is bypassed by opening the
package directly, which is also where the teaching error now lives: it names the
content type it found and lists the four it accepts, instead of `python-docx`'s bare
"is not a Word file" that the CLI surfaced as an unhandled traceback.
`load_document()` deliberately requires its argument — `docx.Document()` defaults to
opening a bundled empty template, which turns a forgotten argument into a silently
empty document rather than an error.

All nine `docx.Document` call sites (`ingest`, `redline/engine`,
`mcp_components/doc_cache`, `sanitize/core`, `text_revision`, `utils/docx`, `cli`,
`tools/live_word` x2) now import `adeu.utils.opc.load_document`.

**Parity table extended to the fifth corpus document.** CC-3's pinned projection
sizes covered four documents; `odot_uic_drywell` was missing for exactly one reason —
python could not open it, so there was nothing to pin against. It projects 7,221
chars in BOTH views in BOTH engines, byte-identical, and is now pinned in both
`test_repro_projection_parity_gaps.py` and `repro_projection_parity_gaps.test.ts`.
A5.1's "identical counts" assertion therefore covers the template case too.

**Tests are synthetic, not corpus-only.** `tests/test_opc_document_types.py` builds
all four flavours through `sdt_fixtures.build_sdt_docx(..., main_content_type=...)`
(new kwarg) and asserts: each opens; the declared type never reaches the text
projection; and `save()` round-trips the declared type unchanged. The corpus is
gitignored and optional, so a corpus-only guard would skip straight past a regression
on a default CI run. A5.7's strict xfail in `test_corpus_validation.py` is un-marked
and passes.

Also corrected two stale references in `repro_projection_parity_gaps.test.ts`: the
skipped mapper-drift guards cite CC-11, but that drift is CC-12 — a leftover from the
task-number collision. CC-11 is the `.dotx` row.

Verification: python ruff + format + mypy clean, 1595 passed / 7 skipped / 0 failed;
node build clean, lint clean, 755 passed + 5 skipped (the CC-12 guards) + 296 + 42.

Note for whoever picks up CC-8 (docs): `is_template()` and `suggested_extension()`
are exported from `adeu.utils.opc` for callers that write a NEW file beside the
original and so have to choose an extension. Nothing uses them yet — the CLI and
sanitize both write to a path the user supplied — but a `.dotx` sanitized to
`out.docx` would be a surprise worth documenting.

### 2026-08-21 — CC-11 addendum after rebasing onto CC-12 (opencode-windows)

Two corrections to the CC-11 entry above, both caused by CC-12 landing between
writing it and pushing.

The stale `CC-11` references in `repro_projection_parity_gaps.test.ts` no longer
exist to correct: CC-12 deleted those comments along with the `it.skip`. The
rebase took CC-12's side.

That means the mapper-agreement guard is now ACTIVE over the corpus table, and
CC-11 added a fifth key to that table — so `odot_uic_drywell` is checked for
ingest-vs-mapper agreement too, on a `.dotx`, and passes. The two tasks compose
better than either planned: CC-12 fixed the separator discipline, CC-11 gave it
a template to prove it on.

Verification re-run on the rebased tree: python ruff + format + mypy clean, 1597
passed / 7 skipped; node build + lint clean, 762 + 296 + 42 passed, 0 skipped.

### 2026-08-21 - CC-1a: classification + ordinals, and a defect in GOLDEN-RAW (opencode-osx)

Claimed CC-1 and sliced it 1a-1f on the board so the two agents can work it in
parallel; taking 1a+1b myself because they are one design.

**Landed (1a, first half):** `classify_sdt` / `classifySdt` and the ordinal
pre-pass in both engines, as a new twin module pair
(`utils/content_controls.py`, `utils/content-controls.ts`) rather than more
weight in `utils/docx.*` - that file is the most contended in the tree and a
small paired file keeps the python/node diff reviewable by eye. 26 tests per
engine, asserting the SAME table transcribed from fixture-standard.md. Both
green.

**The fixture body is now one file, not four.** It was already transcribed by
hand into `scripts/make_cc_fixture.py`, and each engine's
`repro_sdt_table_row_cell_invisibility` suite carries its own copy of the table
XML. That is exactly how the engines drift - it is what produced the CC-12 class
of bug. The normative body now lives in `shared/fixtures/cc_fixture.body.xml`
and the script plus both test suites read it. `make_cc_fixture.py` still
produces byte-identical output.

**Two findings worth recording.**

*`w14:val`, not `w:val`.* The checkbox state lives in the w14 namespace. A
reader that only consults `w:val` reports every checkbox as unchecked - which is
worse than crashing, because the projection would then render a confident `[ ]`
over a ticked box. Pinned in both engines. Related: python-docx does not
register the `w15` prefix at all, and the only thing that currently registers it
is an import side effect in `adeu.redline.comments`. Depending on import order
for the correctness of a projection rule is not acceptable, so the new module
spells the namespaces in Clark notation directly, following `mapper.py:203`.

**DEFECT IN A NORMATIVE GOLDEN - needs Mikko's call (README rule 4).**
GOLDEN-RAW in `acceptance/fixture-standard.md` renders the table as

```
Role | {#cc:14}Contracting Officer{#/cc:14}
{#cc:15}Approver | Jane Roe{#/cc:15}
Notes | {#cc:16}Approved without conditions.{#/cc:16}
```

but both engines emit a GFM header divider after the first row of EVERY table,
and have done since long before this initiative:

```
Role | Contracting Officer
--- | ---
Approver | Jane Roe
Notes | Approved without conditions.
```

So A1.1 as written is unachievable without either changing the golden or
changing table projection for every document in the product. The golden is
wrong, not the engines - the divider is load-bearing for markdown table
rendering and is covered by existing tests. I have NOT edited the golden: it is
normative and frozen, and silently rewriting an acceptance target to match the
implementation is how acceptance tests stop meaning anything. Proposed
correction, pending sign-off:

```
Role | {#cc:14}Contracting Officer{#/cc:14}
--- | ---
{#cc:15}Approver | Jane Roe{#/cc:15}
Notes | {#cc:16}Approved without conditions.{#/cc:16}
```

Until that is confirmed I am building 1b against the goldens as written for
everything except the divider line, and A1.1 will be marked blocked rather than
passed.

Also confirmed on the baseline, as expected and not yet fixed: the CC:2
placeholder ghost text currently projects as bare body text ("This Agreement is
made between Click or tap here to enter text. and the Government of Example."),
which is the A1.4 violation CC-1b removes, and the CC:6 checkbox still projects
the raw glyph. Both are the point of the task.

## 2026-08-21 — CC-1c reconnaissance: what checkboxes actually are (opencode-windows)

CC-1c claimed. 1a is still in flight, so the implementation half cannot be wired
without colliding with osx's `classify_sdt`; this is the half that needed doing
first anyway, because it decides whether §4 is implementable as written. It is —
every finding confirms the frozen spec. No amendment requested.

**Corpus census (10 documents, ~7,700 checkboxes).** All `w14:checkbox`; **zero**
legacy `w:fldChar`+`w:ffData/w:checkBox` form fields. That was the open risk in
the claim note — the legacy field is not a content control and no traversal in
this initiative would reach it, so if the wild were full of them A1.8 would cover
almost nothing. It isn't. Scope confirmed, and pinned so that adding a corpus
document with legacy fields re-opens the question loudly.

All state glyphs are `MS Gothic` 2612/2610, no Wingdings private-use code points.
Convenient, but pinned as *empirical*, not contractual: `w14:checkedState` is
free-form font+code-point, so recognising a checkbox by its character would work
on all ten of these documents and remain wrong. This is the evidence for §4
projecting from `w14:checked`.

**Not one checkbox in the corpus is ticked.** All ~7,700 are `w14:val="0"`; these
are blank published templates. The corpus can validate `[ ]` at production scale
and can say *nothing* about `[x]`, so a corpus-driven implementation would be
half-tested while looking thoroughly exercised. Written up as a test so the gap
is a stated fact. The `[x]` path is covered synthetically and by live Word.

**The trap for the implementation half.** `odot_uic_drywell` has 21 `☐`
characters and 19 checkboxes. The other two are Segoe UI Symbol runs in ordinary
prose — a human typing a box instead of inserting a control. They are text and
must project as `☐`. The obvious way to satisfy A1.8's "NO `☒`/`☐` characters" is
to substitute on the character, which invents two checkboxes that do not exist,
in a document with 19 real ones to hide among. A1.8's no-glyph clause must be
read as scoped to control content; the substitution has to be driven by the
`w14:checkbox` control.

**Three live-Word probes** (`test_live_word_content_controls.py`, now 18 COM
tests):

1. *Rejecting a toggle restores `w14:checked`.* CC-6(b) established the attribute
   is untracked while the glyph swap is tracked — the exact shape that produced
   the bound-store asymmetry in CC-6(e). If reject restored the glyph and
   stranded the attribute, a document would read `☐` on screen and `checked="1"`
   in the file, and §4 would render `[x]` under a visibly empty box. Word rolls
   the attribute back with the revision. This is what licenses §4, and nothing in
   the schema guarantees it, so it is pinned.
2. *Word writes the glyph as literal `w:t` text, not `w:sym`.* Load-bearing for
   the mapper: a 3-character token maps onto a 1-character run only if there is
   one. `<w:sym/>` projects as nothing (node drops it deliberately, per CC-12),
   which would shift every offset after it. Measured on Word's own inserted run,
   not on our fixture's.
3. *Word refuses plain-text edits inside a checkbox* — "You are not allowed to
   edit this selection because it is protected", with no `w:lock` and no
   `w:documentProtection` in the fixture. **Probed expecting the opposite.** The
   hypothesis was that Word would let you overwrite the glyph with prose, leaving
   a `w14:checkbox` whose content is not a glyph and whose `w14:checked`
   describes nothing. It won't. So §4 may assume the content IS the glyph run for
   any document Word produced, and A3.8's rejection of textual mutations other
   than `[ ]` ↔ `[x]` is not an Adeu house rule — it reproduces Word's own
   refusal, which is a much better footing for the error message. Such documents
   remain hand-constructible (we built one to test with), so the projection
   should not crash on prose inside a checkbox; it is malformed input, not a
   shape Word makes.

Remaining for CC-1c, once 1a lands: the projection itself (`[x]`/`[ ]` from
`w14:checked`, no anchor pair, mapper width accounting), both engines.

Verification: python ruff + format + mypy clean, 1614 passed / 7 skipped, of
which 18 are live-Word COM tests on real Word 16.0.

## 2026-08-21 — CC-13 filed: the live-Word suite is nondeterministic (opencode-windows)

Filed while trying to get a clean pre-push run for CC-1c. `python/tests/test_live_word*.py`
fail an arbitrary, different subset on every run of the same commit — observed
`5 passed`, `3 failed`, `5 failed`, `5 passed` back to back on
`test_live_word_structured_insertion.py` alone. Since `.githooks/pre-push` runs the
python suite, this blocks pushing at random, from Windows, for work unrelated to Word.

**Checked whether the content-controls COM battery caused it. It did not.** CC-6 added
15 live-Word tests and CC-1c added 3, all of which call `Documents.Open` in the shared
Word instance, so the suspicion was reasonable. A detached worktree at `f3aadb7` —
before CC-1c's tests and before any conftest change — failed 5/5 on the same file, and
a combined run there gave `4 failed, 21 passed` followed by `25 passed`. Pre-existing.

The failures are cross-document contamination rather than wrong expectations, and the
tell is unmistakable once seen: a failing assertion reports text belonging to a
*different test file*, e.g. `assert '{++Title++}' in 'Initial {==manuscript==}...'`
where the actual value is `test_live_word.py`'s fixture document and the expectation is
`test_live_word_structured_insertion.py`'s. The tools under test resolve Word via
`GetActiveObject` and read `app.ActiveDocument` at call time; the fixture hands the test
a specific `doc`; nothing holds the two together.

One real defect fixed on the way past: `active_word_app` called `app.Activate()`, which
raises the *application* window and says nothing about which *document* is active. It
never called `doc.Activate()`. Added, along with a setup guard that fails with the list
of currently-open documents instead of silently measuring a neighbour. Against a
deliberately poisoned Word — a stray document left open and activated, which is exactly
what a crashed earlier run leaves behind — the suite went from failing to `5 passed`.

Recorded honestly as a mitigation, not a fix. Runs still fail intermittently, and the
new guard has never once fired, which localises the remaining problem: activation is
correct when the fixture yields and is lost *afterwards*, between setup and the tool's
`GetActiveObject` call. CC-13 carries four candidate approaches and an acceptance bar of
10 consecutive clean full-suite runs, including 10 against a poisoned Word.

Practical note for the other agent and for future me: a live-Word failure should be
reproduced from a cold Word (`Stop-Process -Name WINWORD -Force`) before it is believed.
That killing Word between runs changes the outcome is itself the evidence that the state
lives in the Word instance and not in the tests.
### 2026-08-21 - CC-1b: inline anchors land in both engines (opencode-osx)

`sdt_start`/`sdt_end` events now flow through both engines' paragraph traversal,
and ingest and the mapper both consume them. Inline anchored leaf controls
project their `{#cc:N}` pairs with normative flag order, the empty control
projects as the matchable adjacent pair, and the placeholder bubble renders in
the raw view only. **A1.4 is fixed**: the ghost text no longer projects as body
text. That was the worst pre-CC-1 defect - a reader could not distinguish
"Click or tap here to enter text." from a real party name, and neither could a
model asked to fill the field.

Verified 12/12 views byte-identical python<->node (5 corpus documents, the .dotx
template, and the 16-control fixture, raw and clean) with zero mapper drift in
either engine.

**Design points.**

The ordinal pre-pass is computed once per document by each producer from the
SAME shared helper over the SAME part sequence, and threaded down as a
parameter. Passing the map is what makes the boundary visible at all: callers
that omit it (outline, sanitize, domain) keep the historical transparent
behaviour exactly, which is what they want - outline must not grow anchor
tokens.

`SdtEvent` is a sibling of `DocxEvent`, not another `type` case inside it.
`DocxEvent` is a four-string record; an anchor needs the whole `SdtInfo` (flags,
class, placeholder text), so folding it in would have meant a parallel
out-of-band lookup at every consumer - which is the shape of drift this
initiative keeps paying for.

**A rule I had to make explicit rather than guess.** Node's mapper drifted 3
characters from node's ingest on `dau_acquisition_plan`: a heading containing an
empty control, `## {#cc:62 locked}{>>placeholder: ...<<}{#/cc:62}   `. Both
engines strip leading whitespace-only runs in headings and stop at "any non-Run
event"; the mapper counted an sdt boundary as such an event and the ingest did
not. Rather than pick whichever side made the diff vanish, I applied the
documented rule uniformly to all four sites: an anchor is addressable text, so
heading content has begun and the strip stops. My own CC-12 guard caught this,
which is the first time that guard has paid for itself.

**Scope honesty.** Block-level anchors (CC:1), groups (CC:8) and the three table
controls (CC:14-16) are NOT done - they still project transparently, content
visible, anchors missing. Both suites assert that explicitly in a test that
fails the moment those anchors land, so the gap cannot be mistaken for done and
the golden comparison has to be updated deliberately. A1.1/A1.2 full-document
goldens therefore remain unmet (and A1.1 is additionally blocked on the
GOLDEN-RAW divider defect flagged earlier today).

**Filed CC-14, and it is not mine.** Full-suite verification surfaced
`test_p2_json_text_roundtrip_is_exact_or_loud` failing: a one-paragraph document
split into two applies cleanly and yields `'0.\n\n 0.'` instead of
`'0.\n\n0.'`. I stashed my work and reproduced it on a clean tree - pre-existing,
unrelated to content controls, surfaced only because hypothesis happened to
generate that example during one of my runs and cached it. It is the silent
failure mode the property exists to forbid: the engine may refuse an edit, but
it may not accept one and produce a different document. `.hypothesis` is
gitignored, so CI will not rediscover it deterministically; CC-14 says to pin
the example.


## 2026-08-21 - CC-13 measured: the live-Word suite stopped lying (windows)

CC-13 goes to `review`. **15 consecutive runs of all three live-Word files (43
tests), zero failures, every run with 4 stray documents left open and activated
in Word.** That is the poisoned half of the acceptance criterion, exceeded. The
baseline it replaces: the same suites gave `5 passed`, `3 failed`, `5 failed`,
`5 passed` on one unchanged commit.

Two defects, both real, both in the fixture rather than in the tests that were
failing:

1. `active_word_app` activated the *application* (`app.Activate()`) and never the
   *document*. `Documents.Add()` usually makes the new document active, which is
   why this survived so long.
2. The verification checked the fixture's own `Dispatch` handle. That is the
   wrong object. The tools under test resolve Word through
   `GetActiveObject("Word.Application")`, and when two `WINWORD.EXE` processes
   are alive the two calls return **different applications** - so the check could
   agree while the code under test disagreed. `_await_active_document` now polls
   the production lookup path, and polls rather than asserts once because
   `Document.Activate()` is asynchronous and a bare assert after it is a race
   that usually passes.

**The clock is the interesting part.** Cold Word runs the suite in 29s; with 4
strays open it takes 103s, reproducibly, scaling with stray count. Those 74
seconds are the retry loop losing the race and re-activating. Contamination is
therefore still occurring on every single run - it is now being corrected
instead of silently mis-measured. Document accumulation has been converted from
a correctness problem into a performance problem. Worth the trade, and worth
being explicit that it is a trade.

**A dead end, recorded so nobody repeats it.** I tried closing every document
that appeared during a test, scoped to spare a developer's own open documents.
It makes things worse: the live-Word tools hand back Ranges into documents they
opened, so reaping them yields `(-2147417848) The object invoked has
disconnected from its clients` and `Object has been deleted`. That is a *worse*
failure than the one being fixed, because it reads as a COM fault rather than a
test-isolation bug. Reverted, with the reason left in the teardown comment.

**Residual, stated plainly:** one uncharacterised failure in ~21 runs, mid-streak,
not captured; 12 further runs with output capture armed did not reproduce it. So
`review`, not `done`. Observed rate ~5%, down from >50% - the difference between
"pushing from Windows is a coin flip" and "pushing works".

Method note, since it cost me the most time and would cost the next person the
same: **reproduce from a cold Word** (`Stop-Process -Name WINWORD -Force`) before
believing any live-Word result. Run-to-run state lives in the Word instance, not
in the tests, so an uncontrolled Word makes every measurement meaningless -
including the measurements that make a fix look like it worked.
## 2026-08-21 — CC-1b complete: block, group and table anchors (`eb0a141`, osx)

Block-level controls, groups and table row/cell controls now project their
anchors in both engines. CC-1b is `done`.

**Structure.** `iter_block_items(parent, emit_sdt=True)` yields a block-level
control as one *undescended* `BlockSdt`; the consumer recurses into
`w:sdtContent` exactly as it already does for a `Table`. Paired boundary events
were the alternative and were rejected — they would have forced every consumer
to grow a nesting stack and to re-derive the block separators inside it. The
flag is opt-in so outline, domain, sanitize and `_normalize_table` keep the
historical sdt-transparent behaviour; the outline in particular must not grow
anchor tokens. Row and cell controls resolve through `wrapping_sdt`/`wrappingSdt`
(one hop) rather than by changing the CC-0 sdt-transparent iterators.

**Spec §3 exception.** Inside a table cell, a block-level anchor renders inline.
A row is one projected line; token lines would break the `|` grammar and
desynchronise the column count. The row-level anchor is the *inner* wrapper
relative to CriticMarkup: `{++ {#cc:N}...{#/cc:N} ++}`.

**GOLDEN-RAW divider — resolved, and it was the document, not the engine.**
Flagged during CC-1a. GOLDEN-RAW omitted the `--- | ---` line. Both engines have
emitted a GFM divider after the first row of every table since long before this
initiative; without it the projection is not a markdown table, just lines
containing pipes. Transcription error in the acceptance document, corrected in
place with a dated note. Authorised by Mikko.

**A node defect only the parity harness could find.** Hyperlinks inside *any*
block-level control degraded from `[text](mailto:...)` to bare text — 611 bytes
of `fedramp_ssp_rev4`. The node engine derives the OPC part from
`container.part`, and I handed the recursion a raw `w:sdtContent` element, which
silently lost it. Python takes `part=` as an explicit argument and so never had
the hazard. This is the third time a defect has lived in a place where both of
that engine's own producers agreed with each other and were both wrong, or where
one engine's API shape hid a trap the other does not have. A test written
against the other implementation by the same author is not independent evidence;
14/14 byte-identical projections across six corpus documents is.

**Tests.** A1.1/A1.2 now parse the golden out of `fixture-standard.md` and
compare the whole document, so the spec is the oracle instead of a hand-copied
constant. Exactly one substitution is applied — the CC:6 checkbox glyph, still
`☒` rather than `[x]` — and that constant, `_CC1C_PENDING`, is what CC-1c
deletes. The deliberately-failing CC-1b scope marker is gone, replaced by these.
The CC-0 row/cell/nested-table suites now strip anchors before asserting, since
their subject is whether wrapped rows are visible *at all*; each gained an
explicit anchor assertion so nothing can restore visibility while quietly
dropping the anchors.

**Left for others.** CC:6 renders `☒`; that is 1c (windows). 1d, 1e and 1f are
unclaimed and now unblocked — 1b was their dependency.

## 2026-08-21 — CC-1d: anchors survive chrome-stripping (A1.6, osx)

A1.6 names two passes, and neither turned out to be the defect.

**What was already right.** Both marker strippers — the outline's
`_strip_inline_formatting` and search's `_emphasized_snippet` — protect
`{#...}` tokens from emphasis pairing, added by QA 2026-07-23 F4 for
`{#_Ref…}` bookmark anchors. That protection covers `{#cc:N}` unchanged, so
CC-1d pins it rather than rewriting it. Worth stating plainly because the task
scope predicted work here and there was none: `_{#cc:3}_` still strips to
`{#cc:3}`, correctly, since those underscores are real markers around the
token rather than part of it.

**What was wrong.** The passes that *cut* text knew nothing about tokens.
Outline truncation at 200 chars sliced straight through an anchor and emitted
`{#cc:`; the search snippet's radius windows did the same. A split anchor is
worse than a missing one — `{#cc:` is a plausible target an agent will copy
that resolves to nothing — which is exactly why A1.6 words the rule as "the
whole token, or omitted, never split". Both are pre-existing and hit
`{#_Ref…}` too; CC-1 only made them likely, by putting anchors in far more
headings and snippets.

The snippet half is reachable in production, not theoretical: the radius ladder
clamps whenever a result set exceeds the response budget. Windows now widen to
the token edge, which is the behaviour `_balance_snippet_window` already had
for CriticMarkup, so the fix follows the function's existing contract instead
of inventing a second one.

**Two things the fixtures taught.**

The outline has two heading-text derivations and only one can exhibit the bug.
The legacy path rebuilds text with `build_paragraph_text`, which carries no sdt
anchors at all; the fast path slices the projected body, which does. Production
— both MCP servers and the CLI — takes the fast path, because it passes
`paragraph_offsets`. A first cut of the node suite drove the legacy path and
went green over the one code path that is structurally incapable of failing.
Both suites now assert the projection contains `{#cc:` before they begin.

The two engines needed *different* fixtures to produce one heading: Python
needs a `Heading1` pStyle, Node accepts a bare `w:outlineLvl`. That is a real
cross-engine divergence, filed as **CC-15**. It is a consequence of a
documented `python-docx` 1.2 gap, already pinned internally, but the
cross-engine half was untracked. Not fixed here — adding headings changes every
outline consumer and deserves a corpus sweep, not a drive-by.

**Also aligned:** node's `_truncate_outline_text` now `trimEnd()`s like
Python's `.rstrip()`. That divergence pre-dates this task, but trimming a
dangling anchor makes trailing whitespace common rather than rare, so leaving
it would have amplified a latent parity bug.

Every one of the three new suites was run against the unfixed code first:
13/30, 2/9 and 6/10 failed respectively. A regression test that does not fail
on the bug is decoration.

## 2026-08-21 — CC-1e: anchor fabrication, mutation and deletion refused (A1.7, osx)

Two of A1.7's three cases were already refused before this task. VAL-OBS-9
("Internal Anchor Structural Integrity") compares anchor counts, but only in
one direction: it iterates the anchors in `new_text` and fires when one gained
copies. That covers (a) fabricating `{#cc:99}` and (c) rewriting
`{#cc:7 locked}` to `{#cc:7}` — the latter because the flag-stripped form is a
new distinct token.

It does not cover (b), deletion. With `new_text` holding no anchors there is
nothing to iterate, so an edit whose target covered `{#/cc:3}` and whose
replacement omitted it passed validation and left the pair unbalanced in the
projection. That is the whole of the new rule.

**Why it is scoped to `cc` and not made symmetric for every `{#...}`.** The
obvious fix — compare the full anchor multiset both ways, as the footnote and
image checks already do — would have closed two deliberate targeting surfaces:

- `{#cell:paraId}`, the empty-cell write, matched by
  `^\{#cell:[^}]+\}$` in both engines' apply paths.
- The empty pair `{#cc:N}{#/cc:N}`, which spec-projection.md §3 names as
  sanctioned edit surface #1 and which CC-4/CC-5 will route through set_field
  fill semantics.

In both, an anchor legitimately disappears from `new_text` while the wrapper
survives in the document — the control's CONTENT is what changes. A symmetric
rule cannot tell that from a deletion, so the check is `cc`-scoped with the
empty pair (with or without its placeholder bubble, and requiring the SAME
ordinal on both halves) carved out explicitly. Both carve-outs are pinned by
tests, so a later tightening cannot quietly close them.

**Ordered, not multiset.** The first implementation compared sorted lists and
one of its own tests caught the hole: `{#cc:3}A{#/cc:3}` → `{#/cc:3}A{#cc:3}`
has an identical multiset while inverting the pair. Comparing the anchors in
document order refuses that, and costs nothing legitimate — text replacement
cannot move a `w:sdt` wrapper, so reordering controls is never a valid edit.
Note this makes the cc rule stricter than the neighbouring footnote and image
checks, which remain multiset comparisons; that is deliberate, not drift.

Both suites were run against the unfixed engines first: 10 of 17 failed in each.

## 2026-08-21 — CC-14: redline replay silently produced the wrong document (osx)

The row was filed as one bug. It was two, independent, both pre-existing, and
the property search had only ever reached the first — fixing it immediately
surfaced the second from the same test.

**Defect 1 — python only.** `"0 0." → "0.\n\n0."` diffs to target `"0 "` /
new `"0.\n\n"`. The rstrip "Smart Fallback" in `_resolve_single_match` rstrips
the target to `"0"`, sees the replacement extend it, and inserts the remainder
BEFORE the document's trailing space. That is correct for a separator space
inside one paragraph — `"Section 1 " → "Section 1 Revised"` must not glue the
following word on — and wrong as soon as the remainder carries a paragraph
break, because the preserved space becomes the first character of the new
paragraph: `'0.\n\n 0.'`. Guarded so that shape falls through to the F1 rule
ninety lines below, which already resolves it as one atomic modification over
the whole matched span.

`@adeu/core` has no equivalent branch and was already right, so this was a
dual-engine parity break as well as a correctness bug. The correct Node
behaviour is now pinned so it cannot regress toward Python's.

**Defect 2 — both engines.** With defect 1 fixed the property immediately found
`['0.','0 0.','A.','00.'] → [...,'0.','A.','0.',...]`, which produced `'0.A.'`:
a lost paragraph break. `trim_common_context` is word-boundary aware and
deliberately will not trim across `"\n\n"`, so `_single_commented_sub_edit`
received the span whole, mark included. The apply layer then track-deletes a
trailing mark inside a target — a genuine merge `"A.\n\n" → "Z."` depends on
exactly that — but never re-creates the one the replacement asks for.

Rather than reverse-engineer the apply layer's paragraph accounting, a shape
matrix established precisely what is broken: a mark shared by BOTH sides. A
shared LEADING mark is fine, a genuine merge is fine, a mark-free split is
fine. So the fix normalises the span before the apply layer ever sees it,
reducing every broken case to one already verified correct, and leaving the
merge shape untouched.

**Only found because the fix was checked at both entry points.** The first
version of the fix lived in the resolution path alone, and the new tests
(written to drive the apply layer directly, by pinning an index) still failed.
Caller-pinned edits skip resolution entirely. Measuring rather than guessing:
`make_edits_self_contained` widens targets for uniqueness and emits this exact
shape WITH a pinned index in 129 of 4,000 randomised paragraph edits. The
property test never caught that half because its JSON round trip drops the
private index — so an in-process caller was silently losing paragraph breaks on
a path the property suite structurally could not reach. Both entry points now
normalise through one shared helper.

**Note for CI.** `.hypothesis` is gitignored and the default profile is 25
examples; on a cleared cache neither defect reproduces. Confirmed by running
the clean tree — the property passes. Both falsifying examples are therefore
pinned as explicit regression tests, with the underlying shape matrices, in
both engines.

Unrelated observation while verifying: the two `_loads_in_libreoffice` tests in
`test_repro_qa_2026_07_18.py` fail intermittently under xdist (a different one
each run, sometimes neither) and pass 55/55 serially. Reproduced on a clean
tree at 2 of 3 full-suite runs, so it is pre-existing and not CC-14's. It is
the same class as the flake already noted for `TestC1FooterBoundary`; worth its
own row if it starts costing anyone time.

---

## 2026-08-21 — CC-1c checkbox projection + six decisions from Mikko (opencode-windows)

CC-1c landed (`7ab331f`), CC-1 is complete, and the blocked list was worked through
with Mikko. Recording the engineering findings first, the decisions second.

### What CC-1c actually cost, and why the numbers look wrong

A `w14:checkbox` now projects `[x]` / `[ ]` in both engines, ingest and mapper, both
views. The mark is read from `w14:checked` and never inferred from the glyph — a COM
finding, not a preference: Word restores `w14:checked` when a toggle is rejected, so
the attribute is the settled value while the glyph can lag it inside a pending
revision. `[` and `]` are virtual; the mark is a **real run-backed span** over the
glyph run. Because Word writes the glyph as literal `w:t`, the substitution is one
character for one character and no offset arithmetic downstream changes.

Three things the corpus taught that the spec did not:

1. **A checkbox is not always inline, and §4 assumed it was.** 11 of
   `odot_uic_drywell`'s 19 checkboxes are cell-level — a `w:sdt` whose parent is `w:tr`,
   wrapping an entire `w:tc` that holds nothing but the glyph, which is how Word writes
   a checkbox column in a form table. Those never pass through the `w:sdt` branch of the
   traversal at all. This is why both engines substitute at **run emission** instead:
   it is the one point every path reaches, so the fix is path-independent by
   construction rather than by enumeration. The `w:sdt` branch keeps only the degenerate
   no-glyph fallback, so a generated control cannot collapse to a two-character `[]`.
   §4 amended with a clarification (not a behaviour change) on Mikko's sign-off.

2. **The corpus size went DOWN, which looked like a bug and was not.**
   `odot_uic_drywell` moved 7,449 → 7,435. Expected was +38 (19 controls × 2 chars of
   token width). Attribution, verified against a baseline worktree at `d05621c` rather
   than reasoned about: of the document's 21 ballot glyphs, 19 sit in controls and
   **13 of those arrived wrapped in emphasis markers**, projecting as `**<glyph>**`.
   The mark is chrome and now carries none, so −52 for 13 × 4 dropped marker characters
   against +38 of width, net −14. Exact. The old projection was literally handing an
   LLM `**☐**`. `fedramp_ssp_rev4` moved +7,762 on both views = 3,881 checkboxes × 2,
   with no emphasis involved. Both engines agree on all four values; the parity tables
   carry the attribution inline so the next reader does not have to redo this.

3. **The two bare prose glyphs are the whole reason the gate is where it is.** The
   glyph test runs BEFORE the walk to the enclosing control — which keeps the ancestor
   walk to ~7,700 runs rather than 559,000, but more importantly is the correctness
   boundary. `odot_uic_drywell` carries two `☐` runs in ordinary prose outside any
   control; substituting on the character alone would fabricate two checkboxes in a
   document with 19 real ones to hide among.

### A Windows-only test bug that made CC-1c unverifiable here

Node's `golden()` helper read `fixture-standard.md` with `readFileSync` and matched the
code fence as `\n```\n`. Git checks that file out CRLF on Windows, so the match returned
null and both golden tests died in `m![1]` with a bare `TypeError` — a failure that
reads like a projection bug and is not one. Green on macOS, red on every Windows
checkout, which is why it survived. The python twin never had it because
`Path.read_text()` translates newlines; `readFileSync` hands back the bytes as they are.

Normalised on read, and `ccFixtureBodyXml()` hardened the same way. That second one is
currently masked by `trim()` — the shared fixture body is one line — but the day anyone
reformats `cc_fixture.body.xml` to multi-line, python and node would build their fixture
from **different bytes on Windows only**. Cheaper to close than to diagnose later.

### Mojibake in the shared docs, from this side

Separately (`ecefccb`): TASKS.md and PROGRESS.md were carrying 47 and 50 mojibake
sequences — em-dashes stored as `â€”`, plus quotes, ellipses and a ballot glyph. A
UTF-8 file read as cp1252 and re-encoded. This is the exact trap AGENTS.md documents for
`Set-Content -Encoding utf8`, and the corrupted lines are the older sections while
recent writing is clean, so it came from this side. Repaired per-run rather than by a
whole-file round-trip, because both files also hold genuine em-dashes that would have
been destroyed. **Implication worth flagging: any earlier PowerShell-authored edit to a
shared doc is suspect.**

### Decisions from Mikko, 2026-08-21

- **G5 / forms protection → refuse by default, explicit opt-in.** Word writes the
  permitted fills untracked and *reading* `TrackRevisions` throws, so the "always
  tracked" contract is unenforceable there. New `allow_untracked_writes` param (default
  `false`, CLI `--allow-untracked-writes`), unlocking G5 only; without it a teaching
  error naming the cause, with it a report note on **every** untracked write. Kept
  separate from `ignore_document_protection` on purpose: that bypasses a gate the author
  set, this accepts a downgrade of Adeu's own guarantee, and the G5 writes are ones Word
  itself permits. spec-gates.md §1a. Closes CC-6.
- **Bound-store reject → pulled from CC-9 (P3) into CC-5 (v1).** A headless reject
  leaves the store holding the rejected value and the store wins on open, so Word
  re-applies it: a reject that undoes itself, silently. Same class as CC-14. v1 must
  either dual-write on reject or refuse `RejectChange` inside a bound control. Closes
  CC-6's second sign-off item.
- **CC-13 → closed, live-Word stays in the pre-push hook.** Quarantine offered and
  declined; residual flakiness accepted as out of scope for this initiative. The honest
  residual stands on the row.
- **spec-projection.md §4 → clarifying sentence** for cell-level checkboxes. Confirmed
  as documentation, not behaviour.
- **CC-3 → split.** The finished mechanism closes; the dependency-blocked A5 tail is now
  **CC-3b**.
- **CC-1f → CC-2.** A1.9's banner touches the CLI and both MCP servers, which is CC-2's
  surface; doing it in CC-1 meant visiting them twice.

### Board effect

**CC-1 is `done`** (1a `38444d2`, 1b `eb0a141`, 1c `7ab331f`, 1d `a576f34`,
1e `c532d5b`; 1f → CC-2). That unblocks **CC-2, CC-4, CC-5** and A5.5 in CC-3b.
CC-3, CC-6 and CC-13 all moved to `done`. Verification at `7ab331f`: python
1,733 passed / 7 skipped including all 43 live-Word tests on a cold Word, ruff and mypy
clean; node 853 + 306 + 42 passed, build and lint clean.

## 2026-08-21 — CC-16: the LibreOffice interop harness was reporting scheduling (osx)

Filed and fixed the flake noted at the end of the CC-14 entry. It deserved a row
rather than a footnote, because the visible symptom was the *less* important half.

**The measurement changed the diagnosis.** My hypothesis was the 5-second
timeout. Wrong: a single conversion takes ~0.9s warm, and every failing worker
in an 8-way run reported a clean, fast completion — no timeouts at all. The
process exits 0 and simply writes no PDF. Concurrent `soffice` invocations that
share a user profile do not both convert; the second finds the first one's lock,
hands its request over and exits successfully having done nothing. `lo_loads`
only checks whether the PDF appeared, so "another worker held the profile" and
"LibreOffice rejected this document" are the same observation. At 8-way, sharing
converted a known-good file 4/8 times; a private profile per process, 8/8.

**The quiet face is the one that mattered.** `soffice_can_convert` caches per
process, so a probe that loses the race marks LibreOffice unavailable for that
entire xdist worker and every interop assertion on it skips. Five full-suite
runs unfixed: interop tests skipped in four of them (1, 0, 2, 1, 2). Fixed: zero
skips and zero failures, five for five, identical counts. So the QA C1 and H4
interop regressions — a body edit landing in `word/footer1.xml`, a comment
anchored in a footnote, both real shipped bugs this file exists to pin — were
usually not being checked at all, and the suite said green. A test that fails
randomly is annoying; one that silently stops running is how a regression ships.

**Checking the fix hadn't neutered the tests found a second defect.** I ran the
repaired helper against deliberately broken files. Truncated and malformed-XML
inputs were correctly rejected — but a `.docx` whose entire content was the
bytes `this is definitely not a zip` came back as loading *fine*. With no input
filter pinned, LibreOffice sniffs the content, falls back to a plain-text
importer, and produces a perfectly good PDF of the garbage. The interop
assertions could therefore have passed on a file Word would refuse outright.
Pinning `--infilter=MS Word 2007 XML` gives the right answer on all four cases.

**The timeout still moved, for a different reason than I first thought.** Not
because 5s was too tight for a conversion, but because with private profiles the
first run in each worker also *builds* one: ~3.5s at 8-way, and past 5s at
12-way, where all twelve workers then timed out together. A private profile
alone would have traded one flake for another on a wider machine. Raised to
120s and reframed as a hang guard, not a performance assertion. Profiles are
built once per worker and reused (~1.7s warm) rather than per conversion.

**Note on the regression test.** It spawns real subprocesses. Threads would not
reproduce it: the profile is per-process *by design*, so in-process concurrency
shares it legitimately, and a thread-based test would fail against the fixed
code. Three processes is the cheapest reliable reproduction — sharing gave 2/3
on every trial, while 2-way only failed 2 of 3 times. 5 of the 9 new tests fail
against the unfixed harness, including both behavioural ones.

Scope note: this is the cross-platform LibreOffice twin of CC-13 (Windows,
live-Word COM). Same class — a suite asserting about the environment rather than
the code — different mechanism, and the two fixes do not overlap.

## 2026-08-21 — CC-2: fields ledger, banner, appendix summary, edit labels (osx)

A2.1-A2.7 and A1.9 met in both engines across CLI, both MCP servers, the serve
daemon and the appendix. The renderer lives in the engine (`adeu/fields.py`,
`@adeu/core/fields.ts`) because four surfaces emit the same text and spec §7
calls the line format an output contract — a second implementation would be a
second dialect. Both engines render GOLDEN-LEDGER and GOLDEN-BANNER
byte-for-byte.

**The ledger reads the projection, not the DOM.** A control's rendered value
has already survived table flattening, CriticMarkup and the placeholder rules,
so re-deriving it from `w:t` would produce a ledger that disagrees with the
text the agent is editing. The proof is CC:15, a row-level control whose value
is the markdown row `Approver | Jane Roe` — a DOM walk gives `ApproverJane Roe`.

**Benchmarking before wiring the banner caught two quadratics of my own
making.** Collecting fields on fedramp_ssp_rev4 took **8,776ms** against 452ms
for the whole projection: the ledger was twenty times more expensive than
producing the document it describes. Anchor lookup ran a regex over the entire
text once per control (5,007 controls × 600 KB), and `heading_path_at`
re-splits the projection on every call — fine for a handful of search hits,
quadratic for thousands of rows. One `finditer` pass and a precomputed
binary-searched heading index took it to **115ms**, a 76x speedup. A faster
answer is only worth having if it is the same answer, so both engines pin the
new index against the function it replaced at *every offset* of a multi-level
document, and python also over every anchor offset of a real corpus file.

**Then the banner turned out not to need any of that.** It renders four
numbers. Getting them from the full ledger meant 115ms of value previews and
breadcrumbs that nothing displays, so `field_summary` walks the ordinal
pre-pass and stops. That leaves two ways of counting, which is two chances to
disagree — and a banner contradicting the ledger it advertises is worse than no
banner — so the agreement is pinned on the fixture, on edge shapes, and on a
real 19-checkbox corpus template. Even the cheap path is 68ms to load plus 82ms
to classify 5,007 controls, repeated on every full-view read for values that
cannot change while the bytes do not, so it is memoised on the stat signature
and bounded at 32 entries. A test pins that an edited file gets fresh counts
rather than the previous document's.

**Two frozen-spec contradictions, both flagged on the board.** §1 says `--json`
wraps the ledger as `{"content": …}` "per CLI stream conventions", but no such
shape exists anywhere — every mode emits `{markdown, title, file_path}`. And
A2.7 wants `fields_offset` to both "mirror `changes_offset`" and publish
`type: number`, which it cannot, because `changes_offset` publishes `integer`.
Both resolved toward the implemented convention; the offset assertion is
written AS parity so it tracks `changes_offset` rather than freezing a literal.

**Duplicate work caught at rebase.** Windows landed `utils/protection.py` for
CC-4's gates while this was in flight, so `w:documentProtection` briefly had two
parsers. CC-4's won — it runs on every engine load and is the more defensive.
The wordings genuinely differ and both are pinned (`describeProtection()` says
"read-only, enforced" for gate errors per A3.4; the banner says "read-only
(enforced)" per spec §7), so the parse is shared and only the rendering differs.

Smaller things worth knowing: `w:temporary` was extracted nowhere despite being
in the frozen line format; `clean_breadcrumb`/`heading_path_at` and the node
golden-block reader were hoisted so the ledger and search cannot drift;
`cc_fixture_bytes` gained the `body_xml` parameter node already had, without
which A2.2 (protected, zero controls) and A2.3 (250 controls) were not
constructible in python. Per-edit reports now name the innermost containing
control, not the outermost — CC:9's own lock governs the edit, not the group
CC:8 that wraps it.
## 2026-08-22 — CC-4 write gates complete (windows)

Gate matrix, both engines, all surfaces. Python 1,764 passed / 16 skipped; node
922 + 306 + 42; ruff, mypy and eslint clean. Commits: `56aabeb`, `808e829`
(identity), `34330d9` (protection state), `6836cb5` (python gates), `71d5ce4`
(node gates + surfaces), plus the closing commit for widening, COM agreement
and the spec corrections below.

### The architectural gap this row actually had to close

Every gate in the matrix is one question wearing different clothes: *does this
edit's text sit inside control X, and what does X permit?* Neither engine could
answer it. `TextSpan` carried `part_index` — which is exactly this shape of
answer for OPC part walls — and nothing for control walls. So the first two
commits build `TextSpan.sdt_stack` alongside it, `control_ranges` alongside
`part_ranges`, and `controls_at` / `controls_intersecting` alongside
`part_kind_at`. Modelling on the part wall was deliberate: it is the same
problem with a decade of scar tissue (QA 2026-07-18 C1) and a proven
three-layer shape — mapper field, validate refusal, apply backstop.

A span's stack has two sources because neither alone covers the document. The
run walk sees inline controls and is blind to block ones; the mapper's block
cursor sees block controls and is blind to inline ones. CC:9 — an inline
control nested in a block group — cannot be produced by either alone.

### Four findings that changed the work

**Table controls are a third structural kind.** CC:14 (cell-level) and CC:15
(row-level) wrap `w:tc`/`w:tr` and are reached through `_map_table`, not the
block or run walks. The first cut of the identity field left them with no
ranges at all, so every gate would have silently permitted edits inside them.
Caught by asserting the WHOLE set of content-bearing controls rather than
samples — the failure mode here is permissive and quiet, which is the argument
for the stronger assertion.

**CC:2 has no content range, and that is correct.** It is empty, and its
placeholder ghost projects as a virtual bubble rather than as content. So G8
cannot be a span-intersection gate like its siblings and instead matches the
target against the control's placeholder text. Learned from a fixture rather
than from a failing acceptance example.

**A3.5 contradicted spec-gates §1a; §1a governs.** A3.5 says the
forms-protected fills "(b) and (c) apply". §1a — Mikko's 2026-08-21 decision on
CC-6's finding — adds a second gate on exactly those writes, since Word records
them untracked and reading `TrackRevisions` throws there. A3.5 predates the
decision and was never restated. Implemented per §1a and A3.5 corrected. Also
pinned: the two protection parameters are NOT interchangeable, because §1a is
emphatic that they are different admissions — one bypasses the author's gate,
the other concedes Adeu's own output guarantee.

**A3.11 does not exercise G15.** Its example (merging out of block CC:1) is
refused one layer earlier by CC-1e's anchor gate, because the merge would have
to delete `{#/cc:1}` — and that is a *more* precise error, so it stays. G15's
real job is the UNANCHORED walls: repeating-section items, checkboxes,
pictures, which project no tokens and have no anchor gate protecting them. A
merge across a repeating-item wall would otherwise silently hoist one item's
content into the other. Both cases now pinned, each asserting which gate fires.

Related: **A3.10 was already satisfied** by `trim_common_context`, which
narrows the crossing edit to the outside-the-control part before any gate sees
it. The genuine crossing is when both sides change, and there the word-diff
splitter already lands each half correctly. What was actually missing was not
segmentation but DISCLOSURE — an agent that asked to change CC:3 and silently
got a change half outside it was told something untrue by omission. Now a
per-edit report note.

### G10 and G12 are deferred to CC-5, not skipped

Both gate `set_field` values, and `set_field` exists in neither engine. They
are in the §2 matrix for completeness but have no operation to gate. Recorded
on A3, on CC-4's row and on CC-5's, which now also inherits the obligation to
make CC-4's "use set_field instead" advice true for bound controls rather than
a dead end.

### COM agreement is now pinned, not assumed

CC-6 measured what Word permits; A3 pins what Adeu decides; nothing connected
them, so a gate could have been changed into disagreeing with Word and both
suites would have stayed green. `test_live_word_gate_agreement.py` drives real
Word over the same document Adeu gates and asserts the verdicts match — for
`sdtContentLocked`, `sdtLocked` and unlocked controls — plus that
`ignore_control_locks` lands exactly where Word lands once `LockContents` is
cleared. That last one matters because an override that skipped the gate while
the edit still could not apply would convert a clear refusal into a silent
no-op, which is the failure spec-gates §7 exists to prevent.

The one asymmetry left deliberately in place: Adeu is stricter than Word for
G13 (data binding), because there the write succeeds in Word and then silently
reverts on open (CC-6(e)). Being stricter than Word is right when Word's
permission is a trap.

### Context widening

`make_edits_self_contained` now clamps at locked and group walls, so `diff`
output stays closed under `apply` — without it the tool would emit batches its
own engine rejects. The walls are read back out of the projection
(`{#cc:7 locked}`) rather than passed in from the mapper: the function receives
only text, and deriving the walls from that same text makes it impossible for
the wall list to describe a different document than the one being widened. Node
has no `make_edits_self_contained` (its diff is per-part and structural), so
there is nothing to clamp on that side.
