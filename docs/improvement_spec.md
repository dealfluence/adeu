# Adeu — agent-efficiency improvement spec (Python CLI + MCP server, with Node parity)

**Audience.** A future working session on the `adeu` Python package and/or the Node
implementation. This document is self-contained: every claim carries its evidence, every
item carries acceptance criteria, and Python anchors cite **adeu 1.31.0** file:line (the
version audited). Node internals were not audited — Node items are stated as behavioral
parity requirements plus an audit checklist (§8).

**Origin.** Findings from running adeu inside Harvey's legal-AI benchmark (Gemini 3.6
Flash agent, 36–251 instrumented runs, per-turn token accounting), where a harness-side
wrapper had to patch adeu's agent surface to win on tokens/cost/speed. Those patches
(and the failure modes that forced them) are the product backlog: anything a wrapper had
to fix, every other integration — MCP clients, the Node port, other harnesses — still
suffers.

**Updated 2026-08-07 after a second benchmark iteration (v3 → v4).** The first round of
wrapper fixes shrank every response and *still lost*, because it multiplied round trips
— which produced three new package items (A6 native page ranges, B9 structured failure
blame, the B5 salvage contract), one new design principle (P7), and corrections to A3
and B7 below. Everything marked *[v4-validated]* was implemented wrapper-side and
verified live against 1.31.0 before being specified here.

---

## 1. The economics these improvements target

In an agent loop, the entire conversation is re-sent to the model every turn. Therefore:

- **A tool response of size S issued at turn t costs S × (T − t) tokens over a T-turn
  session** — a 50k-token read at turn 10 of 40 bills ~1.5M cumulative input tokens.
- **Tool-call arguments are model *output* tokens** (typically 5× the input price), so
  anything that makes the model retype content — echoed batches, whole-batch retries,
  long `target_text` — is doubly expensive.
- **Turns are the second currency**: each avoidable round-trip re-bills the whole prompt.
- Wall clock in practice is model API time (prefill ∝ prompt size, decode ∝ output), not
  tool execution: the CLI measured **~0.4s per invocation including full ingest** of a
  16-page document. Token cuts are also latency cuts.

Measured on a real 16-page lease carrying 175 tracked changes (all via adeu 1.31.0):

| Call | Size | Note |
|---|---|---|
| `extract --page all` | ~51.7k tokens | the thing agents reach for first |
| `extract` (default page 1) | ~4.8k | |
| `extract --mode outline` | ~0.6k | includes page count |
| `extract --mode appendix` | ~1.0k | |
| `extract --search-query "Chg:"` | ~2.6k | **first 20 match groups only** (hard cap) |
| `diff original.docx markup.docx` | ~7.8k | complete change enumeration, 0.7s |
| apply report, per applied edit | ~150 tokens | echoes + dual previews (see B1) |
| apply batch-failure payload | ~0.13k | dry-run failure payload: 10–40× larger (B2) |

Benchmark facts referenced below (36-task adeu arm, v2 prompt, all numbers from
per-turn transcript accounting): tool-result injections — extract 2.04M tokens
(page-all 1.58M = 78% of it), dry-run/validate 0.89M, apply reports 0.50M; 307 edit
rounds, 26% failed; fresh rounds fail ~20% **independent of batch size**, retry rounds
fail **44%**; worst run: 204 edit submissions for 35 landed edits; 30 batch elements
arrived as fused JSON (`modify}],{comment:` shapes) from long-array generation.

**Round-2 evidence (v3 sweep, same 36 tasks).** A wrapper that compacted every response
(reports −55%/call, whole-doc dumps guarded, dry-run tool removed) cut the average
prompt per turn by 25% (107k → 80k) — and still lost, because **turns rose 42%**
(35.4 → 50.3/run): page-per-page reading replaced whole-doc reads (`page=N` calls
163 → 603, consecutive same-file chains up to 28 pages), apply rounds went 142 → 377,
and 45 of 116 failures became blind retry loops when the compactor dropped the CLI's
`message` field. At the old turn count the same per-turn savings would have beaten the
comparison arm by ~12%. Conclusion with hard numbers: **for agent consumers, the number
of calls a workflow requires dominates the size of each response.** Also observed: the
agent issued zero parallel tool calls in 3,169 turns — one call per turn is the
realistic planning assumption.

---

## 2. Design principles (apply to every surface: CLI, MCP, Node)

