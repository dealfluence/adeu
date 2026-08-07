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
   the slot after rolled-back batches (state provably equals
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
| Dry-run (historical — mode since removed) | full `save_to_stream()` + second engine (36.6 s) | pristine-fed second engine (25.8 s) | Same mutation flag |

### 7.2 End-to-end (VVBIG, measured)

| Flow | Before | After |
|---|---|---|
| `read_docx` full, cold | 18.1 s | 13.1 s |
| `read_docx` warm page turn / outline / search | 18.1 s / ~30 s / 18.1 s (all cold, every call) | 3–5 ms / 2.8 ms / 63 ms |
| Single-edit `process_document_batch` | ~40 s | ~28.5 s (engine 14.9 + batch 11.1 + save 2.5) |
| Dry-run call (historical — mode since removed) | ~55 s | ~40 s |
| RSS peak (dry-run flow; historical) | 4.9 GB | 4.5 GB (and loads no longer double the archive) |

Control document (BIGDOC, 0.4 MB): read path 0.9 s → 0.35 s warm-independent;
the full regression suite stayed green and the projection goldens
(raw/clean/mapper × cells/BIGDOC/VVBIG) byte-identical throughout the work.

### 7.3 Known remaining work (Python)

1. **Run-loop fusion (SHIPPED 2026-07-27 in two steps — see §8.2 and §8.3c;
   the projection is now 6.53 s, down from 8.91 s at the start of that day).**
   The original diagnosis was right: each run's children were walked ~3×
   (`process_run_element` events, `get_run_style_markers`, `get_run_text`),
   plus a python-docx `Run` wrapper per run (560 K on the stress doc).
   §8.2 fused the two leaf walks into one function (−0.68 s and −1.06 s in two
   parts); §8.3c then moved that work INTO the event stream's existing child
   walk, carried on a `ProjectedRun`, removing the second walk entirely
   (−0.81 s more). Cumulative: projection 8.91 → 6.53 s (−27 %), mapper build
   13.17 → 10.43 s (−21 %), end-to-end cold read −9 % (12.64 → 11.47 s).

   What remains in this area is SMALLER than expected and not obviously worth
   taking: the stream still costs ~3.96 s, which is per-child branch dispatch
   and per-run object construction. Eliminating the `Run` wrapper caps at
   ~0.4–0.5 s and would require giving the two twins different item types —
   precisely the twin divergence that caused the six drift mechanisms in
   §7.3.3. Measure before attempting; do not size it from a profile.
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
   All six twins (cells/BIGDOC/VVBIG × raw/clean) are now byte-identical;
   tests/test_twin_projection_parity.py pins each mechanism and the
   `extract(include_appendix=False) == mapper.full_text` contract. One
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

## 8. Cross-engine measurement + first fusion step (2026-07-27)

### 8.1 Where the Node/Python gap actually is

Both engines measured on VVBIG through their real MCP servers over stdio
(one client, identical timing code). Censuses are identical — 559,508 runs,
41,190 paragraphs, 548,757 `w:t`, 2,682,268 elements, 47.3 MB main part — so
the stages are directly comparable:

| stage | node | python (before §8.2) | ratio |
|---|---|---|---|
| load + XML parse | 1.84–2.30 s | 2.71 s (BOM strip 1.06 + `Document()` 1.65) | ~1.3× |
| **text projection (raw)** | **0.99 s** | **9.19 s** | **9.3×** |
| split + paginate | 1.14 s | 0.18 s | **0.16× — Python 6× FASTER** |
| mapper build (engine ctor) | 1.16 s | ~11.3 s | 9.7× |

The projection alone accounts for essentially the whole end-to-end gap
(cold read 4.76 s vs 12.64 s). Two conclusions that should steer future work:

- **Load is already near parity** — lxml is C. Don't spend effort there.
- **Node should port Python's paginator.** Python's regex token scan (§7.1)
  does in 0.18 s what Node spends 1.14 s on — 24 % of Node's cold read, for
  a port already proven in the other engine. This is the largest single
  Node-side win currently known.

The edit-path gap (6× on a 10-edit batch) is the SAME root cause amplified:
the batch contract rebuilds the projection between sequential edits, so
marginal per-edit cost ≈ one mapper build (Python 10.6 s/edit measured vs
Node 1.2 s/edit). A cold read pays the projection once; a 10-edit batch pays
it ~10 times.

### 8.2 Shipped: prefix reuse + leaf fusion

Both changes are byte-identical by golden proof (below) and live in
twin-shared code, so each was applied to BOTH twins:

| fix | before | after |
|---|---|---|
| `get_paragraph_prefix` computed **twice** per paragraph — once for the emitted prefix, again inside `is_heading_paragraph` (41,190 redundant calls) | 9.19 s | 8.51 s |
| `get_run_text` + `get_run_style_markers` fused into `get_run_text_and_markers(r_element, is_heading)` — one walk of the run instead of two, and no python-docx `Run` wrapper needed to read a run | 8.51 s | **7.45 s** |

Total projection −19 %. End-to-end on VVBIG: cold read 12.64 → 11.86 s,
single-edit batch 27.15 → 25.48 s, chained edit 41.31 → 37.23 s. Warm
(cache-hit) reads are unchanged, as expected — they never re-project.

Measured, not assumed, before committing:
- The two survivors (`get_run_text`, `get_run_style_markers`) are still used
  off the hot path (the meta lookahead, outline, sanitize), so they stay.
- Per-run cost of the pair vs the fused function, over all 560 K runs:
  **5.21 µs → 3.44 µs**.
- A variant adding a fast path for the dominant run shape (`[rPr, w:t]` with
  no `b`/`i` — **95.2 %** of runs) measured **no faster** (3.48 µs/run): the
  `list(r_element)` it needs costs what the skipped branches save. Left out
  deliberately; don't re-attempt without measuring.
- Classifying runs with lxml XPath predicates is a trap: `.//w:r[w:rPr/w:b]`
  plus the italic equivalent took **2.23 s**, slower than testing inline in
  the fused loop. Bulk C-level extraction is only worth it for grabbing node
  sets (compiled `.//w:r` = 0.16 s).

### 8.3 How fast can the Python engine get? (bounded, not aspirational)

A minimal Python loop that only appends each run's text costs 0.64 µs/run
(0.359 s over 559 K runs) — so raw interpreter dispatch is NOT the ceiling.
But the fused function, which computes what the projection actually needs, is
3.44 µs/run, while **Node's entire projection is 1.77 µs/run**. Python's
irreducible per-run leaf work is therefore ~2× Node's whole per-run budget.

Conclusion: **matching Node is not realistic in CPython + lxml; ~1.3–1.5×
is.** Full run-loop fusion (§7.3.1) should take the projection to roughly
6.0–6.5 s and per-edit to ~4 s, i.e. cold read ~9–10 s. Closing the batch gap
further needs incremental projection patching (rebuilding only the touched
region between sequential edits) — the next frontier for BOTH engines.

### 8.3a Control document — no small-document regression

Optimizing for a 9.3 MB document is only safe if ordinary documents do not
regress, so both trees were measured in-process (BIGDOC = 0.4 MB control,
`git worktree` at the parent commit vs HEAD, min-of-N):

| | base (dcf03c1) | after §8.2 (c4ef370) | change |
|---|---|---|---|
| BIGDOC projection | 0.388 s | 0.284 s | **−27 %** |
| BIGDOC mapper build | 0.523 s | 0.422 s | −19 % |
| VVBIG projection | 8.914 s | 7.342 s | −18 % |
| VVBIG mapper build | 13.172 s | 11.364 s | −14 % |

The control gained MORE proportionally than the stress document — both fixes
are per-paragraph/per-run, so the benefit scales with content density rather
than file size. Over the wire on BIGDOC, Python vs Node: cold read 622 vs
361 ms (1.72×), warm page turn 15 vs 13 ms, first `clean_view` 568 vs 245 ms,
single edit 1046 vs 519 ms (2.02×) — i.e. the cross-engine ratio is much
gentler on ordinary documents than the 2.7×/6× seen on the stress document.

### 8.3b Measured and REJECTED: header/footer reference lookups

cProfile attributed ~0.45 s of the projection to python-docx's
`get_headerReference`/`get_footerReference` (~25,000 calls, each building and
compiling a fresh XPath string). That looked like an easy win of the same kind
§7.1 landed for `doc.settings`. Direct measurement says otherwise:

| | VVBIG (1,883 sections, 3,544 parts) |
|---|---|
| `iter_document_parts_with_kind` total | 0.281 s |
| all `is_linked_to_previous` checks (current path) | **0.067 s** |
| equivalent direct lxml `sectPr` child scan | 0.008 s (same 3,543 results) |

Maximum available saving is ~0.06 s — **0.8 %** of the projection, not the
0.45 s cProfile implied (inflated ~7× by per-call overhead, exactly the
distortion §2 warns about). Also note 3,544 parts come from 1,883 sections, so
most sections genuinely HAVE definitions and are yielded anyway; skipping
container construction for linked-to-previous sections would help almost
nothing. **Not worth changing twin-shared part-iteration code for 0.8 %** —
recorded here so it is not re-derived from a profile again.

### 8.3c Run-loop fusion, step 2: text/flags carried by the event stream

`iter_paragraph_content` already walks every run's children (to find drawings,
fields and references). Both twins then walked each run AGAIN for its text and
style markers. That second walk is now gone: `process_run_element` accumulates
the projected text and the bold/italic flags in its existing loop and yields a
`ProjectedRun`.

