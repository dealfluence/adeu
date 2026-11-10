# Monorepo Parity, Integration & Prompt Alignment Plan (Post v3.0.7 → v3.2.0)

## Context

Between `v3.0.7` and `v3.2.0`, the core Python (`adeu`) and TypeScript (`@adeu/core`) engines and MCP servers added major capabilities:
1. **Multi-comment support (`ModifyText.comment`):** accepts either a string or a list of `{text: string, author?: string}` entries to attach multiple comments (with per-comment authors) to a single edit.
2. **Manual / Inline Formatting Markup Mode (`markup_mode="manual"`):** renders deletions as strikethrough (`<w:strike/>`) and insertions as bold + underlined `#C00000` red runs with native Word comments and zero `<w:ins>`/`<w:del>` revisions.
3. **One-step DOCX Redline Comparison (`build_redline_comparison` / `diff_docx_files(output_path=...)`):** compares `original` and `modified` `.docx` packages transactionally, repairs OOXML schema child order, and writes a redlined `.docx` in one step.
4. **Markdown Pipe Tables in `ModifyText.new_text`:** multiline `new_text` containing a Markdown pipe table block (`| Col 1 | Col 2 |\n| --- | --- |\n| A | B |`) constructs a native `<w:tbl>` at the paragraph split point; `{#cell:<paraId>}` anchors target empty table cells.
5. **Plain-Text Anchoring Across Structural Markers:** plain-text `target_text` resolves across hyperlinks `[text](url)`, cross-references `[~text~](#ref)`, and bookmarks `{#bookmark}` and preserves them when kept unchanged.
6. **Enriched Batch Receipts & Derivative In-Place Resolution:** per-edit `index`, minted `change_ids` (`Chg:N`), `comment_ids` (`Com:N`), `part` disclosure outside the main body, `actions` receipts, `document_state` (`remaining_tracked_changes`, `remaining_comments`), and automatic in-place output when the input stem ends in `_processed`, `_redlined`, `.redline`, `-redline`, `_markup`, or `.marked`.
7. **`read_docx` Ergonomics & Bounded `page="all"`:** search mode accepts page ranges (`page="4-5"`) and strips quotes around page strings; `read_docx(page="all")` over the response budget serves the first pages that fit (`build_bounded_full_response`) with an L1 outline and continuation hint instead of failing.
8. **Form Fields (`mode="fields"`, `set_field`) & Write-Gate Overrides (`ignore_control_locks`, `ignore_document_protection`, `allow_untracked_writes`):** full support across engines, with `part` disambiguation on `accept`/`reject`.

Several wrapper packages (`langchain-adeu`, `n8n-nodes-adeu`), daemon/live-word paths (`serve.py`, `live_word.py`), MCP schema/tool descriptions, skills (`skills/adeu-redlining/`), `GEMINI.md`, and READMEs have not yet been updated to expose or document all of these features.

## Assumptions

- The CLI (`adeu extract`) intentionally keeps its existing `--search-query` page-range rejection (`test_cli_range_rejected_with_search_query` in `python/tests/test_page_ranges.py`) and its over-budget `page="all"` refusal (`build_budget_guard_message`), whereas agent-facing tool surfaces (`read_docx` in MCP / Live Word / LangChain / `serve.py`) accept search page ranges and (on MCP & LangChain `read_docx`) return bounded first-pages responses when `page="all"` exceeds the response budget without `force=True`.
- `nodes/Adeu/Adeu.node.json` `nodeVersion` (`"1.0"`) and `codexVersion` (`"1.0"`) remain fixed per `AGENTS.md` and `scripts/check_release_consistency.mjs`.
- Each task will be verified with its package test/lint/typecheck suite and committed locally on `main` (no push).

---

## Tasks