1. **Response size is a feature.** Default responses are the smallest complete answer;
   size escalations are explicit (flags), never the default.
2. **Never echo the caller's input back.** The agent has what it just sent; an echo is
   pure duplicated context, re-billed every later turn.
3. **Errors carry the recovery protocol.** Mid-loop guidance is the only guidance agents
   reliably follow (manuals demonstrably get ignored mid-task). An error message should
   say exactly what the next one or two calls should be.
4. **Make the cheap path the default path.** A documented-but-optional cheap path loses
   to an available expensive default every time.
5. **One semantics everywhere.** CLI, MCP, and Node must resolve targets, validate
   batches, and report results identically; divergence (see B5, B3) becomes agent
   confusion and benchmark asymmetry.
6. **Efficiency never at integrity's cost.** Transactional batches, no silent type
   inference into edits, no silent partial application — a legal redline that quietly
   landed 14 of 15 edits is worse than a clean failure.
7. **Minimize the calls a workflow needs, not just the bytes per call.** Agent
   frameworks re-send the whole conversation on every call, and models issue one tool
   call per turn in practice (zero parallel calls in 3,169 measured turns), so an
   operation that returns bounded bulk in ONE call (a page range, a change ledger, a
   salvage-and-report apply) beats N small perfect responses. The v3 sweep is the
   proof: −25% per-call size, +42% calls, net loss.

---

## 3. Group A — reading & review surfaces

### A1. `--mode changes`: a tracked-change/comment ledger (highest-value item)

- **Problem.** There is no compact way to enumerate a document's tracked changes and
  comments. Review-style agents therefore read whole documents (51.7k tokens) or search
  `"Chg:"` (capped at 20 match groups, each dragging its full paragraph). `adeu diff`
  enumerates completely (~7.8k) but only when an earlier draft exists, and it carries
  no `Chg:` ids — so id-based accept/reject still forces raw page reads.
- **Evidence.** 78% of all extract tokens in the benchmark were `page="all"` dumps; 95%
  of those were on source documents, 85% first-contact. The pieces already exist
  internally and are never surfaced: `pagination.PageInfo.tracked_change_count` is
  computed with no consumer (pagination.py:52); `_existing_change_ids()` /
  `_existing_comment_ids()` feed only error messages (redline/engine.py:4670-4687);
  `compute_change_pair_map` knows ins/del pairing; comments come from
  `comments_manager.extract_comments_data()`.
- **Spec.** New extract mode. Header: total changes, total comments, per-page
  distribution line, author roster. Then one line per change:
  `Chg:12  ins  Jane Doe  p3  "…snippet ≤80 chars…"  (pairs Chg:13)`
  and per comment: `Com:5  Bob  p2  "text ≤120"  (reply to Com:4)`.
  Options: `--changes-author <name>` filter, `--page N` filter. Paginate above ~300
  entries. A 40-change document must render in ≤ ~700 tokens.
- **Python anchors.** New builder beside `build_outline_response`
  (mcp_components/_response_builders.py); CLI wiring in `handle_extract`
  (cli.py:683-835); MCP `read_docx` gains the mode.
- **Node.** Same output format byte-for-byte (see §8 conformance).
- **Acceptance.** Golden test on a fixture with ≥3 authors, paired ins/del, reply
  chains, table-cell changes; token-budget test (≤18 tokens/change average); id list
  exactly matches `_existing_change_ids`.

### A2. Search: lift the hard cap, clamp the snippets

- **Problem.** `max_matches = 20` is hardcoded (_response_builders.py:596) with no flag
  and **no offset/continuation**, so dense results are simply unreachable; meanwhile
  each match renders its **entire paragraph** (uncapped — a long clause × 20 ≈ 5–7k
  tokens).
- **Spec.** (a) `--max-matches N` (default 20) and `--match-offset K` (or a
  `next: --match-offset 20` hint line in the truncation note); (b) snippet clamp to
  ±~120 chars around the hit, reusing the existing balance/tidy machinery
  (`_balance_snippet_window`, _response_builders.py:149-166) so CriticMarkup bubbles
  never split; `--full-paragraph` restores today's behavior.
- **Acceptance.** Worst-case default search on the lease fixture drops from ~5–7k to
  ≤1.5k tokens; every match remains reachable via offset; bubbles never truncate
  mid-`{>>…<<}`.