Design points worth keeping:

- **Flags, not marker strings.** The stream cannot know `is_heading` (a
  property of the enclosing paragraph), so it carries booleans and callers
  apply `markers_from_flags(...)`. Threading `is_heading` into the stream would
  have created a second code path through twin-shared code.
- **`ProjectedRun` subclasses `Run`**, so every `isinstance(item, Run)` check
  and every consumer that treats the item as a python-docx run keeps working;
  the mapper still stores it in `TextSpan.run`. Both twins reject a bare `Run`
  loudly rather than silently re-walking, so a bypassed stream cannot quietly
  break the twin contract.
- The branches are **duplicated** between `run_text_and_flags` (standalone) and
  `process_run_element` (inlined — it must `yield` from the same loop, so it
  cannot delegate without re-walking). `tests/test_run_fusion_equivalence.py`
  cross-checks every value the stream produces against the standalone walk for
  26 run shapes × both `is_heading` values. Change both, or neither.

Measured (VVBIG unless noted):

| | before | after |
|---|---|---|
| projection | 7.342 s | **6.531 s** (−11 %) |
| mapper build | 11.364 s | 10.431 s (−8 %) |
| BIGDOC projection (control) | 0.284 s | 0.269 s (−5 %) |
| cold read end-to-end, A/B vs parent commit | — | **0.955× (−4.5 %)** |