### Task 1: Python MCP Schema, `serve.py`, `live_word.py`, and Tool Description Parity (`[x] Completed`)
- **Goal:** Ensure Python MCP schemas, `adeu serve`, Live Word search, and Python MCP tool descriptions expose all v3.2.0 parameters and capabilities.
- **Files:**
  - `python/src/adeu/models.py`
  - `python/src/adeu/mcp_components/tools/document.py`
  - `python/src/adeu/mcp_components/tools/live_word.py`
  - `python/src/adeu/serve.py`
  - `python/tests/test_mcp_schema_and_serve_parity.py` (or existing test files `python/tests/test_serve.py`, `python/tests/test_models.py`)
- **Change:**
  1. Add `part: Optional[str]` to `FlatDocumentChange` in `python/src/adeu/models.py` so `FlatSchemaDocumentChange` publishes `part` on `accept`/`reject` in the Python MCP JSON schema (matching `AcceptChange`/`RejectChange` and Node's `CHANGE_ITEM_SCHEMA`).
  2. Update `ModifyText.new_text` description in `python/src/adeu/models.py` and `PROCESS_BATCH_OPERATIONS_DESC` / `READ_DOCX_TAIL` in `python/src/adeu/mcp_components/tools/document.py` to document `mode='changes'`, `mode='fields'`, page ranges (`"1-5"`, search `"4-5"`), bounded `page='all'`, `match_mode`, `regex`, `{#cell:<id>}` empty-cell anchors, Markdown pipe tables in `new_text`, and multi-comment lists `[{"text": ..., "author": ...}]`.
  3. In `python/src/adeu/serve.py`:
     - Support `markup_mode = req.get("markup_mode", "track_changes")` in `_handle_apply_command` and pass `markup_mode=markup_mode` to `_open_redline_engine_or_exit`.
     - Pre-parse page ranges (`"4-5"`) and strip surrounding quotes on `page_arg` in search mode before calling `build_search_response`.
  4. In `python/src/adeu/mcp_components/tools/live_word.py`:
     - Pre-parse page ranges (`"4-5"`) and strip surrounding quotes on `page` in search mode before calling `build_search_response`, matching `document.py:424-433`.
- **Verify:**
  - `cd python && uv run pytest tests/test_serve.py tests/test_mcp_tool_descriptions.py` (plus new tests)
  - `cd python && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
- **Done when:** Python MCP `process_document_batch` input schema includes `part`, `adeu serve` `apply` honors `markup_mode: "manual"` and `extract` search mode accepts page ranges `"4-5"`, Live Word search mode accepts page ranges, and Python MCP tool descriptions match Node MCP capabilities.

---

### Task 2: Node MCP Server Prompt & Description Parity (`[x] Completed`)
- **Goal:** Bring `@adeu/mcp-server` tool and schema descriptions in `index.ts` into full alignment with v3.2.0 behavior.
- **Files:**
  - `node/packages/mcp-server/src/index.ts`
  - `node/packages/mcp-server/src/budget-guard.test.ts` (or `spec_2026_07_28.test.ts`)
- **Change:**
  1. Update `READ_DOCX_TAIL` in `node/packages/mcp-server/src/index.ts:312-313` so `page='all'` describes the v3.2.0 bounded first-pages response (`build_bounded_full_response`) rather than saying oversized documents are refused, and note that `page` with `search_query` accepts single pages or `"N-M"` page ranges.
  2. Update `PROCESS_BATCH_OPERATIONS_DESC` and `CHANGE_ITEM_SCHEMA` descriptions in `node/packages/mcp-server/src/index.ts:324-332, 879-915` to document Markdown pipe tables in `new_text` (`| Col 1 | Col 2 |\n|---|---|\n| A | B |`) and multi-author comment lists (`[{"text": "...", "author": "..."}]`).
- **Verify:**
  - `cd node && npm run build && npm test && npm run lint`
- **Done when:** `@adeu/mcp-server` builds (updating `desktop-extension/index.js`), passes all tests and lint, and its tool descriptions accurately reflect bounded `page='all'`, search page ranges, pipe tables in `new_text`, and multi-comment lists.

---

### Task 3: LangChain `AdeuReadDocx` Parity (`[x] Completed`)
- **Goal:** Bring `AdeuReadDocx` in `langchain-adeu` to 1:1 behavioral and schema parity with MCP `read_docx`.
- **Files:**
  - `langchain/langchain_adeu/read_docx.py`
  - `langchain/tests/unit_tests/test_read_docx.py`
  - `langchain/tests/integration_tests/test_read_docx.py`
- **Change:**
  1. **Bounded `page="all"` over budget:** In `AdeuReadDocx._run` (`langchain/langchain_adeu/read_docx.py:341-346`), when `kind == "all"` and `not force and len(text) > response_budget_limit()`, return `build_bounded_full_response(text, str(path), paginate(split_structural_appendix(text)[0], ""), outline_nodes=...)` as a successful `(content, artifact)` tuple instead of raising `ToolException(build_budget_guard_message(...))`. Update `force` field description and `_DESCRIPTION` accordingly.
  2. **Search mode page ranges & quote stripping:** In `AdeuReadDocx._run` (`read_docx.py:284-295`), pre-parse `page` when `search_query` is set so `"4-5"` (and quoted `"'4-5'"` / `"'2'"`) resolves via `parse_page_arg` into a `(start, end)` tuple or int before calling `build_search_response`, matching `document.py:424-433`.
  3. **`fields_banner` on `mode="full"` reads:** Pass `fields_banner=banner_for_path(str(path), doc)` to `build_full_document_response` and `build_paginated_response` so documents with Content Controls display the `[Document has N form fields ...]` banner.
  4. **`no_chrome` parameter:** Add `no_chrome: bool = False` to `AdeuReadDocxInput` and forward it to all `build_*_response` calls in `AdeuReadDocx._run`.
  5. **Update `_DESCRIPTION`:** Include `mode='fields'` (`fields_offset`), search page ranges (`page='4-5'`), bounded `page='all'`, and `no_chrome`.
  6. **Update & add tests:** Replace `test_page_all_refuses_when_over_response_budget` in `langchain/tests/integration_tests/test_read_docx.py` with `test_page_all_over_budget_serves_bounded_first_pages` (matching `a8c71f8`), and add integration/unit tests for search with `page="1-2"`, `fields_banner` in `mode="full"`, and `no_chrome=True`.
- **Verify:**
  - `cd langchain && uv run ruff check . && uv run ruff format --check . && uv run mypy langchain_adeu && uv run pytest`
- **Done when:** `AdeuReadDocx` serves bounded pages on over-budget `page="all"`, filters search results by page ranges, displays the form-fields banner in `mode="full"`, supports `no_chrome`, documents `mode="fields"`, and passes all unit/integration tests, ruff, and mypy.

---

### Task 4: LangChain `AdeuApplyChanges` & `README.md` Parity (`[x] Completed`)
- **Goal:** Bring `AdeuApplyChanges` and `langchain/README.md` to full parity with Python MCP `process_document_batch` and v3.2.0 engine features.
- **Files:**
  - `langchain/langchain_adeu/apply_changes.py`
  - `langchain/README.md`
  - `langchain/tests/unit_tests/test_apply_changes.py`
  - `langchain/tests/integration_tests/test_apply_changes.py`
- **Change:**
  1. **Write-Gate Override Parameters:** Add `ignore_control_locks: bool = False`, `ignore_document_protection: bool = False`, and `allow_untracked_writes: bool = False` to `AdeuApplyChangesInput` and pass `gate_overrides={"ignore_control_locks": ignore_control_locks, "ignore_document_protection": ignore_document_protection, "allow_untracked_writes": allow_untracked_writes}` to `RedlineEngine` in `AdeuApplyChanges._run`.
  2. **Enriched v3.2.0 Batch Receipts & Artifacts:**
     - In `_success_artifact` and `_failure_artifact`, add `"actions"` (`stats.get("actions", [])` / `None`) and `"document_state"` (`stats.get("document_state")` / `None`), keeping the key sets identical between both branches.
     - In `_build_success_content` (and pass `source_overwritten` / `existing_output_overwritten` flags):
       - Include overwrite disclosure (`Note: the source document at ... was overwritten in place.` / `Note: replaced existing file ...`).
       - Include `stats.get("overrides_note")` when gate overrides were active.
       - Include `stats.get("document_state")` summary line (`Document State: X tracked change(s), Y comment(s) remaining.`).
       - Include `stats.get("actions")` section (`Applied Actions:` with `[index] OP target_id ...` and minted `comment_id`).
       - In each `Applied Edits:` item, include `report.get("part")` (`Part: ...`), `report.get("field")` (`field: ...`), and minted `change_ids` + `comment_ids` (`IDs: \`Chg:...\`, \`Com:...\``), and drop the duplicate `Clean text preview` line when `critic_markup` is already shown (matching `document.py:909-918`).
  3. **Complete Schema & Tool Descriptions:**
     - Update module docstring, `AdeuApplyChangesInput.changes` description, `_DESCRIPTION`, and the empty-changes error message in `langchain/langchain_adeu/apply_changes.py` to include `set_field`, `part` on `accept`/`reject`, multi-comment lists (`[{"text": "...", "author": "..."}]`), Markdown pipe tables in `new_text`, `{#cell:<id>}` empty-cell anchors, and all derivative suffixes (`.redline`, `-redline`, `_markup`, `.marked`).
  4. **Update `langchain/README.md`:** Document `set_field`, multi-comment lists, pipe tables in `new_text`, `markup_mode="manual"`, gate overrides (`ignore_control_locks`, `ignore_document_protection`, `allow_untracked_writes`), `no_chrome`, search page ranges, and bounded `page="all"`.