### A3. Response-budget guard on whole-document reads

- **Problem.** `page="all"` (and MCP `read_docx` of a large doc) returns tens of
  thousands of tokens in one call. In the benchmark this was the single largest cost
  driver until the harness wrapper added a guard: dumps over ~76k chars (≈4 synthetic
  pages) are refused and answered with the outline plus targeted-read guidance. That
  guard lives in the wrong layer — MCP clients and the Node port don't get it.
- **Spec.** Native guard in both CLI and MCP: a whole-document body request over a
  threshold (default ~76k chars; `--max-chars` / env `ADEU_MAX_RESPONSE_CHARS` to tune,
  `--force` to override) returns: page count, estimated tokens, the outline, and the
  recipe line ("tracked changes → `--mode changes` or `diff`; content → **page
  ranges**, `--page 1-8` then `9-16`"). The refusal must itself cost ≤ ~800 tokens.
- **⚠ v3-validated constraint: this guard MUST NOT ship without A6 (page ranges).**
  A guard that only offers single pages converts legitimate whole-document needs into
  one-page-per-call walking — measured in v3 as +440 extra calls and the single largest
  cost regression of the sweep. Refusing bulk without offering a *bounded* bulk path is
  worse than not guarding.
- **Benchmark result for calibration.** The wrapper version of this turned a 207k-char
  dump into a 2,649-char refusal-with-outline; the agent recovered — but recovered into
  page-walking until ranges existed (v4).
- **Acceptance.** Guard fires only on plain full-mode whole-doc requests (search with
  `page=all` and outline/appendix modes are exempt); `--force` honored; golden test on
  refusal shape.

### A4. `--no-chrome` (or `--terse`) extract flag

- **Problem.** Every response carries a `> **File Path:** …` line; multi-page reads add
  banners, footers, and an appendix pointer — ~85–160 tokens per page, ~5–10k per
  session of pure chrome (_response_builders.py:236,286,376; pagination.py:379-439).
- **Spec.** Flag suppresses the File-Path header, page banner/footer prose (keep a bare
  `[p3/16]` marker), and appendix pointer. Consider making terse the default under
  `--json` (machine consumers don't need navigation prose).
- **Acceptance.** Page content byte-identical apart from chrome; token diff test.

### A5. Compact `diff --json`

- **Problem.** `diff --json` pretty-prints with `indent=2` (cli.py:972) and emits
  default-valued fields (`match_mode:"strict"`, `regex:false`, boilerplate
  `comment:"Diff: …"`), inflating a ready-to-apply batch by 25–40%.
- **Spec.** No indentation; omit fields at default values; `comment` only when
  meaningful. Keep the non-JSON human renderer as is.
- **Acceptance.** Round-trips through `apply` unchanged; size regression test.

### A6. Native page ranges — `--page 2-6` *[v4-validated]*

- **Problem.** `--page` accepts a single page or `all`, so any multi-page need is one
  CLI/MCP call per page. Agents pay one full conversation re-send per call and never
  parallelize, so a 16-page agreement costs 16 calls. Measured in v3: `page=N` calls
  163 → 603, consecutive same-file chains of 20–28 pages, the top cost driver of the
  sweep.
- **Spec.** `--page N-M` (and MCP `page: "N-M"`) returns pages N..M concatenated in one
  response, capped at ~8 pages per call; past the cap, append a continue-with note
  (`continue with --page 9-16`); past the document's true end, stop and note the real
  page count. Keep per-page banners so provenance stays visible. Works for `full` and
  `appendix` modes; with a search query, `page` remains a single-page filter.
- **v4 wrapper precedent (the behavioral spec to match):** pages 2–4 of a 16-page lease
  returned in one call (~10.9k tokens); a `1-12` request returns 8 pages plus
  `continue with page="9-12"`; a range past the end returns what exists plus
  `[range stopped at page N: <adeu's own out-of-range message>]`.
- **Python anchors.** Pagination already exposes per-page offsets
  (pagination.py:303-361); the CLI page parsing is cli.py:739-765. Native support is a
  renderer loop, not new projection work.
- **Node.** Same syntax, same cap, same note wording (conformance §8).
- **Acceptance.** Golden tests: mid-range, cap, early-stop, range+search rejected as a
  filter; token parity with the same pages read singly (minus per-call chrome).

---

## 4. Group B — apply, reports, and errors

### B1. `--report minimal|standard` for apply/dry-run

- **Problem.** Every per-edit report entry (~150 tokens) echoes `target_text` (≤500
  chars) and `new_text` (≤500) back at the caller, plus **two near-duplicate previews**
  (`clean_text` is derived from `critic_markup` by regex — engine.py:661-665). The
  caller wrote those texts one turn earlier. In the benchmark the wrapper stripped
  echoes and kept `{status, type, pages, heading_path, occurrences_modified, warning,
  error, critic_markup≤200}` — a 1-edit report went ~900 → 448 chars with zero loss of
  verification power (placement + rendering confirmed without re-reading the document).
- **Spec.** `--report minimal`: per applied edit `{status, type, pages, heading_path,
  occurrences_modified}` (+`match_mode` when non-strict, +`warning`, + one
  `critic_markup` preview ≤200 chars); failed edits keep full `error` + an ≤80-char
  target stub. Batch level: keep counters + `output_path`; drop `engine`, `version`;
  dedupe `skipped_details` against per-edit `error` (today both emit the same strings —
  engine.py:2947-2948). `--report standard` = today's shape. **MCP default: minimal**
  (its consumer is always a model); CLI default can stay standard for humans.
- **Acceptance.** Golden tests both shapes; token-budget test (≤40 tokens per applied
  edit in minimal); the "do not re-read to verify" workflow provable from a minimal
  report alone.

### B2. Failure payloads teach the split-recovery protocol

- **Problem.** Retry rounds fail at 44% vs 20% fresh, dominated by two behaviors the
  error text never corrects: whole-batch resubmission (nothing was applied, so agents
  resend everything — one 18-edit batch was submitted 4×, an identical 17-edit batch
  3×), and targets sourced from non-adeu views (python-docx `p.text` yields `$______`
  where the projection is `$**___**___`; ten turns were burned guessing).
  Additionally, a **dry-run failure payload is far larger than an apply failure**:
  every innocent edit is echoed with "Not applied: the batch is transactional…"
  boilerplate (engine.py:2680-2706) — 2–5k tokens vs ~130 for the apply abort.
- **Spec.** (a) Append to every batch-failure (apply and dry-run, CLI and MCP):
  *"Nothing was written. Recover in two calls: (1) re-apply this batch WITHOUT the
  failing edit(s); (2) fix the failing edit(s) in a separate small batch. Copy
  target_text verbatim from an adeu extract of the CURRENT file."*
  (b) Collapse dry-run innocent-edit entries to one line: `"N other edits were valid
  and were rolled back (transactional)."`
  (c) Keep the existing sequential-state hint (targets must match post-earlier-edits
  text) — it is correct and useful.
- **Acceptance.** Failure payload for a 20-edit batch with 1 bad edit ≤ ~500 tokens on
  both apply and dry-run paths; message text identical across CLI/MCP/Node.

### B3. Unify the comment-only `modify` shape (verified divergence)

- **Problem — verified against 1.31.0.** The strict CLI model requires `new_text`
  (`ModifyText.new_text: str`, models.py:78): a modify with only `target_text` +
  `comment` is rejected — *"Change #1 (type 'modify') is missing required field
  'new_text'"* — while the MCP coercion layer auto-fills `new_text := target_text`
  for comment-only edits (models.py:400-454; the flat model's `new_text` is Optional,
  models.py:303). Same batch, different outcome by transport. Agents taught the
  MCP-legal shape fail on the CLI; this contributed to the benchmark's
  "missing field" schema failures (92 comment-only submissions were observed).
  **v3 damage measurement:** 25 such edits poisoned 11 batches; all 11 failed — and
  every one surfaced as `invalid_changes_file`, the failure class that was also blind
  under the report-compaction bug (B9), so the agent could not learn the fix.
- **Spec.** Pick one and enforce everywhere; recommended: **make comment-only legal in
  the strict path too** (when `comment` is present and `new_text` is absent, treat as
  annotation-only; keep the hard error when *both* are absent). Update the change-type
  reference epilog (cli.py:551-561) to state it.
- **Acceptance.** The same comment-only batch produces an identical anchored comment
  via CLI file, MCP inline, and Node; schema-reference text updated; regression test
  that `new_text` truly absent + no `comment` still errors.

### B4. Honor `comment` on accept/reject (verified silent drop)

- **Problem — verified against 1.31.0.** `AcceptChange.comment` / `RejectChange.comment`
  are advertised in the schema as "Optional rationale" (models.py:169,179) but the
  review-action path never reads them (engine.py:4837-4948 references comments only for
  side-effect deletion). A lawyer's (or agent's) rejection rationale is silently
  discarded. This also has a quality consequence observed in the benchmark: a `reject`
  leaves **no visible record at all** in the deliverable, and a judged criterion was
  lost exactly because a change was reverted invisibly where a tracked deletion would
  have passed.
- **Spec.** When `comment` is present on accept/reject: anchor a margin comment (by the
  acting author) on the text where the change was resolved. If that anchor is
  impossible (e.g. rejected insertion leaves no text), fall back to the nearest
  surviving run boundary. Report the comment id in the edit's report entry.
- **Acceptance.** Fixture test: reject-with-comment produces a comment visible in the
  output OOXML and in a subsequent extract; schema description updated from "Optional
  rationale" to state where it lands.

### B5. One batch semantics across CLI and MCP — the "explicit salvage" contract *[v4-validated]*

- **Problem.** Today the CLI is strict + all-or-nothing at every layer (schema errors
  reject the file; any skip ⇒ no output written, exit 1 — cli.py:1155-1158), while MCP
  **salvages per-element silently-ish** (invalid elements dropped into
  `rejected_notes`, the valid remainder applied — document.py:121-182, 501-508) and
  **saves output even when `edits_skipped > 0`** (document.py:510-536). Same product,
  two contracts. Both extremes measured badly: all-or-nothing forces whole-batch
  resubmission wars (v2: 204 submissions for 35 landed edits; v3: apply rounds
  142 → 377), while quiet per-element salvage loses edits the model *intended* with no
  prominent signal.
- **Spec — the middle contract, validated in the v4 wrapper:** apply everything that
  validates, and make the failure impossible to miss. The response leads with
  `PARTIAL: applied K of N; M edit(s) FAILED and are NOT in the document`, lists each
  failure with its full diagnostic and an ≤80-char target stub, then instructs:
  *resubmit ONLY the corrected failing edits.* Engine-native this is one pass (no
  second invocation — the engine already isolates per-edit outcomes; only the
  save-on-skip rule changes). Keep strict all-or-nothing available as `--atomic` for
  callers that need the old contract. Live-verified wrapper behavior to match: a
  2-edit batch with one bad target landed the good edit and reported the bad one's
  reason in the same call; same for a schema-invalid element.
- **Acceptance.** Conformance fixtures produce identical outcomes on CLI/MCP/Node in
  both modes; PARTIAL responses always lead with what did NOT land; sequential batches
  where a later edit depended on a failed earlier edit report both failures coherently;
  version-gate the MCP/CLI default change (2.0) with a deprecation note.

### B6. Stop ASCII-escaping report JSON (Python-specific)

- **Problem.** Python `json.dumps` defaults to `ensure_ascii=True`; legal text is full
  of smart quotes/em-dashes, each becoming a 6-char `\uXXXX` escape in every report and
  preview. (Node `JSON.stringify` does not escape non-ASCII — here the parity fix is on
  the Python side.)
- **Spec.** `ensure_ascii=False` for all `--json` outputs and MCP payloads.
- **Acceptance.** Report containing `’ “ ” —` renders those characters literally.

### B7. Garbled-batch hint

- **Problem.** Long inline arrays are where models garble JSON: 30 elements arrived as
  fused strings like `"type": "modify}],{comment: …"`. The schema error names the bad
  type but not the cause or fix.
- **Spec.** When a type string contains `}`/`{`/`":`, append: *"This looks like two
  edits fused during generation — resubmit this edit alone, correctly formed."* (Do
  NOT advise smaller rounds: fresh-round failure is size-independent — v2 measured 18%
  for >12-edit batches vs 21% for ≤12 — and smaller rounds multiply calls, the costlier
  currency per P7. With B5 salvage, a fused element costs only itself.)
