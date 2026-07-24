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
4. **Parse floor.** The remaining floor is XML parsing throughput. Options,
   in increasing invasiveness: skip decompressing non-XML entries on
   read-only paths; parse secondary parts lazily on first access; replace
   the DOM construction for the main part with a streaming (SAX-style)
   parser feeding a minimal purpose-built tree. The last option is the only
   one that materially moves the floor, and it carries the most risk — gate
   it on the same golden-equivalence protocol before adopting.

## 6. Porting checklist for the Python engine

- [ ] Reproduce the stage benchmark and structural census scripts (the
      TypeScript versions live in `node/bench/`; they are ~200 lines each and
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

*Benchmark artifacts: `node/bench/` (stage, pipeline, edit benchmarks; golden
capture/compare harness; synthetic fixture generator). Raw result JSON files
under `node/bench/results/`.*
