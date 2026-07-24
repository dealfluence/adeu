# DOCX Engine Performance: How the Large-Document Gains Were Made

**Status:** Phases 1 (anchor-fallback index), 4 (server-layer projection
cache), and 5 (lazy transactional snapshot) shipped in the TypeScript engine
(2026-07-24). This document describes the work in implementation-neutral
terms so the same gains can be reproduced in the Python engine, which shares
the identical architecture and algorithms.

---

## 1. Symptom

Reading an 8.9 MB DOCX (a 45 MB `word/document.xml`, ~2.7 M XML elements,
3,553 XML parts) through `read_docx` timed out at the client. Measurement
showed one full-mode read took **165.6 seconds**. After the first fix the same
read takes **16.1 seconds**, with byte-identical output.

| Stage (stress document)         | Before   | After   | Speedup |
|---------------------------------|----------|---------|---------|
| Container load + XML parse      | 11.5 s   | 12.5 s  | —       |
| Text projection (+appendix)     | 152.3 s  | 2.4 s   | 62.6×   |
| Split + paginate                | 1.8 s    | 1.2 s   | —       |
| **Total `read_docx` (full)**    | **165.6 s** | **16.1 s** | **10.3×** |

A 0.4 MB control document was unaffected (0.66 s → 0.71 s, within noise).

---

## 2. Method: measure, predict, fix, prove equivalence

The discipline that produced the gain matters more than the specific patch,
and it ports directly:

1. **Stage-level benchmarks, not end-to-end guesses.** A standalone script
   times each pipeline stage in isolation: container decompression, XML
   parsing of the main part alone, XML parsing of every other part, text
   projection with and without the structural appendix, pagination, outline
   extraction. Working-set size is recorded per stage.

2. **A structural census that *predicts* hot spots before running them.**
   After parsing, one linear walk over the main part counts elements,
   paragraphs, runs, tables, rows, cells — and, crucially, counts how many
   cells satisfy the *trigger condition* of each suspected pathological code
   path. For the stress document this predicted ~1.15 billion node visits
   from 430 trigger cells before any projection was run. The prediction and
   the observed 152 s agreed, which confirmed the diagnosis and ruled out
   guesswork.