- **Acceptance.** Unit test on the fused shapes observed.

### B8. Error-size budget knobs (low priority)

- Ambiguity errors: 5 examples × ±50 chars + strategy block ≈ 275–475 tokens
  (markup.py:355-358, 361-439); stale-id errors list 20 ids (engine.py:4689-4695).
  Optional `--terse-errors`: 2 examples × ±25, 8 ids. Errors were not a dominant cost —
  do last.

### B9. Uniform failure envelope with machine-readable blame *[new after v3]*

- **Problem.** Failure payloads have two different shapes — engine failures return
  `{"error": "batch_validation_failed", "errors": ["- Edit 5 Failed: …", …]}` while
  schema failures return `{"error": "invalid_changes_file", "message": "…Change #1
  (type 'modify') is missing…"}` — and in both, **which edits failed is only encoded in
  prose**. Two real costs measured in v3: (a) a consumer that post-processes reports
  must special-case both shapes, and missing one deleted the entire diagnosis (the
  benchmark wrapper dropped `message`, making 45 of 116 failures blind retry loops —
  worst run 40 apply calls, 22 failures, 12.2M tokens); (b) the v4 salvage feature had
  to **regex `"Edit N Failed"` / `"Change #N"` out of prose** to learn which edits to
  re-apply — fragile by construction.