- **Verify:**
  - `cd langchain && uv run ruff check . && uv run ruff format --check . && uv run mypy langchain_adeu && uv run pytest`
- **Done when:** `AdeuApplyChanges` accepts gate overrides, emits minted `Chg:N`/`Com:N` IDs, `part`, `actions`, `document_state`, and overwrite notes in its receipt and artifact, documents `set_field`/pipe tables/multi-comments, and passes all unit/integration tests, ruff, and mypy.

---

### Task 5: `n8n-nodes-adeu` Engine & Operation Parity (`[x] Completed`)
- **Goal:** Upgrade `n8n-nodes-adeu` operations and input coercion to expose all `@adeu/core` v3.2.0 features.
- **Files:**
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/GenericFunctions.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/applyEdits.operation.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/generateDiff.operation.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/extractMarkdown.operation.ts`
  - `node/packages/n8n-nodes-adeu/test/Adeu.node.test.ts`
- **Change:**
  1. **`GenericFunctions.ts` (`coerceChangeItemInPlace` & error hints):**
     - Add `first_only`, `firstonly`, and `all_occurrences` to `MATCH_MODE_SYNONYMS`.
     - Infer `item.type = "set_field"` when `"field" in item && "value" in item`.
     - Normalize comment-only `modify` items (`item.type === "modify"` with `new_text === undefined || new_text === null` and a non-empty `comment` string or `[{text, author?}]` list) by setting `item.new_text = item.target_text` (matching `node/packages/mcp-server/src/index.ts:119-134`).
     - Fix the misleading error hint at line 446 so it accurately states that the Structural Appendix is read-only, while plain-text `target_text` resolves across hyperlinks, cross-references, and bookmarks.
  2. **`applyEdits.operation.ts` (`markupMode`):**
     - Add `markupMode` (`"track_changes"` default | `"manual"`) parameter to `applyEditsDescription` and pass `markup_mode: markupMode` into `new RedlineEngine(doc, author, { ..., markup_mode: markupMode })`.
  3. **`generateDiff.operation.ts` (`redlineDocx` one-step comparison):**
     - Add `"redlineDocx"` (`"Redlined DOCX (One-Step Comparison)"`) to `diffFormat` options (with `author` and `outputBinaryPropertyName` fields shown when `diffFormat === "redlineDocx"`).
     - When `diffFormat === "redlineDocx"`, invoke `build_redline_comparison(origBuffer, modBuffer, { author, compare_clean: cleanView })` from `@adeu/core`, prepare the resulting `.docx` binary as `<base>_redlined.docx`, stash `redlinedBinaryId` in workflow static data under `adeu_last_redlined` (matching `Apply Edits` and `Apply Text Revision`), and return `{ originalFileName, modifiedFileName, fileName, author, diffFormat: "redlineDocx", edits_applied, redlinedBinaryId }`.
  4. **`extractMarkdown.operation.ts` (`mode` & page ranges):**
     - Support `mode`: `"full"` (default), `"fields"` (Content Controls ledger via `collectFields` + `renderLedger` from `@adeu/core`), and `"changes"` (Tracked Changes & Comments summary ledger).
     - Allow `page` to accept either a page number (`0` / empty = all, `1` = page 1) or a page range string (`"2-5"`, `"all"`) via `parse_page_arg` from `@adeu/core`, while keeping backward compatibility with numeric `page` values (`0` = all, `>= 1` = single page).
- **Verify:**
  - `cd node && npm run build && npm test && npm run lint`
- **Done when:** `Apply Edits` supports `markupMode="manual"`, `Generate Diff` supports `diffFormat="redlineDocx"` via `build_redline_comparison`, `Extract Markdown` supports `fields`/`changes` modes and page ranges, `GenericFunctions` coerces `set_field` and comment-only `modify` items, and unit tests in `Adeu.node.test.ts` cover every new capability.

---

### Task 6: `n8n-nodes-adeu` Prompts, Descriptions, `$fromAI` Recipes, README & Examples (`[x] Completed`)
- **Goal:** Update all n8n node descriptions, `$fromAI` recipes, `README.md`, and example workflows so AI agents in n8n use the latest v3.2.0 capabilities.
- **Files:**
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/index.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/applyEdits.operation.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/extractMarkdown.operation.ts`
  - `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/generateDiff.operation.ts`
  - `node/packages/n8n-nodes-adeu/README.md`
  - `node/packages/n8n-nodes-adeu/examples/AI_Agent_workflow.json`
  - `node/packages/n8n-nodes-adeu/test/examples.test.ts`