3. **Golden captures before touching anything.** With the unmodified engine,
   capture to disk: the raw projection (tracked-changes markup view), the
   clean projection (accepted view), and the editor-side projection (the
   mapper's virtual text) for a small control document, the stress document,
   and a synthetic fixture purpose-built to exercise every branch of the code
   being changed. After the change, re-extract and **byte-compare** all of
   them. Performance work on this engine is only safe when the projection is
   provably unchanged, because anchors and IDs in the projection are a
   contract with downstream agents.

4. **Full regression suites** in both the core engine and the MCP server, plus
   new unit tests that pin the changed algorithm against a verbatim copy of
   the *old* algorithm.

---

## 3. Root cause: the empty-cell anchor fallback was quadratic

### 3.1 What the code path is for

Every table cell is projected with a stable, document-native anchor
(`{#cell:<id>}`) so agents can address empty cells. The id is normally the
`w14:paraId` that Word stamps on the cell's first paragraph. Documents not
produced by Word often lack `paraId`s entirely; for an **empty, unlabeled**
cell the engine derives a deterministic fallback id:

```
index  = document-order position of the cell's first paragraph
         among ALL <w:p> elements of its XML part
id     = FNV-1a-32("fallback-paraId-" + index), rendered as
         8-char uppercase hex (zero-padded)
```

The paragraph is then stamped with the derived id (an attribute write into
the in-memory tree) so that later passes — and any process re-reading the
saved file — resolve the same anchor.

### 3.2 Why it was quadratic

The historical implementation computed `index` by **materializing every
paragraph in the entire part and searching for the target**, once **per
fallback cell**:

- collect all `<w:p>` descendants of the whole part → O(document size)
- find the target's position in that list → O(paragraph count)

That alone is `O(fallback cells × document size)`. Two amplifiers made it
worse in practice:

- **Mutation invalidated traversal caches.** The same code path *writes* to
  the tree (stamping the derived id, occasionally creating a missing
  paragraph). In DOM libraries whose descendant collections are cached
  against a tree revision counter, every write invalidates every cached
  collection — so each of the 430 scans re-walked all 2.7 M nodes from
  scratch. (Libraries without such caching pay the full walk every time
  regardless, which is the same outcome.)
- **Twin execution.** The projection exists twice by design — once in the
  reader (ingest) and once in the editor-side mapper, which must produce
  byte-identical text. Both twins contained the same scan, so editing paths
  paid it too, once per mapper (re)build.

The stress document has 6,313 cells, all without `paraId`s, of which 430 are
empty → 430 whole-document scans ≈ 1.15 billion node visits ≈ 150 seconds.

### 3.3 The fix: a per-document paragraph-position index

Replace the per-cell rescan with a lazily built index over the part:

- **One preorder walk** of the part builds `paragraph element → document-order
  position` for every `<w:p>`. Documents that never hit the fallback never
  pay for it.
- Each fallback lookup is then O(1).
- The two twins share one implementation of the whole resolution step
  (find first paragraph → read existing id → derive-and-stamp fallback),
  eliminating the duplicated logic. Each twin passes its own emptiness
  predicate unchanged (the reader keys on "projected cell text is empty",
  the mapper on "projected width is zero") — these are subtly different and
  must not be unified.

### 3.4 Freshness rules (the part that makes it *correct*, not just fast)

The index is a cache over a mutable tree, so its invalidation rules are the
heart of the change. They reproduce the historical behavior exactly:

1. **Foreign mutations invalidate.** If anything else mutates the tree
   between resolutions (e.g. the editing engine inserting paragraphs between
   two mapper builds), the next lookup must rebuild. The reference
   implementation keys freshness on the XML library's tree revision counter;
   any equivalent signal works (explicit dirty flag, rebuild-on-miss, or —
   worst case — rebuild per projection pass). What is *not* acceptable is
   serving positions computed before a foreign mutation: a freshly loaded
   process would compute different positions, and ids would diverge across
   processes, breaking anchor stability.
2. **The algorithm's own id-stamping write does not invalidate.** Stamping an
   attribute cannot change the paragraph set or its order, so the index is
   explicitly kept valid across it. This rule is what breaks the
   mutation-amplifier loop: without it the cache would self-invalidate on
   every fallback cell and the fix would be no fix.
3. **The algorithm's own paragraph creation rebuilds.** When an empty cell
   has no paragraph at all, one is created and appended before its position
   is taken. A created paragraph is absent from any existing index, so the
   lookup detects the miss and rebuilds — the rebuilt index includes the new
   paragraph at exactly the position the historical full rescan would have
   found. This is rare (only paragraph-less cells) and self-limiting.

### 3.5 Invariants any port MUST preserve

- **The id derivation is a cross-engine contract.** 32-bit FNV-1a over the
  ASCII string `fallback-paraId-{index}`: offset basis `2166136261`, and the
  multiply step expressed as shift-adds
  (`hash += (hash<<1)+(hash<<4)+(hash<<7)+(hash<<8)+(hash<<24)`), all in
  32-bit unsigned arithmetic (mask with `0xFFFFFFFF` in languages with big
  integers), rendered as uppercase hex, left-padded to 8 chars. Both engines
  and every process must derive identical ids from identical documents.
- **`index` is the position among ALL paragraphs of the part** (including
  paragraphs inside nested tables, text boxes, etc.), in document order —
  not among body-level paragraphs only.
- **Stamping persists.** The derived id is written onto the paragraph so
  repeat resolutions (same pass, later passes, saved output) take the
  "existing id" path. Repeat resolution of the same cell must return the
  stamped id, not re-derive.
- **Cells with content but no id get NO anchor** (historical behavior — the
  fallback only fires for empty cells).
- **Emptiness predicates stay per-twin** (projected-text-empty vs.
  projected-width-zero).

### 3.6 Verification protocol used (reuse it for the port)

- Synthetic fixture with: a labeled cell, a text cell without id, empty
  cells with unlabeled paragraphs, an empty cell with **no** paragraph
  (creation path), a nested table with empty cells, and a second table —
  the resulting anchor pattern exercises every branch.
- Unit tests pin the new resolver against a **verbatim copy of the old
  algorithm** run on an identically constructed twin document, cell by cell,
  including a scenario where a foreign mutation shifts positions between two
  resolutions (catches stale-cache bugs).
- Reader and mapper projections compared for equality on a fallback-heavy
  document; save → reload → re-project must be identical.
- Golden byte-comparison as described in §2 (three documents × three views).

---

## 4. Where the remaining time goes (measured after the fix)

For the stress document, one full read is now 16.1 s:

| Cost center | Measured | Notes |
|---|---|---|
| XML parse, main part (45 MB) | 6.9 s | Parser throughput ~10 MB/s; parser option tuning yielded only ~6% — the cost is per-node object construction. |
| XML parse, 3,540 other parts | 3.8 s | Header/footer parts of this document are almost all *used* by projection, so lazy parsing helps little *here*; it still helps documents with many unused parts. |
| Text projection | 2.4 s | Linear; includes ~0.6 s building the structural appendix that full-mode reads discard except for one "appendix exists" flag. |
| Pagination | 1.2 s | Linear string processing. |

And the editing path (one tracked-change edit on the stress document):

| Cost center | Measured | Notes |
|---|---|---|
| Load | 11.4 s | Same parse floor as reading. |
| Engine construction (mapper build) | 2.7 s | One full projection. |
| `process_batch`, single edit | 25.9 s | Dominated by **full mapper rebuilds after each applied edit** (the editor rebuilds its projection from scratch per edit, sometimes twice — raw and clean views — plus preview projections for the report). |
| Save | 5.2 s | **Every** part is re-serialized and re-compressed, including the ~3,539 untouched ones. |

## 5. Roadmap (portable to both engines)

Ordered by user-visible value per unit of risk:

1. **Server-layer projection cache.** *(SHIPPED in the TypeScript server;
   measured on the stress document, end-to-end over the wire: cold first
   read 16.2 s, warm page turn 1 ms, warm search 25 ms, warm outline 5 ms,
   responses byte-identical to a cache-less server.)* Key: (absolute path,
   file modification time, file size). Value: projected text (+ pagination,
   + outline nodes), never the parsed tree. The parse cost is paid once per
   document *version*; page turns become string slicing. Invalidation is
   automatic (any rewrite changes mtime/size → new key; stat-checked every
   call). LRU-bounded (3 entries). Two lessons that port: (a) fill the
   clean/accepted view in the background AFTER the first response, but only
   once the server has been quiet for a few hundred ms — a synchronous
   multi-second fill started immediately will stall the page-2 request that
   typically follows; a client explicitly requesting the clean view skips
   the wait and pays for it directly. (b) When the client supplies a
   progress token, report parse progress during cold ingests and yield the
   event loop periodically so the notifications actually flush.
2. **Editing path — transactional snapshot.** *(SHIPPED in the TypeScript
   engine; stress document: single-edit batch 25.9 s → 2.9 s (9×), batch
   memory peak halved; 10-edit batch 21.8 s ≈ 2.2 s/edit marginal.)*
   Profiling — not intuition — found the cost: the batch rollback snapshot
   **deep-cloned every part's tree up front** (~2.7 M nodes), on every
   batch, successful or not. The fix inverts the cost: parts that are still
   "clean" (tree reconstructible from their pristine load-time XML) are not
   cloned at all — rollback re-parses that XML on the rare failure path.
   Three portable lessons:
   - Cleanliness must be tracked through the engine's OWN deterministic
     writes. Anchor stamps (§3) and the tracked-change namespace
     declaration the engine adds at construction are re-derived identically
     by any fresh pass, so they must not flip a part to "dirty" — the first
     implementation missed the namespace stamp and kept deep-cloning the
     45 MB main part; a cleanliness probe (count dirty parts at each
     lifecycle stage) caught it.
   - Restored-by-reparse parts get fresh tree objects; every restore caller
     must already rebuild its projection/comment managers (they did — but
     verify with a use-the-engine-after-rollback test, including a rollback
     that removes parts added mid-batch).
   - The remaining per-edit marginal cost is the full projection rebuild
     between sequential edits (the batch contract validates each edit
     against the state its predecessors produced). Incremental projection
     patching is the next frontier; per-batch cost is now linear with a
     small constant.
   Still open on this path: dirty-part-only save (reuse original bytes for
   untouched parts — the cleanliness marker now exists; note it changes
   which deterministic stamps get persisted, so decide stamp-persistence
   semantics first), and skipping clean-view rebuilds no consumer needs.
3. **Appendix on demand.** Full-mode reads should not build the defined-terms
   /anchors appendix they immediately discard; compute a cheap "appendix
   would be non-empty" signal during the main projection walk instead, and
   build the appendix only in appendix mode. (Also fix the typo-detector's
   candidate bucketing, which currently defeats its own first-letter
   bucketing for terms longer than 5 characters.)
4. **Parse floor — first make parsing RARER, then faster.**
   *4a (SHIPPED in the TypeScript server): hot-document reuse + output
   priming.* The edit tool used to re-parse from disk even when a read of
   the same file version had just parsed it, and the agent's
   read-after-edit parsed a third time. A single hot-DOM slot
   (stat-keyed, consume-on-take since edits mutate, TTL + heap-pressure
   valve) lets the edit take the read's parse; after a successful batch
   the in-memory post-edit document is adopted as the OUTPUT file's cache
   (products built in the background; DOM pinned for chained edits).
   Measured on the stress document, whole agent loop
   read→edit→read→edit→read: 131 s → 52 s (2.5×); edits 37→13 s, output
   reads 20→4.5 s. Portable invariants: (a) background fills reading a
   DOM must be forced to completion before the DOM is handed to a
   mutating consumer; (b) primed products must byte-equal a fresh parse
   of the written file (equivalence-gate test); (c) a DOM may go back in
   the slot after dry-runs and rolled-back batches (state provably equals
   the file); (d) save() must RE-BASELINE each part's pristine XML
   (serialized output becomes the new blob + clean marker) or the first
   chained edit pays the full-tree clone again.
   *4b (SHIPPED in the TypeScript engine): a purpose-built parser +
   minimal DOM.* The tokenization ceiling probe on the 45 MB main part —
   full spec parser 6.70 s, raw scan 0.15 s, scan + minimal node
   construction 0.49 s — showed ~93% of parser time was spec overhead
   (name-validation regexes, namespace resolution, live-collection
   machinery) that WordprocessingML machine output never exercises.
   The replacement implements EXACTLY the DOM subset the engine uses,
   established by auditing every member access in non-test code first:
   tree links, mutation ops (each bumping the document mutation counter
   the snapshot/caching layers contract on), literal prefixed tag names
   (namespace URIs are never consulted), snapshot (non-live) descendant
   queries (every call site materializes immediately), attribute
   get/set, text nodes, and a serializer. Measured: container load
   11.3 s → 1.87 s (6×); cold read over the wire 16.2 s → 4.9 s; whole
   agent edit loop 131 s → 29.5 s across phases. Adoption was gated on
   the full suites plus BYTE-IDENTICAL projection goldens across three
   documents × three views — the strongest equivalence evidence
   available, since every character of the projection passed through
   the new parser. Two portable adoption lessons: (a) audit-then-
   implement beats implement-then-chase — the two gaps the suite caught
   (namespace-variant element creation, nodes stringifying to their own
   XML) were API-surface omissions, not parsing bugs; (b) keep the old
   spec parser as a dev dependency and use it in tests as an INDEPENDENT
   cross-check of the new serializer's output.

## 6. Porting checklist for the Python engine

*(Executed 2026-07-24 — see §7 for what was actually found and shipped. The
bench scripts this checklist references were session-local and have since
been removed; §2 describes the methodology to reproduce.)*

- [ ] Reproduce the stage benchmark and structural census scripts (the
      TypeScript versions lived in `node/bench/`; they are ~200 lines each and
      translate directly).
- [ ] Run the census on the same stress document; confirm the same trigger
      counts (430 empty unlabeled cells).
- [ ] Locate the twin fallback implementations (reader ingest + mapper) —
      the Python engine has the same "collect all paragraphs of the part,
      find position" expression per empty cell. Note: a C-backed XML library
      makes the constant smaller but the asymptotics identical; measure
      before assuming it is negligible.
- [ ] Extract the shared resolver with the position index and the three
      freshness rules (§3.4). If the XML library exposes no revision
      counter, prefer "rebuild once per projection pass" over any scheme
      that risks serving stale positions.
- [ ] Port the equivalence tests (§3.6) including the verbatim-old-algorithm
      pin and the foreign-mutation scenario.
- [ ] Capture goldens with the unmodified engine first; byte-compare after.
      The two engines' goldens should also match **each other** on shared
      fixtures (cross-engine parity is an existing project invariant).

---

## 7. Python engine port — what actually shipped (2026-07-24)

The §6 checklist was executed and the census MATCHED (2,682,269 elements,
6,313 cells all without `paraId`, exactly 430 empty unlabeled cells) — but
the central §3 assumption did not: **the Python engine has no empty-cell
fallback at all** (it emits `{#cell:…}` only when a `paraId` already
exists), so the quadratic scan never existed there and porting the anchor
index was moot. Porting the FNV fallback itself is a *parity* feature (it
changes projection output) and was deliberately deferred. Python's costs
were different, and were found by porting the bench harness (a Python
mirror of the Node one) and profiling, not by assuming Node's profile.

### 7.1 What was measured, then fixed (VVBIG stress document)

| Cost | Before | After | Fix |
|---|---|---|---|
| `strip_bom_from_docx_bytes` | 2.4 s + 1.2 GB RSS spike, every load | ~1.1 s, +7 MB | Probe 3 bytes per XML entry; only re-zip when a BOM exists; validate by lxml-parsing the main part instead of a full python-docx load |
| w16du stamp in engine `__init__` | 1.1 s (tostring 45 MB + regex + re-parse) | ~0 | `etree.register_namespace("w16du", …)` + no eager stamp: tracked-change writes self-declare the prefix locally; docs already declaring it at root serialize byte-identically |
| `paginate` | 4.3 s (37 M `str.startswith` — char-by-char CriticMarkup depth scan) | 0.19 s | Single compiled-regex token scan; equivalence pinned against a verbatim copy of the old walk |
| `iter_document_parts_with_kind` | ~2 s (re-resolved `doc.settings` per section → 14 M relationship probes) | ~0 | Hoist the settings flag once per iteration (both projection twins share this iterator) |
| `mode='outline'` builder | 15.3 s | 1.35 s | Stop re-paginating; cache-backed style resolution (`paragraph.style` rescans the part's 3,547 rels per access — 52 M probes); lxml prefilter + memo for footnote refs (owned ranges overlap); precompute heading flags/levels |
| Server read path | no cache; sync on event loop | stat-keyed LRU-3 projection cache + `asyncio.to_thread` + progress relay + quiet-period background fills | Port of §5.1 with the same key/values contract (never the tree) |
| Pre-batch snapshot | full `save_to_stream()` every batch (2.8 s) | pristine load-time bytes while unmutated (~0) | §5.2's lazy-snapshot idea, Python-shaped: the engine keeps its sanitized input bytes; `apply_edits`/`apply_review_actions` flip a mutation flag; rollback re-inits from whichever bytes were chosen |
| Dry-run | full `save_to_stream()` + second engine (36.6 s) | pristine-fed second engine (25.8 s) | Same mutation flag |

### 7.2 End-to-end (VVBIG, measured)

| Flow | Before | After |
|---|---|---|
| `read_docx` full, cold | 18.1 s | 13.1 s |
| `read_docx` warm page turn / outline / search | 18.1 s / ~30 s / 18.1 s (all cold, every call) | 3–5 ms / 2.8 ms / 63 ms |
| Single-edit `process_document_batch` | ~40 s | ~28.5 s (engine 14.9 + batch 11.1 + save 2.5) |
| Dry-run call | ~55 s | ~40 s |
| RSS peak (dry-run flow) | 4.9 GB | 4.5 GB (and loads no longer double the archive) |

Control document (BIGDOC, 0.4 MB): read path 0.9 s → 0.35 s warm-independent;
the full regression suite stayed green and the projection goldens
(raw/clean/mapper × cells/BIGDOC/VVBIG) byte-identical throughout the work.

### 7.3 Known remaining work (Python)

1. **Run-loop fusion (next frontier).** ~9.5 s of cold projection and ~11 s
   of every mapper (re)build is per-run Python overhead: each run's children
   are walked ~3× (`process_run_element` events, `get_run_style_markers`,
   `get_run_text`) plus a python-docx `Run` wrapper per run (568 K on the
   stress doc). Fusing these into one pass is the single biggest remaining
   win (it also cuts the per-edit sequential-rebuild cost) but touches
   twin-shared rendering code — do it golden-gated, and fix 7.3.3 first.
2. **Hot-engine slot (§5.4a analog).** Deferred: with the mapper build
   dominating construction, reusing the read's parse saves only ~2.7 s per
   edit; revisit after (1) changes the ratio. Output-file read priming IS
   shipped (background fill after slow batch saves).
3. **Twin drift — FIXED (2026-07-24, same day).** Reader and mapper
   projections were NOT byte-identical on real documents (196 diff hunks on
   BIGDOC alone). Six mechanisms, ALL on the mapper side (the reader is
   canonical and its bytes did not change): (a) style-marker elision failed
   across boundary whitespace (`**Request for** **Bids**`); (b) elision
   popped the closing marker without confirming the incoming run actually
   opens one, losing balance on whitespace-only same-style runs
   (`**March 2012 ` with no closer); (c) redline state-transition events
   flushed the pending wrapper group, splitting one replacement into
   per-run `{--…--}` blocks; (d) empty parts still contributed part
   separators (4 extra leading newlines); (e) empty tables/footnote entries
   likewise; (f) a styled run whose only child is a drawing/reference
   emitted dangling markers (`(docx-image:1)****`), plus a per-run meta
   snapshot the reader only takes for text-projecting runs, and the
   cell-anchor space separator ignored the reader's endswith(" ") check.
   All six twins (cells/BIGDOC/VVBIG × raw/clean) are now byte-identical —
   each mechanism and the
   `extract(include_appendix=False) == mapper.full_text` contract were
   verified with dedicated parity checks during the fix. One
   dependent semantics fix: validate_edits now drops raw-view matches that
   live entirely inside tracked deletions BEFORE its clean/original-view
   fallbacks (the aligned mapper made such text matchable, silently
   bypassing the inside-a-deletion diagnostic that fragmentation used to
   provide by accident; the apply-time resolver always filtered these).
   Mapper goldens were recaptured; reader goldens byte-unchanged.
4. **FNV fallback anchors (parity, deferred by decision).** Port would make
   Python emit Node's derived `{#cell:…}` ids on paraId-less docs; port it
   WITH the §3.3 index from day one. Cross-engine goldens can't match until
   this and the nested-table divergence (Node duplicates nested cells as
   parent-row columns; Python nests inline — Python's rendering was chosen
   as the keeper) are resolved.
5. Appendix cost (~4.2 s, appendix mode only) — `domain.py` typo-detector
   still defeats its own first-letter bucketing for >5-char candidates
   (same as node's §5.3 note); left semantics-identical on purpose.

---

*The bench harnesses (stage, pipeline, edit benchmarks; golden
capture/compare; synthetic fixture generator) existed as session-local
scripts in `node/bench/` and `python/bench/` and were removed after the
work landed — every headline number they produced is recorded in this
document. The methodology (§2) is what to reproduce, in either engine,
before the next round of performance work.*