- **Spec.** One failure envelope for both layers, CLI and MCP:
  `{"error": <code>, "failed": [{"index": <0-based>, "reason": "…"}, …], "message": <one-line summary>}`
  — prose stays for humans, `failed[].index` is the machine contract. Emit it from both
  the schema validator and the engine batch path. Consumers (salvage, wrappers, UIs)
  key off `failed[].index`, never off prose.
- **Acceptance.** Both failure layers produce the envelope; indices verified against
  fixtures (including multi-failure batches and fused-JSON elements); prose wording may
  vary between Python and Node but the envelope fields may not.

---

## 5. Group C — workflow & guard ergonomics

### C1. Multi-author guard: teach the lawful recoveries in the message

- **Problem.** The guard message — *"targets an active insertion from another author …
  Accept that change first or scope your edit outside of it"* — is correct but
  incomplete, and one benchmark agent generalized it catastrophically: after two guard
  hits it **bulk-rejected all 124** counterparty changes (including 18 its instructions
  said to accept), then had to rebuild the document edit by edit (85 turns, $29.41,
  and it still failed a criterion because the bulk revert left no visible record).
  Undocumented semantics the message never states: an edit **wholly inside** a foreign
  insertion is allowed (strict/first only — engine.py:2236-2308); `(pairs with Chg:M)`
  means one action resolves both sides.