- **Change:**
  1. Document all 7 `DocumentChange` types (`modify`, `set_field`, `accept`, `reject`, `reply`, `insert_row`, `delete_row`), `part` on `accept`/`reject`, multi-comment lists (`comment: string | [{text, author?}]`), Markdown formatting & pipe tables in `new_text`, `{#cell:<id>}` empty-cell anchors, sequential batch evaluation, whole-paragraph restructuring guidance, and header/footer block restrictions across `Adeu.node.ts`, `descriptions/index.ts`, `applyEdits.operation.ts`, `README.md`, and `examples/AI_Agent_workflow.json`.
  2. Document `markupMode` (`"track_changes"` | `"manual"`), `Generate Diff`'s `"redlineDocx"` mode, and `Extract Markdown`'s `mode` (`"full"` | `"fields"` | `"changes"`) and page range support in `README.md`.
  3. Clean up the stale comment in `node/packages/n8n-nodes-adeu/test/examples.test.ts:9-15`.
- **Verify:**
  - `cd node && npm run build && npm test && npm run lint`
- **Done when:** All n8n node descriptions, `$fromAI` recipes, `AI_Agent_workflow.json`, and `README.md` document the full v3.2.0 feature set and pass `examples.test.ts`, `codex-consistency.test.ts`, and `npm run lint`.

