# Adeu Agent-Efficiency Complete Implementation Plan (Python Package)

**Target Version:** `adeu 2.0.0` / `2.1.0`
**Spec Reference:** `docs/improvement_spec.md`
**Scope:** `python/src/adeu/` and `python/tests/`

---

## Context & Environment Rules
- **Repo Context:** Touches `python/src/adeu/` and `python/tests/`. Package version is **2.0.0** (`python/pyproject.toml:3`).
- **Toolchain Commands (from `python/`):**
  - Lint: `uv run ruff check . && uv run ruff format --check .` (line-length 120)
  - Types: `uv run mypy src`
  - Tests: `uv run pytest` (addopts `-n auto --dist loadgroup`); single file `uv run pytest tests/test_page_ranges.py`; serial debug `uv run pytest -n 0`
- **Response Builders:** Framework-free in `python/src/adeu/mcp_components/_response_builders.py`. Imports no `fastmcp` or `engine` modules to keep CLI and MCP fast.
- **Test-First Requirement:** Write failing unit/integration tests in `python/tests/` before writing implementation code for every single task.
- **Token Budget Assertions:** Assert using `approx_tokens(s) = len(s) // 4` in `python/tests/utils.py`.

---

## Phase 1 (P0 Items) — Tasks 0 to 5

### Task 0 — Synthetic Conformance Fixture Builder (COMPLETED)
**Goal.** One reusable builder that produces the multi-author / paired / threaded / tabular / multi-page DOCX every P0 acceptance test needs.
**Status.** Completed & Verified (commit `226247e`).

### Task 1 — Item A6: Native Page Ranges (`--page 2-6`) (COMPLETED)
**Goal.** One call returns pages N..M, capped at 8, with an explicit continue-with note.
**Status.** Completed & Verified (commit `ef4736c`).

### Task 2 — Item A1: `--mode changes` Tracked-Change & Comment Ledger (COMPLETED)
**Goal.** Enumerate every tracked change and comment in ≤18 tokens/change.
**Status.** Completed & Verified (commit `990a23b`).
**Attempt Ledger:**
- attempt 1: Used newline-anchored regex and bubble_raw[:first_com_start] cutoff -> VERDICT: FAIL
- attempt 2: Tokenised [Chg:...] and [Com:...] tags in a single pass, used heuristic `_is_header_author_text` -> VERDICT: FAIL
- attempt 3 (@opus-coder): Used line-start position (`bubble_raw.rfind("\n", 0, tm.start()) + 1`) to classify Chg tag after Com delimiter as header if line-prefix is whitespace-only, deleted `_is_header_author_text` and `prose_words` -> VERDICT: PASS

### Task 3 — Item B9: Uniform Failure Envelope with Machine-Readable Blame (COMPLETED)
**Goal.** Both schema and engine validation errors emit `{"error", "failed":[{"index","reason"}], "message"}` with 0-based batch indices.
**Status.** Completed & Verified (commit `b731172`).
**Attempt Ledger:**
- attempt 1: Created payloads.py and regex fallback indexing in engine.py/cli.py -> VERDICT: FAIL
- attempt 2: Passed batch_idx explicitly to action/duplicate-id error generators in engine.py, mapped original indices through document.py to preserve input array relative indexing, grouped schema failures by index in cli.py, updated test_failure_envelope.py -> VERDICT: PASS
**Files.**
- `python/src/adeu/payloads.py` (new)
- `python/src/adeu/redline/engine.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_failure_envelope.py` (new)
**Test first.**
1. `test_schema_failure_envelope_indices`: missing field batch at index 1 -> `error == "invalid_changes_file"`, `failed == [{"index": 1, "reason": ...}]`.
2. `test_engine_failure_envelope_indices`: target text mismatch at index 2 -> `error == "batch_validation_failed"`, `[f["index"] for f in failed] == [2]`.
3. `test_multi_failure_indices`: bad edits at 0 and 3 -> `[0, 3]`.
4. `test_action_failure_index_is_batch_relative`: action failure index is relative to input `changes` array.
5. `test_message_field_always_present_and_one_line`: `"\n" not in message`.
6. `test_mcp_failure_carries_envelope`: JSON block in MCP failure string contains `failed` indices.
7. `test_prose_still_present_for_humans`: existing prose error strings survive in `errors`.
**Change.**
- `payloads.py`: `def failure_envelope(code: str, failed: list[tuple[int, str]], message: str) -> dict`.
- `engine.py`: add `failed: list[tuple[int, str]]` to `BatchValidationError`. Map action and edit failures to 0-based batch indices.
- `cli.py`: update `_cli_error` and `_format_batch_validation_error` to return envelope JSON when `_JSON_MODE`.
- `document.py`: `_normalize_changes` returns `rejected_pairs`. Append fenced JSON envelope block to MCP failure strings.
**Done when.** `uv run pytest tests/test_failure_envelope.py` passes (7 tests).