- **Spec.** Extend the message: *"You may edit text wholly inside that insertion
  (match_mode strict/first). To span its boundary: accept Chg:N first, then modify. To
  counter it visibly: accept it, then apply your own tracked edit. Do not bulk-reject a
  counterparty's markup."* Keep it ≤ ~70 tokens.
- **Acceptance.** Message golden test; docs (`adeu help`, MCP tool description) state
  the wholly-inside allowance.

### C2. Gate the author-name bypass

- **Problem.** Guard foreignness is a plain string comparison with the acting author
  (engine.py:2255): setting `--author` to the counterparty's name silently bypasses
  every protection and misattributes revisions. Nothing warns about this.
- **Spec.** When the acting author string equals an author with pending revisions in
  the document, emit a warning in the report (or require `--impersonate-author` to
  proceed). Never document the bypass as a workaround.
- **Acceptance.** Warning fires on the fixture; normal same-author workflows (editing
  your own earlier revisions) stay silent.

### C3. Expose text-based apply on MCP (parity with CLI)

- **Problem.** The CLI's strongest bulk primitive — `adeu apply doc.docx modified.txt`
  (whole-text diff→tracked-changes with post-apply verification, cli.py:1177-1228) and
  its generator twin `adeu diff doc.docx edited.txt --json` — has no MCP tool. For
  heavy rewrites it replaces dozens of modify objects (and all their argument tokens);
  the benchmark's baseline arm effectively hand-rolled this exact strategy to win the
  hardest task.
- **Spec.** MCP tool `apply_text_revision(file, modified_text, output, author)` with
  the same clean-view/whole-document input contract and the same verification gate;
  document the `--allow-major-deletions` interlock.
- **Acceptance.** Conformance with CLI text path on fixtures, including the
  verification-failure path (`.unverified.docx` sibling semantics).

### C4. `ADEU_AUTHOR` ergonomics

- Already supported (cli.py:537). Document it in `help`/README and MCP server docs as
  the way to set attribution once per environment instead of per call.

---

## 6. Group D — performance (do after token work; measured impact is small)

