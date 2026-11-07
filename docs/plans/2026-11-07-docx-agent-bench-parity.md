# docx-agent-bench Parity: Verification of the 2026-11-06 Plan and the Next Improvement Plan

## Context

### Why this plan exists

`docs/plans/2026-11-06-engine-redline-fidelity-and-agent-workflow-plan.md` (implemented in one commit, `5e9aaa0`)
set out to bring Adeu level with or ahead of stetdoc on
[docx-agent-bench](https://github.com/clauseo-ai/docx-agent-bench). In that benchmark's published run `final-2`,
Adeu (v3.0.7, `@adeu/mcp-server@3.0.7` + the `adeu-redlining` skill) was the lowest-scoring arm. Every other arm
was at or near 100% on the short tasks (`reports/final-2/tables.md:7-13`):

| arm | claude-haiku-5-5 | claude-opus-5-5 | gemini-3.8-flash-medium |
|---|---|---|---|
| stetdoc | 40/40 | 40/40 | 34/35 |
| adeu (3.0.7) | 34/40 | 35/40 | 30/35 |

Long tasks, fully correct: stetdoc 6/6 on all three models; adeu 4/6, 4/6 and 2/6 (`tables.md:79-94`).

This document records (1) how the 2026-11-06 changes were checked against the benchmark and what that check found,
and (2) the plan for the gaps that remain.

### Where Adeu lost points in final-2 (from `reports/final-2/runs.csv` and the raw archive)

Every adeu run that was not fully correct falls into one of these classes:

| class | runs | failing check | root cause (verified, see below) |
|---|---|---|---|
| A. compare-v1-v2 | 15/15, all three models | `all-changes-tracked` (all 15), plus `nothing-else-changed` (3 haiku) | With every revision rejected, paragraphs do not match v1: merged paragraphs (fixed in 5e9aaa0) and **tracked paragraph splits** (not fixed) |
| B. long-indenture-markup | 6/6, all three models | Word `REPAIR_PROMPT` | The input already lists `w14 wp14` in `mc:Ignorable` without declaring them. Any writer that keeps that root unchanged ships a file Word repairs. Adeu 3.0.7 had no manual-markup mode, so agents wrote python-docx scripts that kept the bad root |
| C. long-underwriting-markup | 1/6 (gemini) | `all-changes-tracked` | Tracked paragraph split (same as A) |
| D. stale-colleague-edit | 1/5 (haiku) | `no-new-docx` | With `output_path` omitted on `*.redline.docx`, 3.0.7 wrote `*.redline_processed.docx` instead of updating in place |
| E. long-csa-counter-redline | 1/6 (gemini) | timeout at 1800 s, `cover-note` | Agent ran out of time. Not attributable to one engine defect |

Class B evidence, all 12 adeu indenture outputs (`stetdoc check --all`): the 6 Word rejected all carry
`mc_prefix_undeclared`; the 6 Word accepted carry none.

### How the verification was done

Running the benchmark live needs Claude Code `plugin eval`, macOS, Antigravity and Word for Mac. None of these are
available on the verification machine, and live runs cost money. Instead the benchmark was reproduced offline, as
faithfully as its grading allows:

1. **Same fixtures, byte for byte.** Harvey LAB documents at commit `465d1fcf`, built with the benchmark's
   `scripts/build-fixtures.py`. Every SHA-256 matched `suite/evals/fixtures.tsv` and `long-fixtures.tsv`.
2. **Same reader and graders.** A Python port of `suite/evals/lib/scaffold.sh`, `suite/evals/harness/report.sh` and
   `normalise.sh`, using **stetdoc 0.9.0** (the version final-2 used; `pip install stetdoc==0.9.0`) and
   **ooxml-validate v0.1.0** (win-x64 release). The case graders are evaluated from their own frontmatter.
   **Calibration:** `stetdoc compare` on compare-v1-v2 scores 8/8 in the harness, matching every stetdoc run in
   final-2.
3. **Same agent decisions.** Each adeu transcript in the final-2 raw archive
   (`docx-agent-bench-final-2-raw.tar.gz`, SHA-256 `561991ee…87bc`, verified) was turned into an MCP call list. That
   list was replayed over stdio against three builds:
   - **3.0.7** (`npm i @adeu/mcp-server@3.0.7 @adeu/core@3.0.7`, the benchmarked build);
   - **pre**: `b69acb7`, the parent of `5e9aaa0`;
   - **HEAD**: `5e9aaa0`.

   Runs that wrote through the CLI or ad-hoc scripts were not replayed.
4. **Replay fidelity.** The 3.0.7 replays reproduce final-2's per-grader verdicts on all 10 Claude compare-v1-v2
   runs. They also reproduce them on every other replayed run, except 8 runs whose transcripts also wrote outside
   the MCP server (3 Gemini comment-liability-cap runs and 5 stale-colleague-edit runs). Those 8 score the same on
   all builds, so they cannot hide a regression.

The harness lives in `.worktrees/bench-data/`, which is gitignored and stays out of the repo (Task 0). Its main
entry points:
- `bench_harness.py`: scaffold, report and grade;
- `run_case.py`: one case, one scripted agent;
- `mcp_replay.mjs`: stdio MCP replay;
- `extract_calls.py`: transcript → call list;
- `replay_all.py`: the whole replay matrix;
- `probe_*.py` / `probe_*.mjs`: the minimal reproductions cited below.

### Verification results

#### Test suites at HEAD (run 2026-11-07 on Windows)

| suite | command | result |
|---|---|---|
| Python | `cd python && uv run pytest` | 2072 passed, 131 skipped, **3 failed**. All 3 are `RuntimeError: xmllint is required` (`test_cross_platform_consistency.py`, `test_repro_issue23.py`). xmllint is not installed here; this is an environment prerequisite (AGENTS.md), not a regression |
| Python lint | `uv run ruff check . && uv run ruff format --check .` | clean (252 files) |
| Python types | `uv run mypy src` | clean (42 files) |
| Node | `cd node && npm ci && npm run build && npm run test` | core 1120 passed / 32 skipped; mcp-server 338 passed; n8n 51 passed |
| Node lint | `npm run lint` | clean |
| LangChain | `uv run --all-groups pytest` / ruff / mypy | 212 passed; clean. Plain `uv run pytest` fails collection: `langchain-tests` is in a non-default dependency group |

#### Benchmark replay: identical agent calls, 3.0.7 → pre → HEAD

Scope: 104 replayed runs across the 7 write-cases (redline-net60-new-york, comment-liability-cap,
accept-reject-amounts-dates, rename-seller-vendor, chained-edits, stale-colleague-edit, compare-v1-v2).

- **No regressions.** 103/104 runs score identically on 3.0.7 and HEAD, and every HEAD output that was clean
  before is still `ooxml-valid: yes` and stetdoc-valid.
- **One improvement:** stale-colleague-edit `e-uEmpbJ` goes 8/9 → 9/9. Task 10's in-place update of `*.redline.docx`
  is the cause; pre `b69acb7` still scores 8/9.
- **compare-v1-v2 does not improve with the same agent batches:** 0/10 on 3.0.7, pre and HEAD.
  - Tasks 1–2 did fix paragraph merges. Run `e-dXOiiP`'s untracked-diff lines drop 18 → 3; pre is still 18.
  - Every remaining failure is a tracked **paragraph split**.

#### compare-v1-v2 through the new and old one-step paths

All rows use the same fixtures.

| path | accuracy | Word-relevant validity |
|---|---|---|
| stetdoc `compare` (calibration) | 8/8 | ooxml new 0 |
| Node MCP `diff_docx_files(output_path=…)`, HEAD (Task 4) | **8/8** | ooxml new 12: 7 duplicate `w:id`, 5 `tblBorders` order inherited from v1 |
| Python CLI `adeu diff v1 v2 -o out.docx`, HEAD (Task 4) | **6/8** (fails `all-changes-tracked`, `nothing-else-changed`) | new 19 class-A findings |
| Python CLI `adeu diff --json` → `adeu apply`, 3.0.7 (Gemini's pipeline in final-2) | 6/8 | — |
| same two-step pipeline, pre | 6/8 | — |
| same two-step pipeline, HEAD | **8/8** | ooxml new 12, same classes as the Node row |

final-2's Word opened every adeu compare output as CLEAN despite duplicate ids, so the ooxml errors there do not
cost points. They do keep "machine-valid" out of reach (Task 7).

#### long-indenture-markup at HEAD

Manual mode, three edits with comments (`.worktrees/bench-data/calls/indenture-manual.json`):

- **Node MCP** (`markup_mode="manual"`) and **Python CLI** (`adeu apply --manual`) both write:
  - `ooxml-valid: yes`, 0 new errors;
  - stetdoc-valid, `mc_prefix_undeclared` 0;
  - 0 tracked changes;
  - comments present.
- **Task 5 repair confirmed:** one tracked edit on the indenture gives `mc_prefix_undeclared=1` on 3.0.7 and pre, and
  0 on HEAD.

That removes the class-B root cause **whenever Adeu writes the file**.

#### New defects found while verifying

Each was reproduced on HEAD; the plan task that fixes it is in brackets.

1. **Python one-step compare is wrong** [Task 1]. `cli.py:1257-1265` and `document.py:989-1014` call
   `RedlineEngine.apply_edits` on the structured diff. On structured edit #21 (`'Termination**\n\n**5.1 Close-out
   Amount'` → `'… — Mandatory Dealer Poll'`) this:
   - deletes "Termination", the heading's paragraph mark and "5.1 Close-out Amount";
   - re-inserts them with literal `**`.

   With every revision rejected, the two headings merge ("5. Close-out and Early Termination5.1 Close-out Amount").
   The same edits through `process_batch` insert only " — Mandatory Dealer Poll". Reproduction:
   `.worktrees/bench-data/probe_py_onestep.py apply_edits|process_batch`.
2. **One-step compare writes unsafely** [Task 1].
   - Both engines write the output even when `skipped > 0`, which breaks the "a saved file with skipped edits is
     impossible" invariant (AI_CONTEXT.md §6, QA F2).
   - Neither guards `output_path == original_path`. Node `diff_docx_files(output_path=<original>)` overwrote v1 and
     reported only "Redline saved to"; v1-unchanged then failed.
   - Python's async tool runs the whole diff+apply on the event loop, not via `asyncio.to_thread` (AI_CONTEXT.md §12).
3. **Tracked paragraph splits read as untracked edits under the benchmark reader** [Task 2]. stetdoc's `--mode original`
   view prints a paragraph whose mark is a tracked insertion as `{joins-next} …`.
   - `report.sh:72` strips `{joins-next}` but never joins the two blocks, so any split shows up as an
     `untracked-diff`.
   - stetdoc's **own** tracked `split` operation fails the same way:
     `stetdoc edit master-supply-agreement.docx --ops '[{"op":"split","ref":"p145","before":"The amount payable"}]'`
     → original view `p145 {joins-next} …` / `p146 The amount payable…`.
   - stetdoc's arm never splits: its compare writes whole-paragraph deletions and insertions.
   - Adeu's split shape is correct OOXML (ECMA-376 §17.13.5.15, the inserted paragraph mark). It loses points only
     because of this reader artifact.
4. **Node fails to append after a bold run before trailing punctuation** [Task 3]. Document:
   `…determined in accordance with **Section 6(e) of the Agreement**.`. Edit: `target_text="in accordance with
   Section 6(e) of the Agreement."` → `"…Agreement, as supplemented by …."`.
   - Node: `BatchValidationError: - Failed to apply edit targeting: '**...'` (`engine.ts:6488-6496`) on HEAD, pre and
     3.0.7.
   - Python applies it.

   With `partial=false`, this one edit rolled back a 33-edit batch in final-2 run `e-jEKrtV`. The message names the
   internal sub-edit (`**`), not the caller's target.
5. **Header text is indistinguishable from body text** [Task 4].
   - `read_docx` prints the page-header line ("Pinnacle Health Systems, Inc. — … — Indenture") as the first line of
     page 1, with no marker.
   - An "Executive Summary at the top" edit anchored on that line wrote a Heading plus a `w:tbl` into
     `word/header1.xml` in both engines. Every page would repeat it.
   - Python warned only that the edit's comment was dropped; Node said nothing.
   - The indenture and underwriting prompts both ask for a summary at the top.
6. **New parameters never reach the model in Claude Code** [Task 5]. AI_CONTEXT.md §7a: clients drop optional-parameter
   descriptions, so the tool description is the only reliable channel.
   - Node `DIFF_DOCX_DESC` (`index.ts:308-309`) does not mention `output_path`/`author_name`.
   - `PROCESS_BATCH_*_DESC` (`index.ts:303-306`) does not mention `markup_mode`.
   - The skill is now the only place that teaches them.
7. **Manual-mode receipts are false** [Task 6].
   - The Node manual batch reported `**IDs:** Chg:1, Chg:2` and "Document State: 6 tracked change(s)". The saved
     file has 0 tracked changes (stetdoc ledger, and HEAD's own `read_docx(mode="changes")`).
   - `read_docx` renders struck and underlined text as plain text, so an agent cannot read back what it marked.
8. **Duplicate revision ids** [Task 7]. Both engines repeat one `w:id` across:
   - a multi-paragraph insertion's run `w:ins` and paragraph-mark `w:ins`;
   - since Task 2, a deleted paragraph's mark `w:del` and its run `w:del`.

   The Open XML SDK reports `Sem_UniqueAttributeValue`; stetdoc reports `revision_id_duplicate`, which it classes
   as "Word would reject or repair".

#### Verdict on the 2026-11-06 plan

Per-task status is in the table below. In short:

- **Real, verified wins:**
  - Task 2 (paragraph merges on reject);
  - Task 4 in **Node**;
  - Task 5 (CT_RPr / CT_ParaRPr order and `mc:Ignorable` repair, which removes the indenture `REPAIR_PROMPT` cause);
  - Task 6 output validity;
  - Task 10 in-place derivative files;
  - the Python two-step compare pipeline (6/8 → 8/8).
- **Gaps:**
  - Task 4's Python path is wrong (defect 1).
  - Task 1 is correct OOXML but does not change the benchmark verdict (defect 3).
  - Task 7 is only partly implemented (see table).
  - Several claimed contracts break repo invariants (defects 2, 6, 7).
- **Expected benchmark effect of HEAD.** This is a projection; no live agent was run.
  - compare-v1-v2 passes only when the agent uses the one-step **Node** tool, which the new skill prescribes.
  - It still fails when the agent hand-writes batches that split paragraphs (what every final-2 Claude run did), or
    follows `references/cli-fallback.md`'s new `adeu diff … -o` recipe (defect 1).
  - The indenture validity loss is fixed only if the agent writes through `process_document_batch(markup_mode=
    "manual")` and not through python-docx.

#### Per-task audit of the 2026-11-06 plan

Each previous-plan task's Change bullets were checked against the code at `5e9aaa0`. The "Verified this session" column
marks claims re-read directly while writing this plan; the rest come from a read-only audit pass over the same tree
(file:line cited) and were not re-read.

| prev. task | status | gaps at HEAD | evidence | picked up by |
|---|---|---|---|---|
| 1 Split pilcrow / reject merge | done, both engines | none in code; benchmark reader still fails splits (defect 3) | `engine.py:1820-1897, 6541-6550, 7642-7756`; `engine.ts:2406-2533, 3306-3394`; `paragraph_split_reject_parity` tests (Node lacks Python's second `src_p` test) | Task 2, Task 12 |
| 2 Whole-paragraph delete reject | done, both engines | duplicate `w:id` on mark `w:del` + run `w:del` (defect 8) | `engine.py:6670-6745`; `engine.ts:9599-9675` | Task 7 |
| 3 Diff sub-alignment / balance | done in Python; DP sub-alignment in both | Node has no `make_edits_self_contained`. **By design, not a gap**: Node pins `_match_start_index` positionally (`diff.ts:1190-1192`, verified). Synthetic `Diff:` comments are still generated, then stripped downstream (`cli.py:1248-1249`, verified). `_has_balanced_markdown` checks parity only, not nesting | `diff.py:647-672, 1480-1495`; `diff.ts:720-746` | none (record only) |
| 4 One-step compare | Node correct; Python wrong (defect 1); unsafe in both (defect 2) | LangChain `diff_docx.py` has no `output_path`/`author_name` (verified: untouched by `5e9aaa0`). CLI `-o x.docx` without `--author` is refused by `_guard_text_output_path` (`cli.py:1177-1186, 311-316`, verified) | `document.py:944-1031`; `index.ts:1484-1544`; `cli.py:1257-1265` (verified) | Task 1 |
| 5 Schema order / `mc:Ignorable` | done for `mc:Ignorable`, CT_RPr and CT_ParaRPr order | `_insert_para_rpr_track_change` helper not created (ordering inlined at each call site; behaviour correct). Empty `<w:pPr/>` pruning not implemented. Node `comments.ts:760` `appendChild(pPr)` is **not a bug**: the `<w:p>` is freshly created and empty, so `pPr` lands first (`comments.ts:749-760`, verified). Node compliance test omits a CT_ParaRPr assertion | `engine.py:6868-6916`; `bridge.ts:276-306`; `engine.ts:3776-3872` | Task 9 (pPr pruning, Node test) |
| 6 Manual markup mode | conversion done in both engines + CLI `--manual` | inline `~~`/`++` in `new_text` **not implemented** (`engine.py:1261, 1315`, `_find_next_emphasis` / `_parse_inline_markdown` know only `**` and `_`, verified). `manual_del_props`/`manual_ins_props` not implemented. LangChain `apply_changes` has no `markup_mode` (verified). Node batch description omits `markup_mode` (defect 6). Receipts false (defect 7) | `engine.py:4022-4140`; `engine.ts:6532-6655`; `cli.py:1448, 2288` | Task 5, Task 6, Task 9 |
| 7 Tables / creation | pipe tables in `new_text` done, both engines | `insert_table` change type and `create_if_missing` / `adeu create` **do not exist** (verified: grep finds neither outside `test_insert_table_row_*`) | `engine.py:1440-1550`; `engine.ts:3000-3096` | Task 9 |
| 8 Fuzzy whitespace context | done, both engines | none | `engine.py:5247-5300`; `engine.ts:8117-8170` | — |
| 9 `read_docx` ergonomics | done, both engines | `page="all"` budget refusals (final-2's top tool error) untouched | `safe-regex.ts:131-155`; `_response_builders.py:467-479`; `outline.py:778-846` | Task 10 |
| 10 Derivative names / receipts | done at all 6 sites | action receipts have no clean paragraph preview (claimed, not implemented). Manual-mode IDs/counts false (defect 7) | `document.py:733`; `cli.py:1483`; `engine.py:3509-3548, 4019-4033`; `engine.ts:5967-6088` | Task 6, Task 9 |
| 11 Skill streamlining | done | says `partial=false` is the default; the tools default to salvage (`SKILL.md:58`, `mcp-tools.md:38` vs `document.py:631` `partial: bool = True`, `index.ts:304`, verified). Teaches mid-paragraph `\n\n` splits (`SKILL.md:66`, verified). Recommends `adeu diff -o` (`cli-fallback.md:32`, verified; defect 1) | `skills/adeu-redlining/` | Task 8 |

Previous-plan test files exist for every task, under different names in three cases (`test_diff_subalignment_and_balance.py`
for `test_diff.py`; `test_batch_ergonomics_and_receipts.{py,test.ts}` for `test_batch_receipts_and_paths`).

## Assumptions

- The benchmark is scored as in final-2: grader regexes over the stetdoc-generated report, and validity judged by
  Word (machine checks only triage). The score that matters is "fully correct".
- Dual-engine parity (AGENTS.md) holds for every engine change below. The Node path matters most for the
  benchmark: Claude Code and Antigravity both run `@adeu/mcp-server`.
- No live agent runs are part of verification. Each task's "Verify" uses unit tests in both engines (the
  committed evidence) plus the local, uncommitted replay harness (Task 0). A live benchmark rerun is a separate, paid
  step for the user.
- Word is not available on the verification machine. "Word-valid" below means ooxml-validate reports 0 new errors
  and stetdoc reports no new class-A findings. final-2 shows these two checks are necessary but not sufficient:
  6 of 511 files clean under both still got a repair prompt.

## Tasks

Priority: **P0** blocks benchmark parity; **P1** is a correctness/robustness gap the benchmark exposes; **P2** is
hygiene.

### Task 0: Benchmark harness: dropped from the repo

- **Status:** `[x] Dropped (user decision, 2026-11-07)`. The benchmark is not Adeu's code; the repo carries only Adeu
  fixes. The harness stays local and gitignored in `.worktrees/bench-data/`, with environment-configurable paths
  (`BENCH_DIR`, `HARVEY_DIR`, `STETDOC`, `OOXML_VALIDATE`, `BENCH_RAW`, `BENCH_RUNS`) and its setup notes in
  `.worktrees/bench-data/README.md`. Every "harness" step below means that local copy; a fresh checkout cannot run
  it, so the repo-side evidence for each task is its unit tests.
- **Verified locally, 2026-11-07:** `run_case.py calib compare-v1-v2 stetdoc-compare` → 8/8;
  `replay_all.py --server old=<3.0.7> --server head=<HEAD> compare-v1-v2` reproduces final-2's failed-grader set on
  all 10 Claude runs.

### Task 1 (P0): Make Python one-step compare correct and safe; harden the Node one

- **Status:** `[x] Done (2026-11-07)`. Root cause: `apply_edits`' pinned branch applied a context-widened edit raw;
  only text-match resolution re-trims widened context. Fix: `make_edits_self_contained` flags `_widened`, and the
  pinned branch resolves flagged edits through the shared per-occurrence pipeline (`_resolve_occurrence`). All
  one-step writers go through `adeu/compare.py` / `@adeu/core` `compare.ts` (transactional, alias-guarded); the
  Python MCP tool runs in `asyncio.to_thread` and raises `ToolError`; `adeu diff -o x.docx` works without
  `--author`; LangChain `adeu_diff_docx` gains `output_path`/`author_name`. Node needs no widening fix (its diff
  never widens). `test_cli_bug_repro.py::test_diff_output_flag_text_and_guards` step 5 pinned the superseded
  "`-o x.docx` is refused" contract and was updated. Local harness: Python CLI one-step 6/8 → 8/8.
- **Goal:** `adeu diff v1 v2 -o out.docx`, Python MCP `diff_docx_files(output_path=…)` and LangChain's diff tool
  produce the same redline as the passing two-step path. They never overwrite an input and never write a file with
  skipped edits.
- **Files:**
  - Python: `python/src/adeu/cli.py` (`handle_diff`, ~1167-1265); `python/src/adeu/mcp_components/tools/document.py`
    (`diff_docx_files`, 929-1031); `python/src/adeu/redline/engine.py` (`apply_edits` pinned path, 4605+);
  - Node: `node/packages/mcp-server/src/index.ts` (`diff_docx_files`, 1461-1592);
  - LangChain: `langchain/langchain_adeu/diff_docx.py`;
  - Tests: `python/tests/test_mcp_diff_output.py`, `node/packages/mcp-server/src/diff_output.test.ts`.
- **Change:**
  1. **Root cause first.** Find why `apply_edits` on a pinned structured edit whose target crosses `\n\n` and `**`
     (edit #21 above) skips the affix trimming that `process_batch` applies. Then either:
     - fix the pinned path (preferred if other `apply_edits` callers depend on pins); or
     - route every one-step writer through one shared function that replays the structured edits through the
       sequential `process_batch` contract, exactly as `adeu diff --json | adeu apply` does.

     Mirror the decision in Node, even though Node passes today: both engines must share one contract.
  2. **Transactional:** if any edit is skipped, raise and write nothing. Reuse the CLI failure contract
     (`batch_validation_failed`) and the MCP error shape.
  3. **Alias guard:** refuse `output_path` that resolves to `original_path` or `modified_path` (realpath/samefile,
     as `_paths_alias_same_file` does). Any write over an existing file says "replaced existing file"
     (AI_CONTEXT.md §7a, QA F16-F19).
  4. **Python MCP:** run extraction, diff, apply and save inside `asyncio.to_thread` with keyword arguments
     (AI_CONTEXT.md §12). Return `isError` on failure, not a plain "Error computing diff" string.
  5. **CLI:** `adeu diff a.docx b.docx -o out.docx` must work without `--author`. Today a missing `--author` routes
     to `_guard_text_output_path` (`cli.py:1177-1186`), which refuses `.docx` targets.
- **Verify:**
  - New tests in both engines on a fixture built to the edit #21 shape: a bold heading followed by a bold numbered
    sub-heading, where only the sub-heading gains a suffix. Assert:
    - the one-step output's rejected view equals v1 and its accepted view equals v2;
    - no literal `**`;
    - the output is byte-identical to the two-step output.
  - Tests that `output_path == original_path` is refused, and that a forced skipped edit writes nothing.
  - `cd python && uv run pytest tests/test_mcp_diff_output.py tests/test_cli*.py`;
    `cd node && npm run build && npm test`.
  - Harness: `run_case.py head compare-v1-v2 py <python> agents/py_cli_onestep.py` → 8/8.
- **Done when:** all four one-step surfaces (CLI, Python MCP, Node MCP, LangChain) score 8/8 on compare-v1-v2 in the
  harness, and none can overwrite an input or save a partial redline.

### Task 2 (P0, needs a product decision): Split-free paragraph restructuring

- **Status:** `[x] Decided, no engine change (user, 2026-11-07)`. Keep the inserted-pilcrow split: it is correct,
  Word-native OOXML and loses points only to a reader artifact in a benchmark that is not Adeu's. Agents are steered
  away from mid-paragraph splits by Task 8 item 2 and toward the one-step compare by Task 8 item 1. The options below
  are kept for the record.
- **Goal:** An edit that inserts paragraph breaks inside an existing paragraph leaves the original paragraph intact
  in the "all revisions rejected" view, even for a reader that does not merge tracked paragraph marks. That reader
  is the benchmark's (`report.sh:72`), and others behave the same way.
- **Decision for the user:**
  - **Option A, recommended default:** represent a split as a rewrite of the shorter side:
    - the original paragraph keeps its own, untracked mark;
    - the moved text is a tracked deletion in the original paragraph plus a tracked insertion in a new paragraph
      whose mark is a tracked insertion;
    - new paragraphs go **before** the original paragraph when the prefix is shorter, **after** it when the suffix
      is shorter.

    Robust everywhere, at the cost of showing the moved fragment as struck and re-inserted.
  - **Option B:** keep today's inserted pilcrow (Word-native and minimal) as the default, behind a
    `split_style="pilcrow"|"rewrite"` parameter, and file the grader issue (Task 12). The benchmark score then
    depends on the upstream fix.
- **Files:**
  - Python: `python/src/adeu/redline/engine.py` (`track_insert` ~1529, the multi-paragraph modification branch);
  - Node: `node/packages/core/src/engine.ts` (`_track_insert_multiline` ~3098 and its callers ~9252/9513/9570);
  - New tests: `python/tests/test_split_free_restructure.py`, `node/packages/core/src/split_free_restructure.test.ts`;
  - Docs: AI_CONTEXT.md §11 ("Paragraph Splits Relocate the Suffix" must describe the new shape).
- **Change:**
  1. Detect the split condition at resolution time: the resolved edit inserts ≥1 paragraph break, and the host
     paragraph keeps original (non-deleted) visible text on both sides of the insertion point.
  2. Under `rewrite`:
     - choose the shorter side;
     - emit a tracked deletion of that side's original runs, preserving run properties;
     - emit tracked insertions of the same text, with the same rPr, in the new paragraph(s);
     - stamp `w:ins` only on the new paragraphs' marks;
     - leave the host paragraph's mark untouched;
     - copy the host's pPr to the new paragraph that receives the moved fragment.
  3. Keep comment anchoring rules: a commented split stays one atomic change, per AI_CONTEXT.md §6 "A Commented
     Modify Is Never Word-Split".
  4. Parity: identical XML shape in both engines; add the case to `shared/conformance/` goldens.
- **Verify:**
  - For prefix-split, suffix-split, middle-replacement split, a pure `\n\n` insertion and a 3-way split, assert:
    - (a) `reject_all_revisions` yields the original paragraph byte-for-byte in text;
    - (b) `accept_all_revisions` yields the requested paragraphs;
    - (c) stetdoc `--mode original`, through the harness `body()`, equals the original.
  - Harness: replay all 10 final-2 Claude compare-v1-v2 runs and the underwriting run `a-jPN7dL` on HEAD.
- **Done when:** under Option A, ≥9/10 replayed compare-v1-v2 runs pass `all-changes-tracked` with no other grader
  regressing, and the 103 already-equal replays stay equal.

### Task 3 (P0): Node parity for insertions at style-marker boundaries; actionable apply errors

- **Status:** `[x] Done (2026-11-07, f052395)`. Correction to the premise: Python did NOT succeed. It reported the
  edit applied and silently dropped the inserted clause (the virtual-only no-op branch); Node failed the batch. Both
  engines produce the same `"**" → ", as supplemented …"` hunk. Fix in both: a marker-only-target hunk with real
  replacement text becomes an insertion at the hunk start; Python's virtual no-op branch now covers deletions only;
  apply failures name the caller's target (`_report_target`, first 40 chars as before; no reason code added). Tests:
  `test_marker_boundary_insert.py`, `marker_boundary_insert.test.ts` (Node red before the fix: 3/3 failed). The
  benchmark-fixture repro (`tidemark-draft-confirmation.docx`) now applies 1/0 on Node.
- **Goal:** Appending or inserting text next to a bold/italic run, as the plain-text target in the defect-4
  example does, applies in Node exactly as in Python. When an apply-stage failure does happen, it names the caller's
  edit and the reason.
- **Files:**
  - Node: `node/packages/core/src/engine.ts` (sub-edit resolution and the failure site 6488-6496, plus
    `_word_diff_sub_edits` / marker-only hunk dropping);
  - Python: `python/src/adeu/redline/engine.py` (message parity at 4706/4739/4886);
  - New tests: `node/packages/core/src/marker_boundary_insert.test.ts` and a Python twin.
- **Change:**
  1. Reproduce with the tidemark fixture's `**Section 6(e) of the Agreement**.` (probe:
     `.worktrees/bench-data/probe_split.mjs … "^in accordance with Section 6\(e\)"`).
  2. Find which sub-edit carries `target_text == "**"`. Make Node drop marker-only hunks and anchor the insertion on
     the last real span, as Python does (AI_CONTEXT.md §1 "Resolved Ranges May Start on Virtual Spans" and §11
     "Word-diff sub-edits DROP marker-only hunks").
  3. Failure messages use the parent edit's `target_text` (first 60 chars) plus a reason code, for example "insertion
     point falls on formatting markup". Never use the sub-edit's text.
- **Verify:** the new tests pass in both engines; harness replay of `e-jEKrtV` and `e-Vyxkx1` shows 0 batch rejections.
- **Done when:** the final-2 compare replays have no `Failed to apply edit targeting: '**...'`, and the same edit list
  applies identically in both engines.

### Task 4 (P0): Guard block structure in headers and footers; disclose the part an edit lands in

- **Status:** `[x] Done (2026-11-07)`. Narrowed from the plan: only an added Markdown heading line or pipe-table row
  is refused. Plain extra paragraphs in a header stay allowed (letterheads have several), so no override flag is
  needed. The refusal names the part and the first/last body line, plus `match_mode="first"` when that line recurs
  (found on the real indenture: its first body line `## **INDENTURE**` appears twice). Receipts carry `part` and
  render `**Part:**` (both MCP servers, CLI). Tests: `test_header_block_guard.py`, `header_block_guard.test.ts`
  (Node red before the fix: 5/5). Local harness: the final-2 summary edit is refused with that hint; retargeted as
  instructed, all 3 edits apply, the summary table lands in `word/document.xml`, header untouched, `ooxml-valid: yes`.
- **Goal:** An agent placing an executive summary "at the top of the document" cannot silently write headings,
  tables or extra paragraphs into a page header or footer.
- **Files:**
  - Python: `python/src/adeu/redline/engine.py` (validation, using mapper `part_boundary_at`);
  - Node: `node/packages/core/src/engine.ts`, `node/packages/core/src/mapper.ts`;
  - MCP response builders in both servers;
  - `skills/adeu-redlining/SKILL.md`.
- **Change:**
  1. Validation: reject a `modify` whose resolved range is in a header or footer part when its `new_text` adds a
     paragraph break, a heading marker, a list marker or a Markdown table. Use a teaching message: "This text is the
     page header (word/header1.xml), repeated on every page. To add content at the start of the document, target the
     first body paragraph: '<first body paragraph snippet>'". Inline wording changes in headers stay allowed.
  2. Every per-edit receipt whose range is outside the main story names the part (`**Part:** header
     (word/header1.xml)`).
  3. **Decision (deferred):** whether `read_docx` should mark header/footer regions inline. This changes the Virtual
     Text contract and every golden, so it is out of scope here; items 1-2 handle the hazard without it.
- **Verify:**
  - Tests in both engines: the exec-summary edit on the indenture header is rejected with a message containing the
    first body paragraph; an inline header wording change still applies.
  - Harness: `calls/indenture-manual.json` with the summary retargeted to the first body paragraph → `ooxml-valid:
    yes` and the table in `word/document.xml`.
- **Done when:** no batch can put a table or heading into a header or footer part without an explicit override.

### Task 5 (P0): Tool descriptions carry the new parameters

- **Status:** `[ ] Pending`
- **Goal:** In Claude Code, which drops optional-parameter descriptions (AI_CONTEXT.md §7a), the model learns the
  one-step compare and manual markup from the tool descriptions alone.
- **Files:**
  - `node/packages/mcp-server/src/index.ts` (`DIFF_DOCX_DESC`, `PROCESS_BATCH_*_DESC`);
  - `python/src/adeu/mcp_components/tools/document.py`;
  - `node/packages/mcp-server/src/repro.qa_2026_07_23.client-compat.test.ts`;
  - `python/tests/test_gemini_schema_compat.py`.
- **Change:**
  1. `DIFF_DOCX_DESC` gains one sentence: "Pass `output_path` (and optional `author_name`) to write the comparison
     as a tracked-changes .docx in one step — the recommended way to make a redline between two versions."
  2. The batch description gains the `markup_mode="manual"` sentence (strikethrough/underline instead of Track
     Changes).
  3. Keep each description, including the build tag, under the pinned 2048-char budget; trim elsewhere if needed.
     Python descriptions get the same sentences.
- **Verify:** the client-compat test asserts both phrases are present and the length stays under budget, in both
  engines.
- **Done when:** both phrases are in both servers' published descriptions, under budget.

### Task 6 (P1): Truthful manual-markup receipts, and a projection that shows manual markup

- **Status:** `[ ] Pending`
- **Goal:** After a `markup_mode="manual"` batch, the receipt and `read_docx` tell the agent what is struck and what
  is inserted. No fictitious `Chg:N` ids or tracked-change counts.
- **Files:**
  - Python: `python/src/adeu/redline/engine.py` (report/receipt fields); `python/src/adeu/ingest.py` +
    `python/src/adeu/redline/mapper.py` (Virtual Text contract);
  - Node: `node/packages/core/src/engine.ts`; `node/packages/core/src/ingest.ts` + `node/packages/core/src/mapper.ts`;
  - Both MCP response builders.
- **Change:**
  1. In manual mode, per-edit receipts omit `IDs` (or list comment ids only). "Document State" counts the saved
     document's real revisions and comments, recomputed after save.
  2. **Decision:** project manually struck runs as `~~text~~` and manually inserted (underline + colour) runs as
     `++text++` in `read_docx`. The previous plan's Task 6 promised these tokens as `new_text` input, but they were
     never implemented (audit table). Build both directions together, so read-back and write use one dialect:
     `_find_next_emphasis` / `_parse_inline_markdown` in both engines learn `~~` and `++`.
     - This is a Virtual Text contract change, so mapper and ingest must project the same virtual spans.
     - Gate it so ordinary underlined or struck text in untouched documents is not reinterpreted: only runs carrying
       the exact manual-markup property set Adeu writes, or opt-in `show_manual_markup=true`.
- **Verify:** a test in both engines: a manual batch → receipt has no `Chg:` and "0 tracked change(s)" → `read_docx`
  shows `~~$50,000,000;~~++$25,000,000;++`.
- **Done when:** the manual-mode receipt and read-back match the saved document in both engines.

### Task 7 (P1): Unique revision ids in both engines

- **Status:** `[ ] Pending`
- **Goal:** No two revision elements in a part share a `w:id`. This brings ooxml-validate `Sem_UniqueAttributeValue`
  and stetdoc `revision_id_duplicate` to zero on Adeu-written redlines.
- **Files:**
  - Python: `python/src/adeu/redline/engine.py` (`reuse_id` paths, Phase-2 pilcrow stamping, group resolution in
    `_get_paired_nodes` / accept / reject); `python/tests/test_repro_chg_id_coalescing.py`;
  - Node: `node/packages/core/src/engine.ts` (same paths);
  - AI_CONTEXT.md §6 F1 ("A Logical Replacement Resolves as ONE Id-Set").
- **Change:**
  1. Mint a fresh id for every `w:ins`, `w:del`, `w:pPrChange` and `w:rPrChange` element, including paragraph-mark
     revisions.
  2. Replace "shared id = one logical revision" with an explicit group derivation for accept/reject and for the
     `(pairs with Chg:N)` projection. Derive groups from adjacency plus same author plus same `w:date`, which is
     how Word itself groups. Keep the user-visible `Chg:N` numbering stable by projecting the group's first id.
  3. Update AI_CONTEXT.md F1 and the "Multi-Run Deletion IDs" bullet.
- **Verify:**
  - `find_out_of_range_long_hex_numbers`-style guard: a new test scans every saved part for duplicate revision ids
    in both engines.
  - Existing reject/accept group tests stay green unmodified.
  - Harness compare outputs show 0 `Sem_UniqueAttributeValue`.
- **Done when:** Node one-step compare on compare-v1-v2 has 0 duplicate-id errors, and every group accept/reject
  test passes.

### Task 8 (P0): Skill and reference corrections for the benchmark workflows

- **Status:** `[ ] Pending`
- **Goal:** The skill steers agents onto the paths verified to pass, and documents only features that exist.
- **Files:** `skills/adeu-redlining/SKILL.md`, `references/mcp-tools.md`, `references/cli-fallback.md`.
- **Change:**
  1. **Compare:** "Always produce a redline between two files with `diff_docx_files(…, output_path=…)`. Never
     hand-build compare batches." Until Task 1 lands, the CLI fallback must recommend `adeu diff --json` → `adeu
     apply`, not `adeu diff -o` (defect 1).
  2. **Paragraph restructuring:** until Task 2 lands, "to split or restructure paragraphs, target the whole
     paragraph(s) and supply the complete new paragraphs; never insert `\n\n` into the middle of an existing
     paragraph."
  3. **Summaries at the top:** "The first lines of page 1 may be the page header; anchor document-top insertions on
     the first body paragraph (check `mode="outline"`)."
  4. **Manual markup:** "When asked for strikethrough/underline markup instead of Track Changes, use
     `markup_mode="manual"`; do not script python-docx."
  5. **Fix the `partial` default:** `SKILL.md:58` and `mcp-tools.md:38` say batches are transactional by default
     (`partial=false`). Both servers default to salvage (`partial=true`; `document.py:631`, `index.ts:304`). State the
     real default, and tell agents to pass `partial=false` when a half-applied batch would be worse than none. The
     skill does not claim `insert_table` or document creation; keep it that way unless Task 9 builds them.
  6. Keep the skill self-contained so agents do not spend a turn reading `references/`. Turns and tool calls are
     reported metrics: adeu's median was 10–11.5 turns against stetdoc's 6.5–7 (`tables.md:98-113`).
- **Verify:** read the diff. `cd python && uv run pytest` and `cd node && npm test` stay green (any skill-metadata
  tests).
- **Done when:** every tool, parameter and recipe in the skill exists and passes the harness on the case it targets.

### Task 9 (P2): Finish or formally drop the previous plan's unimplemented items

- **Status:** `[ ] Pending`
- **Goal:** Every item the previous plan claimed but did not ship (audit table) is either built or recorded as
  dropped. Most urgent: long tasks need an executive-summary table and a cover-note `.docx`. Today agents drop to
  python-docx for both, which is exactly how indenture outputs inherited the bad `mc:Ignorable` root in final-2.
- **Files:**
  - Models: `python/src/adeu/models.py`, `node/packages/core/src/models.ts`;
  - Engines: `python/src/adeu/redline/engine.py`, `node/packages/core/src/engine.ts`;
  - MCP servers; CLI (`adeu create`);
  - LangChain: `langchain/langchain_adeu/apply_changes.py`;
  - Tests: `node/packages/core/src/ooxml_schema_compliance.test.ts`.
- **Change:**
  1. **Build or drop, then record the decision:**
     - an `insert_table` change type (both engines, zod enum, schema pins). Pipe tables in `new_text` already cover
       most uses;
     - a `create_docx(markdown, output_path)` tool / `adeu create`, built on a schema-valid template with declared
       namespaces. This is the one that matters for the CSA cover note.
  2. **Build (small, parity gaps):**
     - LangChain `apply_changes(…, markup_mode=…)`, passed to `RedlineEngine` as the CLI and both MCP servers do;
     - prune an empty `<w:pPr/>` from `<w:p>` after a revision mark or property is removed, in both engines;
     - add the CT_ParaRPr ordering assertion the Node compliance test lacks (Python has it).
  3. **Recommend dropping (record in the previous plan):**
     - `manual_del_props` / `manual_ins_props` constructor options: no caller needs them;
     - the shared `_insert_para_rpr_track_change` helper: the ordering is inlined and correct;
     - a Node `make_edits_self_contained`: Node pins edits positionally (`diff.ts:1190-1192`);
     - one-line paragraph previews in action receipts: Task 6 fixes the receipt fields that mislead agents.
  4. Anything built must pass ooxml-validate with 0 errors and stetdoc with 0 class-A findings.
- **Verify:** tests in both engines for each built item; harness: a CSA cover note created this way gives
  `created: …cover….docx` and `ooxml-valid: yes`.
- **Done when:** no item in the audit table is left both unbuilt and undecided, and the skill describes table and
  document creation exactly as shipped.

### Task 10 (P1): Bounded reads instead of refusals for `page="all"`

- **Status:** `[ ] Pending`
- **Goal:** Remove adeu's most common self-inflicted tool error: 58 `read_docx` errors in final-2's short runs, all
  "Refused unbounded full document read" (census: `.worktrees/bench-data/error_census.py short`).
- **Files:**
  - `python/src/adeu/mcp_components/_response_builders.py`, `node/packages/mcp-server/src/response-builders.ts`;
  - `python/tests/test_response_budget_guard.py`, `node/packages/mcp-server/src/budget-guard.test.ts`.
- **Change:** **Decision.** When `page="all"` exceeds the budget, return the outline plus the first in-budget page
  range with the continuation footer (Task 9 of the previous plan) as a **successful** result, not an error. Keep
  `force=true` for the full body. Update both budget-guard tests deliberately, as their own change: the old
  refusal is a contract, not a bug.
- **Verify:** both budget-guard tests updated and green; harness replays show 0 `read_docx` errors.
- **Done when:** no `read_docx` call in the final-2 replays returns `isError`.

### Task 11 (P2): Optional repair of pre-existing schema-order violations

- **Status:** `[ ] Pending`
- **Goal:** Adeu-written files do not count inherited out-of-order elements as new errors when they are validated
  against a different base. Example: compare-v1-v2's v1 has `w:tblBorders` out of order in every `w:tblPr`, while
  base-final v2 does not.
- **Files:** both engines' save paths.
- **Change:** an opt-in `repair_schema_order` that reorders children of `w:tblPr`, `w:rPr` and `w:pPr` in parts Adeu
  already re-serializes. It is off by default because of surgical mode (AI_CONTEXT.md §2). One-step compare turns it
  on, since its output is a new document.
- **Verify:** the harness compare row shows 0 `tblBorders` errors with it on; untouched-part byte identity is
  unchanged with it off.
- **Done when:** one-step compare outputs validate with 0 new ooxml errors against v2.

### Task 12 (external, needs the user): Report the grader artifact upstream

- **Status:** `[x] Out of scope for this implementation (user, 2026-11-07: only Adeu changes)`. Not filed. The
  evidence below stays here for the user's decision.
- **Goal:** The benchmark stops penalizing correct tracked paragraph splits for every arm.
- **Change:** File an issue on clauseo-ai/docx-agent-bench. Evidence:
  1. `report.sh:72` removes `{joins-next}` without joining blocks;
  2. stetdoc's own `split` operation produces `{joins-next}` and would fail `all-changes-tracked`;
  3. a one-line awk fix in `body()` joins a `{joins-next}` block with the next block before comparison.

  Opening the issue is the user's call; this task drafts the text.
- **Verify:** n/a (external).
- **Done when:** the user has decided whether to file it.

## Risks

- **Task 2 changes what a split looks like in every redline.** Lawyers may prefer the minimal pilcrow. Mitigation:
  the `split_style` switch, and a conformance golden for each shape.
- **Task 7 rewrites revision grouping**, the area with the most historical regressions (QA F1, ADEU-QA-004).
  Mitigation: land it alone, run the Hypothesis hunt profile, and keep every existing group test unmodified.
- **Replays are not live agents.** HEAD agents will make different calls (for example, the one-step compare). The
  harness measures the engine on yesterday's decisions plus the scripted ideal path, not tomorrow's agent
  behaviour. Only a paid live rerun (the benchmark's `scripts/run-all.sh`, `RUNS=5 LONG_RUNS=2`) settles the final
  number.
- **Word remains the judge of validity.** 7 files in final-2 passed both machine checks and still got a repair prompt.
  Task 4 and Task 7 reduce risk; they cannot prove Word acceptance without Word.
- **Benchmark conflict of interest** (README "Conflict of interest"). The reader and graders are stetdoc's. Task 12
  addresses the one artifact found; others may exist.

PLAN COMPLETE