### Task 4 — Item B1: `--report minimal|standard` (COMPLETED)
**Goal.** Minimal edit reports omitting echoed input text and duplicate `clean_text` previews in minimal mode (≤40 tokens/applied edit).
**Status.** Completed & Verified.
**Attempt Ledger:**
- attempt 1: Added payloads.py shrink_batch_stats, cli --report flag, MCP default minimal -> VERDICT: FAIL
- attempt 2: Omitted empty heading_path/pages and clamped critic_markup to 35 chars in payloads.py, changed cli.py to check key presence for "new_text" in report, updated test_report_minimal.py to call product code and check real engine output -> VERDICT: FAIL
- attempt 3 (@opus-coder): Rebuilt shrink_batch_stats in payloads.py to drop all 4 echoes including comment, added balanced CriticMarkup clamp (never slicing inside bubbles), fit budget via fit_to_budget using real JSON token estimation, added clamp_text in utils/text.py, fixed CLI standard mode key presence check, restored test_repro_feedback_layer.py assertion -> VERDICT: FAIL
- attempt 4 (@opus-coder): Added `_bubble_segments` to collapse inter-bubble gap contexts to short ASCII `...` elision markers, drop trailing bubbles with `(+N more spans)` when needed, and drop `pages` if needed under budget pressure. Added `test_minimal_report_token_budget_multi_occurrence` test. -> VERDICT: FAIL
- attempt 5 (@opus-coder): Removed `warning` from `_UNBUDGETED_FIELDS` for applied edits, added geometric clamping for `warning`, added fallback `critic_markup` drop in `_fit_to_budget`, added test for regex replacement warning. -> VERDICT: FAIL (untracked repro script left)
- attempt 6 (@coder): Deleted untracked scratch file `python/repro_warning_budget.py`. -> VERDICT: FAIL (duplicated regex in payloads.py)
- attempt 7 (@coder): Replaced `_CRITIC_BUBBLE_RE` in `payloads.py` with `CRITICMARKUP_BLOCK_RE` imported from `adeu.diff`. -> VERDICT: FAIL (plain-text fallback on engine-truncated preview string left open bubble)
- attempt 8 (@coder): Added `_has_orphaned_critic_delimiters` check to drop `critic_markup` when engine-truncated preview string contains broken/incomplete CriticMarkup delimiters, added `test_minimal_report_large_echo_cap_modify_text` test. -> VERDICT: PASS
**Files.**
- `python/src/adeu/payloads.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_report_minimal.py` (new)
**Test first.**
1. `test_minimal_report_drops_echoes`: minimal report omits `target_text`, `new_text`, `clean_text`.
2. `test_minimal_report_keeps_verification_fields`: retains `{status, type, pages, heading_path, occurrences_modified, critic_markup}`.
3. `test_minimal_report_keeps_match_mode_only_when_non_strict`: `match_mode` present only when non-strict.
4. `test_failed_edit_keeps_full_error_and_stub_target`: failing edit retains full error + ≤80-char target stub.
5. `test_minimal_report_token_budget`: `approx_tokens(json.dumps(e)) <= 40`.
6. `test_standard_report_is_unchanged`: standard report matches today's shape.
7. `test_batch_level_keeps_version_drops_engine`: stats keeps `version`, drops `engine`.
8. `test_skipped_details_deduped_against_edit_errors`: deduplicate batch skipped details against edit errors.
9. `test_mcp_default_is_minimal`: MCP batch response omits duplicate clean preview.
**Change.**
- `payloads.py`: `def shrink_batch_stats(stats: dict) -> dict`.
- `cli.py`: add `--report minimal|standard` to `p_apply` (default `standard`).
- `document.py`: default MCP batch responses to minimal report format.
**Done when.** `uv run pytest tests/test_report_minimal.py` passes (9 tests).