Benchmark measurement: CLI ≈ 0.4s/invocation *including* full ingest of a 16-page doc
(import cost dominates; the builders were deliberately kept fastmcp-free because
importing fastmcp costs ~0.7s — _response_builders.py:72-80). At 401 extract calls per
36-run sweep this totaled ~4.5s per run against a 409s mean — negligible there. It is
NOT negligible for interactive UX or 100+-page documents.

- **D1. On-disk projection cache for the CLI.** Persist `base_text`, page offsets, and
  serialized outline nodes keyed by the doc_cache stat triple (path, mtime_ns, size)
  under a cache dir; consult before `_load_docx_or_exit`. Determinism is already a
  tested invariant (doc_cache.py:19-22).
- **D2. `adeu serve`** — JSON-lines daemon over stdin reusing the MCP `doc_cache`
  singleton (LRU 3, stat-checked per call, background pre-warming — doc_cache.py:41,
  82-88, 129-166; document.py:269-308, 433-442, 549-558), eliminating per-call
  interpreter+import cost for high-volume harnesses.
- **D3.** Make the doc_cache LRU size env-tunable (3 entries is small for multi-document
  matters).

---

## 7. Group E — MCP-specific

- **E1. Drop the mandatory `reasoning` argument** (or make it optional). Every MCP tool
  requires a leading `reasoning` string that is deleted unused
  (document.py:641, 838, 900 — `del reasoning  # reason-first UX; not used`). That is a
  pure output-token tax on **every** call, paid at the most expensive rate. If
  reason-first UX matters for logs, make it optional and never require clients to send
  it.
- **E2. Response compaction per B1/B2** — the MCP batch response renders per-edit
  markdown blocks with **both** previews (document.py:562-613); apply the minimal-report
  shape there too.
- **E3. Missing-file suggestions** (up to 10 close-match sibling names, shared.py:19-51)
  are good — port the same helper to the CLI (which today prints only a sandbox hint,
  cli.py:193-204) and to Node.
- **E4. Id-discovery hints in errors** currently differ by transport (CLI names
  `adeu markup -i` / `adeu extract`; MCP names `read_docx` — shared.py:13-16). Once A1
  ships, both should name the changes ledger.

---

## 8. Node implementation — parity audit & conformance

The Node package was not audited in this work. Treat every item above as a behavioral
spec and run this audit first:

1. **Map the surface.** For each: extract modes (full/outline/appendix/search + page
   semantics + 19,000-char synthetic pagination), diff (docx-docx and docx-text), apply
   (file batch + text path + dry-run), accept-all, markup preview, sanitize — does the
   Node CLI/MCP expose it, and with which flags?
2. **Check the known Python pain points in Node** (they may or may not exist there):
   single-page-only `--page` (A6 — does Node already support ranges?); search match cap
   and per-snippet size; report echo fields and `clean_text` duplication; dry-run
   failure verbosity; the two failure-envelope shapes and prose-only blame (B9);
   comment-only modify shape (B3); accept/reject `comment` handling (B4); per-element
   salvage vs transactional and save-on-skip behavior (B5); `reasoning` argument (E1);
   response chrome (A4); unicode escaping (B6 — Node's `JSON.stringify` default is
   fine, verify nothing re-escapes).
3. **Conformance suite (the real deliverable).** Build a shared fixture set — generated
   synthetic DOCX, not benchmark corpus documents — covering: multi-author tracked
   changes with ins/del pairs, comments with replies, tables with cell revisions,
   smart-quote/unicode text, a >4-page document, a document with 175+ changes. For each
   fixture and each operation, store golden outputs (ledger, outline, search, reports,
   error payloads). **Python CLI, Python MCP, and Node must produce identical goldens**
   (modulo a declared allowlist of cosmetic differences). Add token-budget assertions:
   ledger ≤18 tok/change, minimal report ≤40 tok/edit, batch failure ≤500 tok, guard
   refusal ≤800 tok.
4. **Version lockstep.** Ship the behavior changes under the same minor version in both
   packages; record the surface version in every report (`version` field stays, even if
   `--report minimal` drops it from per-edit payloads — keep it batch-level).

---

## 9. Prioritized roadmap