The end-to-end A/B is run alternating base/current in one session, because
absolute timings on this machine move with available RAM. Do not compare
absolute numbers across sections measured on different days.

**A ratio measured under memory pressure is NOT the ratio users see.** The
first A/B of this change was run with ~3 GB free and reported 0.803×
(55.6 s → 44.6 s). Re-run with ~5 GB free it is **0.955× (12.16 s → 11.61 s)**,
which is the representative figure and the one tabulated above. Under paging,
a change that allocates less wins far more than it does normally — real, but
not the number to quote. Peak RSS is identical before and after (1429 MB), so
the fusion neither costs nor saves memory; only the ~4× inflated absolutes
differed. Check free RAM before trusting any absolute timing here.

**Two estimates in this document were wrong; both corrected here.**

1. §8.3 predicted ~2–3 s from this step. Actual: ~0.8 s on the projection. The
   error came from a "fused prototype lower bound" probe that omitted the event
   branches, the `ProjectedRun` construction and the event yielding — it
   measured *less work*, not *the same work fused*. The honest accounting: the
   stream went 3.173 → 3.959 s (+0.786 s of in-loop work) while removing
   1.948 s of second-walk cost, i.e. ~1.16 s net in that area.
2. **Generators are NOT a cost centre.** Three-layer `yield from` nesting costs
   **0.060 s for 571,564 items**. "Eliminate the nested generators" was on the
   roadmap on the strength of a cProfile reading; it is now off it. The
   remaining ~3.96 s of stream time is per-child branch dispatch plus
   per-run object construction, not generator machinery.

Method note, twice learned the hard way: size a Python win with a plain loop
over real data, and make any prototype do ALL the work the real code does.
cProfile inflated the header/footer estimate ~7× (§8.3b) and a too-simple
prototype inflated this one ~3×.

### 8.3d Current cross-engine numbers (2026-07-27, end of day)

Both engines through their real MCP servers over stdio, same client, same
timing code, measured back-to-back with ~5 GB free (medians; VVBIG):

| metric | node | python | py/node |
|---|---|---|---|
| read COLD (page 1) | 4.95 s | 11.47 s | 2.32× |
| read WARM page turn | 16 ms | 27 ms | 1.7× |
| read WARM outline | 3.4 ms | 14 ms | 4.1× |
| read WARM search | 26 ms | 131 ms | 5.0× |
| first `clean_view` | 2.13 s | 11.88 s | 5.6× |
| single-edit batch | 10.50 s | 24.35 s | 2.32× |
| 10-edit batch (all applied) | ~22 s | ~112 s | ~5.1× |

Peak RSS, measured for the first time (sampled during the call):

| | node | python |
|---|---|---|
| cold read | 2293 MB | **1429 MB** |
| single-edit batch | 3687 MB | not measured |
| 10-edit batch | **4841 MB** | not measured |

Two things fall out of that:

- **Node uses ~1.6× the memory of Python for a cold read** (2.29 GB vs
  1.43 GB) — the opposite of the intuition that the Python engine is the
  memory-hungry one. §7.2's 4.5 GB Python figure is the *dry-run* flow (a
  mode since removed, which built a second engine), not a read.