### Task 5 — Item B2: Failure Payloads Teach Split-Recovery Protocol (COMPLETED)
**Goal.** Append two-call split recovery protocol instruction to all batch failures.
**Status.** Completed & Verified (commits `49932c8`, `468cd15`, `d956962`, `7efc221`).
**Files.**
- `python/src/adeu/payloads.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_failure_recovery_protocol.py` (new)
**Test first.**
1. `test_cli_batch_failure_carries_protocol`: stderr contains split recovery guidance.
2. `test_cli_schema_failure_carries_protocol`: schema failure carries guidance.
3. `test_mcp_batch_failure_carries_protocol`: MCP failure string contains identical guidance.
4. `test_envelope_message_carries_protocol`: envelope `message` ends with guidance.
5. `test_failure_payload_size_budget`: 20-edit batch / 1 bad edit failure output ≤ 500 tokens.
6. `test_sequential_state_hint_preserved`: sequential state hint retained.
**Change.**
- `payloads.py`: define `BATCH_RECOVERY_PROTOCOL`.
- `cli.py` & `document.py`: append `BATCH_RECOVERY_PROTOCOL` to batch failure outputs.
**Done when.** `uv run pytest tests/test_failure_recovery_protocol.py` passes (6 tests).

---

## Phase 2 (P1 Items) — Tasks 6 to 11

### Task 6 — Item A2: Search Flags (`--max-matches`, `--match-offset`, Snippet Clamping) (COMPLETED)
**Goal.** Paged search results with snippets clamped to ±120 chars around hits.
**Status.** Completed & Verified (commits `6dfb8d8`, `734a488`, `d51d1a5`, `34f0e54`, `021580e`).
**Files.**
- `python/src/adeu/mcp_components/_response_builders.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_search_paging.py` (new)
**Test first.**
1. `test_default_cap_is_twenty_and_reports_total`: header states 50 total, 20 shown.
2. `test_max_matches_respected`: `max_matches=5` returns 5 hits.
3. `test_match_offset_pages_without_overlap`: offset pagination yields non-overlapping pages.
4. `test_offset_past_end_is_not_an_error`: returns "no matches in this window" note.
5. `test_snippet_clamped_to_120_chars_each_side`: snippet clamped to ±120 chars around hit.
6. `test_full_paragraph_opt_out`: `--full-paragraph` returns full paragraph.
7. `test_clamped_snippet_never_leaves_markup_unterminated`: CriticMarkup bubbles balanced.
8. `test_continue_note_names_next_offset`: tail note names `--match-offset 20` / `match_offset=20`.
9. `test_search_token_budget`: `approx_tokens(default response) <= 20 * 60`.
10. `test_cli_search_flags`: `adeu extract <f> --search-query Supplier --max-matches 3` prints 3 entries.
**Change.**
- Update `build_search_response` signature with `max_matches=20`, `match_offset=0`, `full_paragraph=False`.
- Clamp snippet windows using `_balance_snippet_window`. Recompute highlight offsets.
- Wire `--max-matches`, `--match-offset`, `--full-paragraph` in `cli.py` and `document.py`.
**Done when.** `uv run pytest tests/test_search_paging.py` passes (10 tests).

### Task 7 — Item A3: Whole-Document Response Budget Guard (COMPLETED)
**Goal.** Refuse unbounded `page="all"` reads > 76k chars with a ≤800-token recipe and outline.
**Status.** Completed & Verified (commit `7eeb483`).
**Attempt Ledger:**
- attempt 1: Implemented response budget guard in payloads.py, cli.py, document.py, live_word.py. -> VERDICT: FAIL
- attempt 2: Exempted file sink -o output in cli.py, passed page_count in CLI and live_word, suppressed duplicate prose to stderr in --json mode. -> VERDICT: FAIL
- attempt 3 (@opus-coder): Measured budget on emitted --json envelope, valid CLI flags in recipe, scoped stderr suppression. -> VERDICT: FAIL
- attempt 4 (@opus-coder): Fixed empty no-op edit bug in diff.py _split_cross_paragraph_hunks. -> VERDICT: PASS
**Files.**
- `python/src/adeu/payloads.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_response_budget_guard.py` (new)
**Test first.**
1. `test_guard_fires_on_oversize_all_pages`: `--page all` on big doc exits non-zero with recipe.
2. `test_guard_output_is_under_800_tokens`: `approx_tokens(output) <= 800`.
3. `test_guard_includes_outline`: guard output contains L1 headings outline.
4. `test_small_document_is_unaffected`: 1-page doc returns full body.
5. `test_force_overrides`: `--page all --force` overrides guard.
6. `test_env_var_overrides_threshold`: `ADEU_MAX_RESPONSE_CHARS` sets custom limit.
7. `test_guard_does_not_fire_for_search_or_ranges`: search and page range requests exempt.
8. `test_mcp_guard_fires`: `read_docx(page="all")` on big doc raises `ToolError` with recipe.
9. `test_guard_never_fires_when_paginating_normally`: `--page 3` unaffected.
**Change.**
- `payloads.py`: implement `whole_doc_guard_message()` and `response_budget_limit()`.
- `cli.py`: trigger guard on unbounded `full` mode extract over threshold unless `--force`.
- `document.py`: trigger guard in `_read_docx_disk` when `page == "all"`.
**Done when.** `uv run pytest tests/test_response_budget_guard.py` passes (9 tests).

