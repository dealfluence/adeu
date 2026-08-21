# Progress Log — Content Controls

Append-only. Newest entries at the bottom. Every entry: date, author (human or agent),
what happened, and any decision/deviation with its justification.

---

## 2026-08-21 — Initiative bootstrap (Claude, with Mikko)

- Field research across 24 US/Canada public-sector .docx/.dotx (10 SDT-positive).
  Headline facts driving the design (full evidence in the proposal artifact):
  - FedRAMP SSP rev4: 5,007 SDTs — 3,881 checkboxes (plain `☒/☐` text runs, not
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