- Node's 10-edit batch peaks at **4.84 GB**, right at the default ~4 GB V8
  old-space limit. That is the mechanism behind §8.3e's OOM, and it means Node
  is close to the ceiling on large batches even when they succeed.

Python's in-harness RSS probe returned a nonsense 4 MB: this venv's
`python.exe` is a launcher stub, so the real interpreter is a CHILD process.
Sample the whole process tree (the 1429 MB figure above does).

Progress this day, Python end-to-end: cold read 12.64 → 11.47 s (−9 %),
single edit 27.15 → 24.35 s (−10 %), 10-edit batch ~123 → ~112 s (−9 %). The
cold-read gap to Node narrowed from 2.66× to 2.32×. No Node code was changed.

Measurement hygiene, learned twice today: this machine's absolute timings
degrade badly under memory pressure and Node degrades MORE erratically than
Python (Node cold read ranged 6.8–10.1 s and a single edit 11.7–34.5 s with
~3 GB free, against 4.91–4.97 s and 10.1–10.6 s with ~5 GB free). Check free
RAM first; prefer alternating A/B ratios; discard runs whose spread exceeds a
few percent.

### 8.3e Node OOM on a REJECTED multi-edit batch (open bug)

Reproducible 4/4: `process_document_batch` with 10 `modify` changes on VVBIG
where at least ONE `target_text` is ambiguous — so the whole batch must be
rejected — kills the Node server at ~18.7 s with
`Mark-Compact … 4072.6 (4115.4) MB … allocation failure`. The stdio transport
closes and the client sees the process vanish instead of an error response.

Key asymmetry: the SUCCESSFUL 10-edit apply path completes in ~22 s within the
default heap, and `NODE_OPTIONS=--max-old-space-size=8192` rejects cleanly in
13.9 s. So the excess is specific to the validation-failure / diagnostic path,
not to applying edits. §8.3d's measurement shows why it is so close to the
edge: the successful batch already peaks at **4.84 GB** against a ~4 GB default
old-space, so the ambiguity diagnostics (which embed surrounding-text excerpts
per occurrence, per edit, over a multi-MB projection) only need to add a little
to tip it over.

Two separate defects: the memory blow-up itself, and that a failed batch takes
the server process down instead of returning an MCP error. Profile the reject
path (`--heap-prof`) rather than guessing at the retention.

Related cross-engine divergence, also unfixed: Node signals batch rejection via
MCP `isError`, while the Python server returns it as a NORMAL result whose text
begins "Batch rejected." Clients cannot use one check for both.

### 8.4 Golden harness (kept this time)

`python/scripts/golden_projection.py` — `verify [manifest]` / `capture <dir>` /
`compare <base> <new>`. Captures 7 views × 3 documents (cells synthetic
fixture, BIGDOC, VVBIG): `reader_raw`, `reader_clean`, `reader_appendix`,
`mapper_raw`, `mapper_clean`, `outline`, `pagination`. It asserts the §7.3.3
twin contract on every computation, mirrors the production outline path exactly
(`return_paragraph_offsets=True`, as `doc_cache._fill_view` does), and checks
that requesting offsets does not change the projected text. All 21 views
stayed byte-identical across both §8.2 changes.

**The baseline is now COMMITTED and automatically enforced** — previously the
evidence lived only in a commit message, so "byte-identical" could not be
re-checked later:

- `tests/golden_manifest.txt` — sha256 + length per view. Hashes only, so no
  multi-MB golden text enters git. Regenerating it is a deliberate, reviewable
  act (`capture` then copy the MANIFEST) and must be called out in the commit.
- `tests/test_projection_goldens.py` — runs the gate. The `cells` fixture is
  built in-process so it gates EVERY machine including a fresh clone; BIGDOC
  runs when present (~2.5 s); VVBIG needs `ADEU_GOLDEN_SLOW=1` (~60 s, kept
  out of the default ~30 s suite). The gate is verified to actually fail: a
  doctored hash produces a mismatch and exit 1.
- `tests/test_run_fusion_equivalence.py` pins the fused function against
  VERBATIM copies of both pre-fusion originals over 26 run shapes × both
  `is_heading` values (§3.6's "pin against the old algorithm").

Use `verify` for the pass/fail gate; use `capture` + `compare` when you need
to SEE a diff, since those keep the full text side by side.

---

*The 2026-07-24 bench harnesses (stage, pipeline, edit benchmarks; synthetic
fixture generator) were session-local scripts in `node/bench/` and
`python/bench/` and were removed after that work landed — every headline
number they produced is recorded in this document. The methodology (§2) is
what to reproduce, in either engine, before the next round of performance
work. The golden harness described in §8.4 IS committed — use it.*