### Task 8 — Item E1: `reasoning` Parameter Becomes Optional (COMPLETED)
**Goal.** Make `reasoning` optional across all MCP tools without losing reason-first description.
**Status.** Completed & Verified.
**Files.** `python/src/adeu/mcp_components/tools/document.py`, `sanitize.py`.
**Test first.** `python/tests/test_mcp_reasoning_optional.py` (new):
1. `test_reasoning_not_in_required`: `"reasoning"` not in tool schema `required[]`.
2. `test_reasoning_still_advertised`: `"reasoning"` remains in `properties`.
3. `test_call_without_reasoning_succeeds`: calling tool without `reasoning` succeeds.
4. `test_call_with_reasoning_still_succeeds`: calling with `reasoning` succeeds.
5. `test_no_positional_callers_remain`: static check verifying keyword invocations.
**Change.** Set `reasoning: Optional[str] = ""` and move `reasoning` to the end of parameter list across all MCP tools.
**Done when.** `uv run pytest tests/test_mcp_reasoning_optional.py` passes (5 tests).

### Task 9 — Item B6: `ensure_ascii=False` for Agent-Facing JSON (COMPLETED)
**Goal.** Output literal UTF-8 in JSON outputs instead of `\u2019` escapes.
**Status.** Completed & Verified (commit `e5f66c4`).
**Failed Verify Cycles:** 2 (Escalated to @opus-coder: trigger (a) second failed cycle, trigger (b) same finding in two verdicts)
**Attempt Ledger:**
- attempt 1: Passed ensure_ascii=False to json.dumps in cli.py, payloads.py, created test_json_unicode.py -> VERDICT: FAIL (test_error_envelope_preserves_unicode didn't fail on pre-change code; stale comments in payloads.py)
- attempt 2: Updated test_error_envelope_preserves_unicode to target adeu markup --json error envelope output; updated comments in payloads.py -> VERDICT: FAIL (test_error_envelope_preserves_unicode still passed on pre-change code because all CLI error envelope emitters were already ensure_ascii=False at base)
- attempt 3 (@opus-coder): Replaced test_error_envelope_preserves_unicode with test_markup_json_success_preserves_unicode targeting adeu markup ... -o - --json success output (cli.py:1618), where ensure_ascii=False was newly enabled by Task 9. Verified all 5 tests fail when reverted to 6b01a1c~1 base -> VERDICT: PASS
**Files.** `python/src/adeu/cli.py`, `python/src/adeu/mcp_components/tools/document.py`, `python/src/adeu/redline/engine.py`.
**Test first.** `python/tests/test_json_unicode.py` (new):
1. `test_extract_json_preserves_unicode`: literal smart quotes and dashes in JSON output.
2. `test_apply_stats_json_preserves_unicode`: curly quotes preserved in apply stats JSON.
3. `test_markup_json_success_preserves_unicode`: unicode preserved in the markup success envelope's embedded `content`.
4. `test_no_escaped_sequences_anywhere`: `"\\u"` absent in stdout across subcommands.
5. `test_output_is_utf8_decodable`: stdout decodes as valid UTF-8.
**Change.** Pass `ensure_ascii=False` to all `json.dumps()` calls.
**Done when.** `uv run pytest tests/test_json_unicode.py` passes (5 tests).

### Task 10 — Item B7: Name the Fused-JSON Failure Mode (COMPLETED)
**Goal.** Append specific fused-JSON hint when model serializes JSON object into `type` field.
**Status.** Completed & Verified.
**Files.** `python/src/adeu/payloads.py`, `python/src/adeu/cli.py`, `python/src/adeu/mcp_components/tools/document.py`.
**Test first.** `python/tests/test_fused_json_hint.py` (new):
1. `test_cli_fused_tag_gets_specific_hint`: fused JSON element receives fused hint on CLI.
2. `test_mcp_fused_tag_gets_specific_hint`: fused JSON element receives fused hint on MCP.
3. `test_ordinary_bad_tag_is_unchanged`: ordinary typos (`modfy`) retain standard error.
4. `test_hint_triggers_on_each_marker`: triggers when `type` contains `{`, `}`, or `":`.
5. `test_hint_does_not_trigger_on_punctuation_only`: single `:` does not trigger hint.
**Change.** Define `FUSED_JSON_HINT` in `payloads.py`. Append to validation error messages when `type` contains `{`, `}`, or `":`.
**Done when.** `uv run pytest tests/test_fused_json_hint.py` passes (5 tests).

### Task 11 — Item C1: Actionable Multi-Author Guard Message (COMPLETED)
**Goal.** Replace vague guard refusal with concrete next steps (`{"type": "accept", "target_id": "Chg:N"}` or `match_mode` `"strict"`/`"first"`).
**Status.** Completed & Verified (commit `095c42d`).
**Failed Verify Cycles:** 6
**Attempt Ledger:**
- attempt 1: Updated guard refusal message in engine.py with accept action JSON and match_mode options, created test_guard_message.py -> VERDICT: FAIL (when match_mode="strict", message re-recommends strict/first; tests 3, 4, 5 passed pre-change code)
- attempt 2: Tailored guard refusal advice based on edit.match_mode; sharpened test_guard_message.py -> VERDICT: FAIL (missing test_guard_still_names_author_and_ids test in test_guard_message.py required by plan)
- attempt 3 (@opus-coder): Added test_guard_still_names_author_and_ids to test_guard_message.py -> VERDICT: FAIL (3 foreign authors / 6 IDs produces 82 approx tokens, exceeding 70-token budget; test name mismatch)
- attempt 4 (@opus-coder): Bounded author hints in engine.py to first author + max 2 IDs + (+N more) counter -> VERDICT: FAIL (54+ character author name produces 71+ approx tokens, exceeding 70-token budget)
- attempt 5 (@opus-coder): Truncated author names in engine.py dynamically based on remaining budget -> VERDICT: FAIL (re-implemented inline author truncation instead of using clamp_text from adeu.utils.text)
- attempt 6 (@opus-coder): Used clamp_text from adeu.utils.text -> VERDICT: FAIL (abnormally long w:id breaks token budget; straddling match_mode=all edit recommends strict which also fails; test_guard_message.py uses inline len//4 instead of approx_tokens)
- attempt 7 (@opus-coder): Gated strict/first advice on match_mode == "all" and fully_within_foreign_ins; applied clamp_text(msg, GUARD_MESSAGE_CAP); used approx_tokens in test_guard_message.py and added test cases -> VERDICT: PASS
**Files.** `python/src/adeu/redline/engine.py:2283-2286`.
**Test first.** `python/tests/test_guard_message.py` (new):
1. `test_guard_names_the_accept_action`: error includes copy-pasteable accept action JSON.
2. `test_guard_names_the_narrowing_alternative`: error names `strict`/`first` match_mode.
3. `test_guard_still_names_author_and_ids`: author name and change IDs preserved.
4. `test_guard_message_token_budget`: `approx_tokens(message) <= 70`.
5. `test_strict_edit_inside_foreign_insertion_still_allowed`: strict edit wholly inside foreign insertion succeeds.
**Change.** Rewrite guard refusal message in `engine.py:2283-2286` to list exact resolution options.
**Done when.** `uv run pytest tests/test_guard_message.py` passes (5 tests).

---

## Phase 3 (P2 Items) — Tasks 12 to 16

### Task 12 — Item B5: Explicit Salvage Contract (`--partial` / `--atomic`) (COMPLETED)
**Goal.** Apply valid edits when some fail and lead response with `PARTIAL: applied K of N...`. Default CLI to `--atomic`, MCP to partial.
**Status.** Completed & Verified.
**Failed Verify Cycles:** 0
**Attempt Ledger:**
- attempt 1: Implemented explicit salvage contract with `partial: bool` parameter in `RedlineEngine.process_batch`, `--partial`/`--atomic` CLI flags, MCP default `partial=True`, and 7 unit tests in `test_explicit_salvage.py` -> VERDICT: PASS
**Files.**
- `python/src/adeu/redline/engine.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_explicit_salvage.py` (new)
**Test first.**
1. `test_cli_partial_lands_valid_edits_and_leads_with_failures`: `--partial` lands valid edits and lists failures on stderr.
2. `test_cli_default_is_still_atomic`: default CLI apply is atomic (nothing written on failure).
3. `test_partial_json_failed_indices_are_machine_readable`: stats contains `"status": "partial"` and `failed` array.
4. `test_partial_rejected_for_text_file_input`: text-file apply rejects `--partial` with exit 2.
5. `test_pairing_contradiction_still_rejects_whole_batch_under_partial`: contradictory actions reject whole batch.
6. `test_mcp_defaults_to_partial_and_reports_schema_rejects_in_one_list`: MCP defaults to partial apply.
7. `test_partial_failure_payload_within_token_budget`: failure header ≤ 60 tokens; 20-edit/1-bad failure block ≤ 500 tokens.
**Change.**
- Add `partial: bool = False` to `RedlineEngine.process_batch`. Record input batch positions.
- Add `--partial` and `--atomic` flags to `adeu apply`.
- Default MCP `process_document_batch` to partial apply. Prepend `PARTIAL: applied K of N...` header on partial success.
**Done when.** `uv run pytest tests/test_explicit_salvage.py` passes (7 tests).

### Task 13 — Item A4: `--no-chrome` Extract Flag
**Goal.** Strip navigation prose, banners, footers, and appendix pointers from extract output.
**Failed Verify Cycles:** 3 (opus-coder cycle 1)
**Attempt Ledger:**
- attempt 1: Added no_chrome parameter across response builders and --no-chrome CLI flag, created test_no_chrome.py -> VERDICT: FAIL (File Path header and navigation prose still emitted under no_chrome in search zero matches, page filter with no hits, window offset past total, and deep outline level)
- attempt 2: Suppressed File Path headers and navigation prose in build_search_response and render_outline_tree -> VERDICT: FAIL (build_changes_response with no_chrome=True on zero changes/comments returned empty string "")
- attempt 3 (@opus-coder): Emitted bare summary line in build_changes_response when no_chrome=True -> VERDICT: FAIL (regex_downgraded_note was dropped under no_chrome in compose() when hits existed)
**Files.**
- `python/src/adeu/mcp_components/_response_builders.py`
- `python/src/adeu/cli.py`
- `python/tests/test_no_chrome.py` (new)
**Test first.**
1. `test_no_chrome_drops_file_path_header_and_prose`: `--no-chrome` suppresses `**File Path:**` and banners.
2. `test_no_chrome_page_content_is_byte_identical_apart_from_chrome`: non-chrome body text identical.
3. `test_no_chrome_keeps_bare_page_marker_on_multipage`: multipage outputs keep bare `[pN/M]` marker.
4. `test_no_chrome_saves_tokens`: saves ≥ 20 tokens per page.
5. `test_no_chrome_composes_with_json`: composes with `--json`.
**Change.** Add `no_chrome: bool = False` to response builders and `--no-chrome` flag to `adeu extract`.
**Done when.** `uv run pytest tests/test_no_chrome.py` passes (5 tests).

### Task 14 — Item A5: Compact `diff --json` (COMPLETED)
**Goal.** Reduce `diff --json` size by 25%+ by removing indentation, default fields, and generator `Diff:` comments.
**Status.** Completed & Verified (commit `62f61e3`).
**Failed Verify Cycles:** 1
**Attempt Ledger:**
- attempt 1: Used exclude_defaults=True, separators=(",",":"), and stripped Diff: boilerplate comments in cli.py, created test_compact_diff_json.py -> VERDICT: FAIL (tests 3 and 4 in test_compact_diff_json.py passed against pre-change code)
- attempt 2: Sharpened tests in test_compact_diff_json.py to assert single-line output, omission of default fields, and absence of Diff: boilerplate comments -> VERDICT: PASS
**Files.** `python/src/adeu/cli.py:970-977`.
**Test first.** `python/tests/test_compact_diff_json.py` (new):
1. `test_diff_json_is_unindented_and_omits_defaults`: unindented JSON without default fields or `Diff:` comments.
2. `test_diff_json_at_least_25_percent_smaller`: JSON size ≤ 75% of indented version.
3. `test_diff_json_round_trips_through_apply`: compact JSON round-trips through `adeu apply`.
4. `test_diff_json_preserves_a_meaningful_comment`: user comments preserved.
**Change.** Dump models with `exclude_defaults=True`, separators `(",", ":")`, and strip `Diff: ` boilerplate comments.
**Done when.** `uv run pytest tests/test_compact_diff_json.py` passes (4 tests).

### Task 15 — Item C3: MCP `apply_text_revision` Tool (COMPLETED)
**Goal.** Expose whole-text diff->tracked-changes apply primitive over MCP with clean-text verification gate.
**Status.** Completed & Verified (commit `cebb7fc`).
**Failed Verify Cycles:** 3 (opus-coder cycle 2)
**Attempt Ledger:**
- attempt 1: Extracted text_revision module and added apply_text_revision MCP tool -> VERDICT: FAIL (flat 30% deletion threshold broke existing small doc deletion tests; CLI error message lost --allow-major-deletions flag hint; CLI json/human verification failure/success outputs regressed)
- attempt 2: Restored character deletion threshold in text_revision.py and CLI failure/success outputs -> VERDICT: FAIL (unneeded >50 paragraphs guard refused 30% char deletion; MCP schema text still said >30%; CLI text apply lost progress line, overwrite warning, BatchValidationError catch, and full refusal message)
- attempt 3 (@opus-coder): Removed paragraph deletion guard; restored full refusal message; routed CLI text apply back through shared apply path -> VERDICT: FAIL (CRITICMARKUP_TOKENS widened to closing tokens like ~> causing false positive CriticMarkup refusals on plain text containing ~>)
- attempt 4 (@opus-coder): Restricted _CRITICMARKUP_TOKENS in text_revision.py to open tags only: ("{++", "{--", "{~~", "{==", "{>>"); added unit and end-to-end tests in test_mcp_apply_text_revision.py -> VERDICT: PASS
**Files.**
- `python/src/adeu/text_revision.py` (new)
- `python/src/adeu/cli.py`
- `python/src/adeu/mcp_components/tools/document.py`
- `python/tests/test_mcp_apply_text_revision.py` (new)
**Test first.**
1. `test_apply_text_revision_produces_tracked_changes`: text revision produces expected tracked changes.
2. `test_apply_text_revision_refuses_major_deletion_without_flag`: major deletion refused unless `allow_major_deletions=True`.
3. `test_apply_text_revision_refuses_criticmarkup_input`: refuses input containing CriticMarkup.
4. `test_apply_text_revision_verification_failure_writes_unverified_sibling`: verification failure writes `.unverified.docx` sibling and does not touch requested output path.
5. `test_cli_text_apply_still_behaves_identically`: CLI text apply behavior unchanged.
**Change.** Extract shared text revision helpers into `text_revision.py`. Register `apply_text_revision` tool on MCP.
**Done when.** `uv run pytest tests/test_mcp_apply_text_revision.py` passes (5 tests).

### Task 16 — Items E3 & E4: Shared Missing-File Suggestions & Discovery Hints (COMPLETED)
**Goal.** Offer sibling file suggestions on CLI missing-file errors and point ID discovery hints at `--mode changes`.
**Status.** Completed & Verified (commit `d394d43`).
**Failed Verify Cycles:** 2
**Attempt Ledger:**
- attempt 1: Added suggest_sibling_docx to utils/docx.py, updated CLI missing file handler and ID hints -> VERDICT: FAIL (suggest_sibling_docx in utils/docx.py duplicated sibling matching in mcp_components/shared.py)
- attempt 2: Made suggest_sibling_docx in utils/docx.py single source of truth accepting limit and path -> VERDICT: FAIL (_not_found_error lost the (+N more in <dir>) suffix when total siblings exceeded cap)
- attempt 3 (@opus-coder): Updated suggest_sibling_docx to return tuple[list[str], int]; updated shared.py _not_found_error to append (+N more in <dir>) suffix when total exceeds cap -> VERDICT: PASS
**Files.**
- `python/src/adeu/utils/docx.py`
- `python/src/adeu/mcp_components/shared.py`
- `python/src/adeu/cli.py`
- `python/src/adeu/redline/engine.py`
- `python/tests/test_file_hints_and_id_discovery.py` (new)
**Test first.**
1. `test_cli_missing_file_suggests_siblings_and_drops_the_sandbox_essay`: missing file lists sibling candidates and drops sandbox warning.
2. `test_cli_missing_file_json_mode_still_emits_the_error_contract`: `--json` preserves `file_not_found` code.
3. `test_cli_stale_id_error_names_the_changes_ledger`: stale ID error names `--mode changes`.
4. `test_mcp_hint_names_the_changes_ledger_and_never_the_cli`: MCP hint names `read_docx` with `mode='changes'`.
**Change.** Add `suggest_sibling_docx()` to `utils/docx.py`. Update CLI missing file handler and ID discovery hints.
**Done when.** `uv run pytest tests/test_file_hints_and_id_discovery.py` passes (4 tests).

---

## Phase 4 (P3 Items) — Tasks 17 to 21

### Task 17 — Item C2: Gate Author-Name Bypass (COMPLETED)
**Goal.** Emit warning when acting author matches an author with pending revisions in the document.
**Status.** Completed & Verified.
**Files.** `python/src/adeu/redline/engine.py`, `python/src/adeu/cli.py`, `python/src/adeu/mcp_components/tools/document.py`.
**Test first.** `python/tests/test_author_impersonation_gate.py` (new):
1. `test_warning_when_acting_author_impersonates_a_pending_author`: warning emitted on author name match.
2. `test_no_warning_for_a_distinct_author`: no warning for distinct author name.
3. `test_no_warning_on_a_clean_document`: no warning on clean document without pending changes.
4. `test_cli_surfaces_the_warning`: warning displayed on CLI stderr and in stats JSON.
**Change.** Add `author_impersonation_warning` property to `RedlineEngine`. Include in batch stats and CLI/MCP response summaries.
**Done when.** `uv run pytest tests/test_author_impersonation_gate.py` passes (4 tests).

### Task 18 — Item B8: Error-Size Budget Knobs (`--terse-errors`)
**Goal.** Optional flag to reduce ambiguity examples (2 max, ±25 chars context) and listed stale IDs (8 max).
**Files.** `python/src/adeu/markup.py`, `python/src/adeu/redline/engine.py`, `python/src/adeu/cli.py`.
**Test first.** `python/tests/test_terse_errors.py` (new):
1. `test_terse_ambiguity_error_is_much_smaller`: terse ambiguity error size ≤ 150 tokens.
2. `test_full_ambiguity_error_is_unchanged_by_default`: default ambiguity error format unchanged.
3. `test_terse_stale_id_error_lists_at_most_eight_ids`: terse stale ID error lists max 8 IDs.
**Change.** Add `terse` parameter to `format_ambiguity_error` and `terse_errors` to `RedlineEngine`. Add `--terse-errors` to `adeu apply`.
**Done when.** `uv run pytest tests/test_terse_errors.py` passes (3 tests).

### Task 19 — Item D3: Env-Tunable Doc-Cache LRU
**Goal.** Allow configuring `doc_cache` capacity via `ADEU_DOC_CACHE_ENTRIES`.
**Files.** `python/src/adeu/mcp_components/doc_cache.py`.
**Test first.** Append to `python/tests/test_doc_cache.py`:
1. `test_lru_size_is_env_tunable`: `ADEU_DOC_CACHE_ENTRIES="7"` sets cache capacity to 7.
2. `test_lru_size_falls_back_on_garbage_and_zero`: invalid values fall back to default (3).
**Change.** Read `ADEU_DOC_CACHE_ENTRIES` in `doc_cache.py`.
**Done when.** `uv run pytest tests/test_doc_cache.py` passes.

### Task 20 — Item D1: On-Disk Projection Cache for CLI
**Goal.** Skip DOCX parsing and Virtual Text projection on repeated CLI reads of unchanged files.
**Files.** `python/src/adeu/disk_cache.py` (new), `python/src/adeu/cli.py`.
**Test first.** `python/tests/test_disk_projection_cache.py` (new):
1. `test_second_read_is_byte_identical_and_hits_the_cache`: cached read produces byte-identical output.
2. `test_outline_mode_is_byte_identical_from_cache`: outline mode works from cache.
3. `test_mtime_change_invalidates`: modifying file invalidates cache.
4. `test_disable_switch_and_unwritable_dir_are_non_fatal`: `ADEU_NO_CACHE=1` and unwritable cache dir handle gracefully.
5. `test_corrupt_cache_entry_is_ignored`: corrupt cache file ignored and regenerated.
**Change.** Implement `disk_cache.py` using JSON storage keyed by path stat triple + `__version__`. Integrate into `cli.py` extract flow.
**Done when.** `uv run pytest tests/test_disk_projection_cache.py` passes (5 tests).

### Task 21 — Item D2: `adeu serve` (JSON-Lines Daemon)
**Goal.** Keep single process alive with warm `doc_cache` for high-volume stdin/stdout JSON-lines callers.
**Files.** `python/src/adeu/serve.py` (new), `python/src/adeu/cli.py`.
**Test first.** `python/tests/test_serve_daemon.py` (new):
1. `test_ping_and_extract_over_one_session`: ping and extract commands over single daemon session.
2. `test_malformed_line_does_not_kill_the_daemon`: malformed JSON line returns error and keeps serving.
3. `test_unknown_command_and_missing_file_use_the_cli_error_codes`: returns standard CLI error codes.
4. `test_serve_output_matches_one_shot_cli`: daemon output matches one-shot `adeu extract --json`.
5. `test_apply_over_serve_writes_the_document`: daemon apply writes document successfully.
**Change.** Implement `run_serve()` in `serve.py` and register `serve` subparser in `cli.py`.
**Done when.** `uv run pytest tests/test_serve_daemon.py` passes (5 tests).

---

## Final Verification Checklist
1. `uv run ruff check . && uv run ruff format --check .` -> zero errors.
2. `uv run mypy src` -> zero errors.
3. `uv run pytest` -> all tests pass across all newly created test files.
4. Document updated behavior in `docs/PERFORMANCE.md` and `docs/TODO.md`.

PLAN COMPLETE
