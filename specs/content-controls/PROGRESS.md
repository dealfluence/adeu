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