---

### Task 7: Skills, `GEMINI.md`, READMEs & Release Version Synchronization (`[x] Completed`)
- **Goal:** Bring `GEMINI.md`, `skills/adeu-redlining/`, root/package READMEs, and plugin/skill version manifests up to v3.2.0 parity.
- **Files:**
  - `GEMINI.md`
  - `skills/adeu-redlining/SKILL.md`
  - `skills/adeu-redlining/references/mcp-tools.md`
  - `skills/adeu-redlining/references/criticmarkup.md`
  - `skills/adeu-redlining/references/cli-fallback.md`
  - `.claude-plugin/marketplace.json`
  - `scripts/bump.py`
  - `scripts/check_release_consistency.mjs`
  - `README.md`
  - `python/README.md`
  - `node/packages/mcp-server/README.md`
- **Change:**
  1. **`GEMINI.md`:** Fix `process_document_batch` parameter names (`original_docx_path`, `author_name`, `output_path`, `markup_mode`); add `match_mode`, `regex`, `insert_row`, `delete_row`, Markdown pipe tables in `new_text`, `{#cell:<id>}` anchors; add `mode="changes"` (`changes_author`, `changes_offset`), page ranges (`page="1-8"`, search `page="4-5"`), `search_regex`, `search_case_sensitive`, and bounded `page="all"` behavior to `read_docx`; add `diff_docx_files` (including one-step `output_path` / `author_name`) and `finalize_document` / `sanitize_docx`; align the Recommended Workflow with the low-turn workflow in `SKILL.md`.
  2. **`skills/adeu-redlining/`:**
     - `SKILL.md` & `references/mcp-tools.md`: document multi-comment lists `[{"text": "...", "author": "..."}]`, `apply_text_revision`, and missing `read_docx` parameters (`changes_author`, `changes_offset`, `max_matches`, `match_offset`, `force`).
     - `references/criticmarkup.md`: document Content Control tokens (`{#cc:N}...{#/cc:N}`, `[ ]`/`[x]`), `{#cell:<paraId>}` anchors, `![alt](docx-image:N)` markers, and plain-text anchor resolution across hyperlinks, cross-references, and bookmarks.
     - `references/cli-fallback.md`: document multi-comment lists and Markdown pipe tables in `new_text`.
  3. **Version Lockstep:**
     - Sync `.claude-plugin/marketplace.json` (`"version": "3.2.0"`) and `skills/adeu-redlining/SKILL.md` (`version: "3.2.0"`), and register `.claude-plugin/marketplace.json` in `scripts/bump.py` (`FILES_TO_BUMP`) and `scripts/check_release_consistency.mjs` (`VERSIONED_MANIFESTS`) so plugin versions never drift on future releases.
  4. **READMEs:**
     - `README.md`: update Recommended Agent Prompt and CLI examples to include `mode="outline"` / `"changes"` / `"fields"`, `set_field`, `markup_mode="manual"`, and one-step `adeu diff -o`.
     - `python/README.md`: document `adeu diff -o redlined.docx --author ...`, `adeu apply --manual`, `SetField`, and accurate batch failure JSON keys.
     - `node/packages/mcp-server/README.md`: add `apply_text_revision`, one-step `diff_docx_files(output_path=...)`, and `markup_mode="manual"`, and remove non-existent Cloud/Email tool listings.
- **Verify:**
  - `node scripts/check_release_consistency.mjs`
  - Full test suites across `python/`, `node/`, and `langchain/`
- **Done when:** All prompts, skills, READMEs, and version manifests are synchronized to `3.2.0` and all Python, Node, and LangChain test and lint suites pass with zero errors.

---

## Risks

- **MCP Schema Dialect Guard (`node/packages/mcp-server`):** `spec_2026_07_28.test.ts` enforces that emitted tool schemas contain no `$schema`, `definitions`, `additionalItems`, or tuple-form `items`. All schema description updates in `node/packages/mcp-server/src/index.ts` must preserve flat Zod object shapes and be verified with `npm run build && npm test`.
- **LangChain Artifact Key Parity (`langchain-adeu`):** `TestAdeuApplyChangesArtifactParity` enforces that `_success_artifact` and `_failure_artifact` share the exact same key set. Any new artifact key (`actions`, `document_state`) must be added to both functions simultaneously.
- **n8n Codex Integrity (`n8n-nodes-adeu`):** `Adeu.node.json` `nodeVersion` (`"1.0"`) and `codexVersion` (`"1.0"`) must not be bumped, and every example workflow in `examples/` must pass `examples.test.ts`.

PLAN COMPLETE