| Priority | Items | Why first | Rough size (Py) |
|---|---|---|---|
| **P0** | **A6 page ranges**; A1 changes ledger; B9 failure envelope; B1 minimal reports; B2 failure protocol; B3 comment-only unification; B4 honor accept/reject comment | The v3→v4 iteration proved calls dominate bytes (P7): A6 and B9 are the two turn-killers, alongside the original cost drivers and correctness gaps | each S–M (renderer + wiring + tests) |
| **P1** | A2 search flags; A3 budget guard (**only with A6 shipped**); E1 reasoning arg; B6 unicode; B7 fused-JSON hint; C1 guard message | Cheap, high leverage, no semantic risk | each XS–S |
| **P2** | B5 explicit-salvage contract (+`--atomic`); A4 chrome flag; A5 diff JSON; C3 MCP text-apply; E3/E4 | Contract changes needing version care — B5's design is v4-validated, its default flip still needs a version gate | S–M |
| **P3** | D1/D2/D3 perf; B8 error budgets; C2 author gating | Real but not benchmark-binding | M |

**Benchmark rerun note.** The Harvey harness pins `adeu==1.31.0` in
`lab-sandbox-adeu:latest` and records `adeu_version` in each run's `config.json`. After
shipping P0/P1 as 1.32 (or 2.0 if B5 changes defaults): bump the pin, rebuild the
image, and re-run the adeu arm only — the baseline arm is adeu-free and stays valid.
With A6+A1+A3+B1+B5+B9 native, the harness wrapper's v4 layer (page-range loop,
report compaction, salvage-with-regex-blame, failure nudges) becomes redundant and
should be reduced to thin pass-throughs, so the benchmark measures the product, not
the wrapper.

---

## 10. Guardrails — what NOT to change

- **Keep batches transactional by default** and keep the CLI's strict schema stance (no
  silent type inference into legal edits): the product's own rationale is correct —
  *"silently treating a typeless object as 'modify' turns malformed batches into
  unintended edits."* Salvage only ever behind an explicit `--partial`.
- **Keep the post-apply verification gate on the text path** (clean-text equality, the
  `.unverified.docx` sibling, the never-overridable page-chrome guard).
- **No silent author impersonation** (C2 is a gate, not a feature).
- **Don't shrink failure detail for the *failing* edits** — compaction targets echoes,
  duplicates, and innocent-edit boilerplate, never the diagnostic itself.
- **Ids must stay OOXML-derived and stable** (`_get_next_id` allocating above the
  document max is load-bearing for the "read once, act many" agent workflow).

---

## Appendix — verification transcripts for the two correctness gaps

**B3 (comment-only modify, CLI vs MCP), adeu 1.31.0 in `lab-sandbox-adeu:latest`:**

```
$ adeu apply lease.docx '[{"type":"modify","target_text":"Annual Escalation:** 3.0% per annum","comment":"x"}]' --dry-run --json
❌ The changes file is not a valid edit batch:
  - Change #1 (type 'modify') is missing required field 'new_text'.

$ # same edit with "new_text" == "target_text":
applied: 1 skipped: 0 | first edit status: applied
```

**B4 (accept/reject rationale dropped):** `AcceptChange`/`RejectChange` declare
`comment: Optional[str] = Field(None, description="Optional rationale.")`
(models.py:169,179); `apply_review_actions` (engine.py:4837-4948) reads `comment` only
to snapshot comment-deletion side effects — the rationale string is never written to
the document.

**A6/B5/B9 (v4 wrapper behaviors, live-verified against 1.31.0 on a 16-page lease):**

```
# range read: pages 2-4 in ONE tool call (~10.9k tokens; was 3 calls)
adeu_extract(file, page="2-4")

# explicit salvage: 2-edit batch, one bad target
PARTIAL (auto-salvaged): applied 1 of 2 edit(s) -> /workspace/output/r.docx; 1 edit(s)
FAILED and are NOT in the document.
Failures: {"error": "batch_validation_failed", "errors": ["- Edit 1 Failed: Target
text not found in document:\n  \"THIS DOES NOT EXIST IN THE DOCUMENT\""]}

# schema failure with message retained (the v3 blind spot)
Failures: {"error": "invalid_changes_file", "message": "The changes file is not a
valid edit batch:\n  - Change #1 (type 'modify') is missing required field
'new_text'. …"}
```

The blame indices for salvage had to be regex-parsed from that prose ("Edit 1",
"Change #1") — the fragility B9's `failed[].index` field removes.