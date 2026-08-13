# Agent-efficiency implementation plan â€” Node packages (`@adeu/core`, `@adeu/mcp-server`)

Reference spec: `docs/improvement_spec.md` (Groups Aâ€“E, Â§8 Node parity audit, Â§9 roadmap).

## Context

- Monorepo `adeu`: Python engine (`python/`, adeu **2.2.0**) and Node port
  (`node/packages/core` = `@adeu/core` **2.2.0**, `node/packages/mcp-server` =
  `@adeu/mcp-server` **2.2.0**, `node/packages/n8n-nodes-adeu`). Versions read from
  `python/pyproject.toml:3`, `node/packages/core/package.json:3`,
  `node/packages/mcp-server/package.json:3`.
- **The audit's headline finding: Python has already shipped most of
  `docs/improvement_spec.md`; Node has shipped almost none of it.** This plan is
  therefore a *parity port*, not a green-field design: Python is the byte-format
  authority for every response, and each task cites the Python `file:line` the
  Node code must reproduce. Verified Python implementations found this session:
  A1 changes ledger (`_response_builders.py:1391`), A2 search flags/clamp/budget
  (`_response_builders.py:716`, `:158-202`), A3 budget guard
  (`_response_builders.py:654`, `payloads.py:398-479`), A4 `no_chrome`
  (`_response_builders.py:405,428,461-464`), A6 page ranges
  (`pagination.py:26-89`, `_response_builders.py:485`), B1 minimal report
  (`payloads.py:16,299-395`), B2 recovery protocol (`payloads.py:67`),
  B3 comment-only modify (`models.py:427-505`), B5 salvage
  (`engine.py:2551-2560,2656-2889`), B7 fused hint (`payloads.py:74-81`),
  B9 envelope (`payloads.py:90-121`, `engine.py:70-94`), C1 guard message
  (`engine.py:2320-2358`), C2 impersonation warning (`engine.py:2588-2670,2886`),
  C3 text apply (`tools/document.py:1140`), E1 optional `reasoning`
  (`tools/document.py:1377-1383`), E4 ledger id hint (`mcp_components/shared.py:15`).
- Node's surface today (verified by reading the sources): MCP tools `read_docx`,
  `process_document_batch`, `accept_all_changes`, `diff_docx_files`,
  `finalize_document` (`node/packages/mcp-server/src/index.ts:346,684,921,1038,1120`);
  response builders in `node/packages/mcp-server/src/response-builders.ts`;
  engine in `node/packages/core/src/engine.ts`. **There is no Node CLI** â€” every
  CLI-flavoured spec item lands on the MCP surface or on a builder parameter.
- Node gaps confirmed: no page ranges (single page / `all` only,
  `index.ts:519-568`), no changes ledger (`mode` enum is
  `["full","outline","appendix"]`, `index.ts:365-370`), hardcoded
  `max_matches = 20` with no offset and whole-line snippets
  (`response-builders.ts:594-603,646-704`), no budget guard, no failure envelope
  (prose only, `index.ts:866-874`), transactional-only batches with no `partial`
  (`engine.ts:3584-3594`), both previews echoed in reports
  (`index.ts:1280-1285`), `reasoning` **required** on all five tools
  (`index.ts:353,689,934,1043,1126`), no text-apply tool, accept/reject `comment`
  silently dropped (`engine.ts:4591-4699` never reads `action.comment`).
- Node already satisfies: B3 at engine level (comment-only modify normalises
  `new_text := target_text`, `engine.ts:2809-2831`), B6 (nothing re-escapes
  non-ASCII), E3 (closest-sibling file-not-found suggestions,
  `index.ts:91-155`), and pagination equivalence with Python (`pagination.ts:190-233`
  vs `pagination.py:373-431` â€” the only Python-side extra is the
  `<w:br w:type="page"/>` force-split branch, and **no projection emits that
  marker**, verified by grep across `python/src/adeu/**`).

## Toolchain (verified)

Run from `node/` unless stated:

| Purpose | Command |
|---|---|
| Install | `npm ci` |
| Build all packages | `npm run build` |
| Test all packages | `npm run test` |
| Lint | `npm run lint` |
| Single core test file | `cd node/packages/core && npx vitest run src/payloads.test.ts` |
| Single mcp-server test | `cd node/packages/mcp-server && npx vitest run src/ledger.test.ts` |
| Single test by name | `cd node/packages/core && npx vitest run -t "renders a pair suffix"` |
| Python reference (goldens) | `cd python && uv run python <script>` |
| Release consistency | `node scripts/check_release_consistency.mjs` (repo root) |

Environment rules the executor must obey:

1. **Build before test, always.** `@adeu/mcp-server` imports `@adeu/core` through
   the workspace symlink to `core/dist`. A core change is invisible to
   mcp-server tests until `npm run build` runs. Several mcp-server tests spawn
   `dist/index.js` (`mcp.schema-gaps.test.ts:127-134`) and fail loudly if the
   bundle is stale.
2. `npm run lint` currently only lints `n8n-nodes-adeu` (neither `core` nor
   `mcp-server` declares a `lint` script â€” verified in their `package.json`).
   Do **not** add lint scripts or eslint config; do not touch the deliberate
   split TypeScript pin documented in `node/package.json` and `AGENTS.md`.
3. `node/packages/core` has `vitest.config.ts` with a `@shared/fixtures` alias;
   `mcp-server` has none (vitest defaults). Put core tests in
   `node/packages/core/src/*.test.ts` and server tests in
   `node/packages/mcp-server/src/*.test.ts`, mirroring existing naming.
4. Never edit `python/**` in this plan. Python-side mirrors are called out as
   parity notes only (`AGENTS.md` "Dual-Engine Parity"): every task here either
   ports an existing Python behaviour or is flagged `PARITY-LEADING`.
5. End every file with a single trailing newline. Minimal diffs â€” no drive-by
   reformatting of the large existing files.

## Target versions

- P0 + P1 + P2 ship as **2.3.0** for `@adeu/core`, `@adeu/mcp-server` and `adeu`
  (Python) together (spec Â§8.4 version lockstep). Bump with
  `python scripts/bump.py minor` from the repo root, then verify with
  `node scripts/check_release_consistency.mjs` (Task 22).
- B5's *default* contract flip (salvage-by-default on the CLI) is a **3.0**
  concern for Python. On Node, MCP `partial` defaults to `true` in 2.3.0 to match
  the shipped Python MCP default (`tools/document.py:1511-1514`); the library
  default (`RedlineEngine.process_batch`) stays `partial = false`, exactly as
  Python (`engine.py:2555`).
- `engine.ts:3663` hardcodes `version: "1.18.2"` in batch stats â€” stale by four
  minors. Task 8 replaces it with the real package version.

## Assumptions

Each is a choice made where the request or the spec left room; all are
reversible and flagged in the receipt.

1. **Python is the byte-format authority.** Where Python's shipped wording
   differs from the prose in `docs/improvement_spec.md` (e.g. C1's guard message
   is `Accept first with {"type": "accept", â€¦}` rather than the spec's draft
   sentence), Node matches **Python**, not the spec draft. The spec's Â§8.3
   requirement is identical goldens, so the shipped implementation wins.
2. **No Node CLI is created.** CLI-only spec items (`--no-chrome`, `--max-matches`
   as a flag, `--report`, `--atomic`) become builder/engine parameters plus MCP
   tool parameters *only where Python's MCP surface exposes them*. Python's MCP
   `read_docx` has no `no_chrome` parameter, so Node's must not either: the
   parameter exists on the builders (Task 17) for future CLI use and for tests.
3. **`no_chrome` on builders is internal.** Not advertised in any tool schema in
   2.3.0.
4. **B1 "minimal" on MCP is the rendered markdown report**, matching Python's
   MCP renderer (`tools/document.py:787-822`): one preview, no `target_text`/
   `new_text` echo. The JSON `shrink_batch_stats` port (Task 1) exists for
   structured consumers (n8n, future CLI) and is unit-tested directly.
5. **B4 (honour `comment` on accept/reject) is PARITY-LEADING** â€” Python has
   *not* implemented it (`models.py:169,179` still say "Optional rationale";
   `engine.py`'s `apply_review_actions` never reads it). Task 10 implements it in
   Node behind a report-visible contract and **must not be released before the
   Python mirror lands**; the task carries that gate explicitly.
6. **B8 (terse error knobs) is out of scope** â€” spec Â§9 puts it last, Python has
   no `--terse-errors` either, and it needs a CLI to be reachable.
7. **A5 (compact `diff --json`) is an audit-only task on Node** (Task 20): Node's
   MCP `diff_docx_files` returns the human `@@ Word Patch @@` text and has no
   JSON mode, so the only Node surface at risk is `generate_structured_edits`.
8. **D1/D2/D3 (perf) are out of scope**: Node already has an in-process doc cache
   with LRU + hot-DOM reuse (`mcp-server/src/doc-cache.ts`), and the spec ranks
   these last.
9. **Goldens are captured from Python once and committed** under
   `shared/conformance/`, with fixtures generated by a Node script so both
   engines read byte-identical `.docx` files. Conformance tests compare Node
   output to the committed goldens and **skip with a clear message** if a golden
   is missing rather than silently passing.
10. **`page` stays a single published JSON type (string)** in the MCP schema â€”
    the existing `z.preprocess` numberâ†’string shim at `index.ts:377-387` is
    load-bearing for real clients (QA 2026-07-23 client-compat) and ranges must
    reuse it, not introduce a union.
11. **Token budgets are asserted with the project's crude unit**:
    `approxTokens(s) = Math.floor(s.length / 4)`, matching Python's
    `len(json) // 4` (`payloads.py:208`).

## Parity map (spec item â†’ Python authority â†’ Node target)

| Item | Python authority (`file:line`) | Node target |
|---|---|---|
| A1 ledger | `mcp_components/_response_builders.py:1372-1815` | new `mcp-server/src/ledger.ts` + `read_docx` mode |
| A2 search | `_response_builders.py:111-343,716-1278` | rewrite `build_search_response` |
| A3 guard | `_response_builders.py:654-700`, `payloads.py:398-479` | `core/src/payloads.ts` + `read_docx` |
| A4 chrome | `_response_builders.py:405,428,461-464,485,519-546,558,628-630` | builder params |
| A6 ranges | `pagination.py:20-89`, `_response_builders.py:485-555` | `core/src/pagination.ts` + new builder |
| B1 report | `payloads.py:16-20,299-395`, `tools/document.py:787-822` | `core/src/payloads.ts` + `formatBatchResult` |
| B2 protocol | `payloads.py:67-71,87-121` | `core/src/payloads.ts` |
| B3 modify | `models.py:427-457`, `engine.py` normalise | verify + MCP type inference |
| B4 comment | *not implemented* | PARITY-LEADING (Task 10) |
| B5 salvage | `engine.py:2551-2560,2656-2889`, `tools/document.py:739-770` | `engine.ts` + MCP `partial` |
| B7 fused | `payloads.py:74-81`, `cli.py:549-550` | `core/src/payloads.ts` + MCP boundary |
| B9 envelope | `payloads.py:90-121`, `engine.py:70-94` | `core/src/payloads.ts` + `engine.ts` |
| C1 guard msg | `engine.py:2320-2358`, `GUARD_MESSAGE_CAP` `engine.py:49` | `engine.ts:3157-3176` |
| C2 impersonation | `engine.py:2588-2670,2886` | `engine.ts` stats |
| C3 text apply | `tools/document.py:1140-1203`, `text_revision.py:209` | new MCP tool |
| E1 reasoning | `tools/document.py:1377-1383` | all five Node tools |
| E4 id hint | `mcp_components/shared.py:15-18`, `engine.py:4988` | `engine.ts:4553-4555` |

---

# Phase 0 â€” Foundations (must land first; every later task depends on them)

## Task 0 â€” Shared primitives in `@adeu/core`
- **Status**: COMPLETED
- **Failed Verify Cycles**: 2
- **Attempt Ledger**:
  - attempt 1: implement shared primitives in core -> FAIL (double trailing newline in node/packages/core/src/primitives.test.ts)
  - attempt 2: fix trailing newline in primitives.test.ts -> FAIL (missing trailing newline in node/packages/core/src/pagination.ts)
  - attempt 3: ensure trailing newline in pagination.ts -> PASS

- **Goal**: give the later tasks the four primitives Python's implementations are
  built on: `clamp_text`, `parse_page_arg` + `PAGE_RANGE_MAX_PAGES`,
  `offset_to_page`, and a public `extract_comments_data`.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/core/src/utils/text.ts` (exists; `truncate_middle` at
    `:47`, `REPORT_ECHO_CAP` at `:11`)
  - modify `node/packages/core/src/pagination.ts` (exists)
  - modify `node/packages/core/src/outline.ts` (exists; `_offset_to_page` at
    `:565` â€” export it, do not duplicate)
  - modify `node/packages/core/src/index.ts` (exists, 14 lines)
  - create `node/packages/core/src/primitives.test.ts`
- **Test first** â€” `node/packages/core/src/primitives.test.ts`:
  - `clamp_text`: `clamp_text("abcdef", 6) === "abcdef"`;
    `clamp_text("abcdefgh", 6) === "abc..."` (i.e. `slice(0, cap-3) + "..."`);
    `clamp_text("abcdefgh", 2) === "a..."` (the `max(1, cap-3)` floor);
    result of a clamp is never longer than `cap` for `cap >= 4`.
  - `parse_page_arg`: `undefined`/`null` â†’ `["single", 1]`; `3` â†’ `["single", 3]`;
    `"3"` â†’ `["single", 3]`; `" all "` and `"ALL"` â†’ `["all", null]`;
    `"2-6"` and `" 2 - 6 "` â†’ `["range", [2, 6]]`; `0`, `-1`, `""`, `"x"`,
    `"0-3"`, `"2-0"` each throw with the exact message
    ``Invalid page parameter: '<raw>'. Provide a positive integer, page range (e.g. '2-6'), or 'all'.``
    (message string copied verbatim from `python/src/adeu/pagination.py:50`).
    Note `"6-2"` (start > end) does **not** throw here â€” the builder rejects it
    (Task 3), matching Python.
  - `PAGE_RANGE_MAX_PAGES === 8`.
  - `offset_to_page(0, [])` â†’ 1; `offset_to_page(25, [0, 10, 20])` â†’ 3;
    `offset_to_page(5, [0, 10, 20])` â†’ 1.
  - `extract_comments_data` is importable from `@adeu/core` and returns `{}` for
    a document with no comments part (build one with the pattern used in
    `node/packages/mcp-server/src/mcp.schema-gaps.test.ts:87-115`).
- **Change**:
  1. `utils/text.ts` â€” add, next to `truncate_middle`:
     ```ts
     /**
      * Hard-caps `text` to at most `cap` characters, marking the elision with an
      * ASCII "...". Use this wherever the cap is a real ceiling: truncate_middle
      * keeps head AND tail plus a "[N chars omitted]" note, so its result
      * routinely runs LONGER than `cap` â€” fine for a 500-char echo budget, fatal
      * for the minimal report's per-edit token budget.
      * Mirrors python/src/adeu/utils/text.py clamp_text.
      */
     export function clamp_text(text: string, cap: number): string {
       if (text.length <= cap) return text;
       return text.slice(0, Math.max(1, cap - 3)) + "...";
     }
     ```
  2. `pagination.ts` â€” add `export const PAGE_RANGE_MAX_PAGES = 8;` beside
     `PAGE_TARGET_CHARS` (`:5`), plus:
     ```ts
     export type PageArgKind = "single" | "range" | "all";
     const _PAGE_RANGE_RE = /^\s*(\d+)\s*-\s*(\d+)\s*$/;

     /** Mirrors python/src/adeu/pagination.py parse_page_arg (:26-89). */
     export function parse_page_arg(
       page: number | string | null | undefined,
     ): [PageArgKind, number | [number, number] | null] {
       const bad = (raw: unknown): never => {
         throw new Error(
           `Invalid page parameter: '${raw}'. Provide a positive integer, page range (e.g. '2-6'), or 'all'.`,
         );
       };
       if (page === null || page === undefined) return ["single", 1];
       if (typeof page === "number") {
         if (!Number.isInteger(page) || page < 1) bad(page);
         return ["single", page];
       }
       if (typeof page === "string") {
         const s = page.trim();
         if (!s) bad(page);
         if (s.toLowerCase() === "all") return ["all", null];
         const m = _PAGE_RANGE_RE.exec(s);
         if (m) {
           const startP = parseInt(m[1], 10);
           const endP = parseInt(m[2], 10);
           if (startP < 1 || endP < 1) bad(page);
           return ["range", [startP, endP]];
         }
         if (!/^\d+$/.test(s)) bad(page);
         const val = parseInt(s, 10);
         if (val < 1) bad(page);
         return ["single", val];
       }
       return bad(page);
     }
     ```
     `^\d+$` is deliberate: JS `parseInt("3x")` returns 3 where Python's `int()`
     raises, and a silent "3" for "3x" would diverge from Python.
  3. `outline.ts` â€” change `function _offset_to_page` at `:565` to
     `export function offset_to_page` and update its two internal call sites
     (`:87`, `:681`). Keep the body byte-identical.
  4. `index.ts` â€” extend the export lines:
     ```ts
     export { paginate, split_structural_appendix, parse_page_arg, PAGE_RANGE_MAX_PAGES, PaginationResult, PageInfo, PageArgKind } from './pagination.js';
     export { extract_outline, offset_to_page, OutlineNode } from './outline.js';
     export { extract_comments_data } from './comments.js';
     export { clamp_text, truncate_middle, REPORT_ECHO_CAP, PREVIEW_TEXT_CAP } from './utils/text.js';
     ```
     (`extract_comments_data` is already exported from `comments.ts:555`.)
- **Done when**:
  - `cd node && npm run build` succeeds (both packages).
  - `cd node/packages/core && npx vitest run src/primitives.test.ts` â€” all new
    tests pass.
  - `cd node && npm run test` â€” zero failures (no existing test asserts on
    `_offset_to_page`'s privacy).

## Task 1 â€” `core/src/payloads.ts`: failure envelope, recovery protocol, budget guard, minimal report
- **Status**: COMPLETED
- **Failed Verify Cycles**: 2
- **Attempt Ledger**:
  - attempt 1: port payloads module with failure envelope and budget guard -> FAIL (env variable ADEU_MAX_RESPONSE_CHARS parsing discrepancy with Python int() for digit-group underscores like "1_000")
  - attempt 2: support digit underscores in ADEU_MAX_RESPONSE_CHARS parsing -> FAIL (truthiness of empty pages: [] array, deduplication key for non-string skipped_details, and line splitting regex for Unicode line breaks)
  - attempt 3: align payloads truthiness, deduplication, and line splitting with Python -> PASS

- **Goal**: port `python/src/adeu/payloads.py` (479 lines) to TypeScript so
  B9/B2/B7/B1/A3 all draw their strings and budgets from one shared module.
- **Difficulty**: HARD (three interacting size-fitting loops; every string is a
  golden).
- **Files**:
  - create `node/packages/core/src/payloads.ts`
  - create `node/packages/core/src/payloads.test.ts`
  - modify `node/packages/core/src/index.ts` (export the module's public API)
  - modify `node/packages/core/src/diff.ts` **only if** it does not already export
    a CriticMarkup block regex â€” check first: Python uses
    `diff.CRITICMARKUP_BLOCK_RE` (`payloads.py:9`). Search Node for an existing
    equivalent (`rg -n "CRITICMARKUP|\\{>>\\.\\*\\?" node/packages/core/src/diff.ts`)
    and reuse it; only add one if genuinely absent. UNVERIFIED â€” executor must
    confirm whether `diff.ts` exports such a regex before adding anything.
- **Test first** â€” `node/packages/core/src/payloads.test.ts`:
  - `failure_envelope`:
    - `failure_envelope("batch_validation_failed", [[0, "boom"]], "Batch rejected.")`
      â†’ `{ error: "batch_validation_failed", failed: [{ index: 0, reason: "boom" }], message: "Batch rejected. " + BATCH_RECOVERY_PROTOCOL }`.
    - Indices are **0-based** and preserved as given (spec B9); a two-failure
      batch `[[1,"a"],[4,"b"]]` renders both in order.
    - `message` is flattened: newlines collapse to single spaces with empty lines
      dropped (`payloads.py:111`).
    - A non-batch code (`"response_budget_exceeded"`) does **not** get the
      protocol appended.
    - An already-protocol-bearing message is not double-suffixed.
    - `errors` is present only when passed.
  - `has_fused_json_marker`: true for `'modify}],{comment:'`, `'{"type"'`,
    `'a":b'`; false for `"modify"`, `""`, non-strings.
  - `response_budget_limit()` â†’ 76000 by default; honours
    `process.env.ADEU_MAX_RESPONSE_CHARS = "1000"`; ignores unparseable values
    (restore the env in `afterEach`).
  - `whole_doc_guard_message`:
    - contains the exact head line
      ``Refused unbounded full document read for '<path>' (16 pages): total size (207,000 chars, ~51,750 tokens) exceeds response budget limit (76,000 chars).``
      â€” thousands separators via `toLocaleString("en-US")`, `est = floor(chars/4)`.
    - contains all six recipe lines verbatim (`payloads.py:460-465`), including
      ``  - Tracked changes ledger: --mode changes (MCP mode='changes')``.
    - with a 400-entry outline, the emitted length stays
      `<= GUARD_EMITTED_MAX_CHARS` (3100) and the tail note
      ``  (N more headings: --mode outline / MCP mode='outline')`` appears.
    - with `outline === ""` there is **no** `Outline (L1 Headings):` section.
    - a >160-char path is rendered as `"..." + path.slice(-160)`.
    - budget test: `approxTokens(msg) <= 800` (spec A3).
  - `shrink_batch_stats`:
    - drops `engine`, keeps `version`, keeps counters and `output_path`.
    - an applied edit loses `target_text`, `new_text`, `clean_text`, `comment`
      and keeps `status`, `type`, `critic_markup`, `pages`, `heading_path`,
      `occurrences_modified`; `match_mode` only when `!== "strict"`.
    - a failed edit keeps the full `error` and an `<= 80`-char clamped
      `target_text` stub, and carries **no** `critic_markup`.
    - budget test: for every applied edit,
      `approxTokens(JSON.stringify(edit without "error")) <= 40`
      (`MINIMAL_EDIT_TOKEN_BUDGET`), asserted over a fixture set that includes:
      a plain edit; an edit with a 12-page `pages` array; an edit with a
      260-char `warning`; a `match_mode:"all"` fan-out whose `critic_markup`
      holds 10 bubbles.
    - CriticMarkup safety: for every shrunk value, the string is either `""` or
      balanced â€” no output may contain a bare `{--`, `--}`, `{++`, `++}`, `{==`,
      `==}`, `{>>` or `<<}` outside a complete bubble.
    - `skipped_details` entries that repeat a per-edit `error` (whole message or
      any single line of it) are dropped; unrelated notes survive; order is
      preserved.
- **Change**: port `payloads.py` function-for-function, keeping the Python names
  in snake_case so reviewers can diff the two files side by side. Public API:
  ```ts
  export const MINIMAL_EDIT_TOKEN_BUDGET = 40;
  export const FAILED_TARGET_STUB_CAP = 80;
  export const BATCH_RECOVERY_PROTOCOL = "Nothing was written. Recover in two calls: (1) re-apply this batch WITHOUT the failing edit(s); (2) fix the failing edit(s) in a separate small batch. Copy target_text verbatim from a fresh read of the CURRENT file, not from another tool's view of it.";
  export const FUSED_JSON_HINT = "This looks like two edits fused during generation â€” resubmit this edit alone, correctly formed.";
  export const BATCH_ERROR_CODES: ReadonlySet<string>; // {"invalid_changes_file","batch_validation_failed"}
  export const GUARD_EMITTED_MAX_CHARS = 3100;
  export interface FailureEnvelope { error: string; failed: { index: number; reason: string }[]; message: string; errors?: string[] }
  export function failure_envelope(code: string, failed: [number, string][], message: string, errors?: string[]): FailureEnvelope;
  export function has_fused_json_marker(text: unknown): boolean;
  export function response_budget_limit(): number;
  export function whole_doc_guard_message(total_chars: number, limit: number, file_path?: string, outline?: string, page_count?: number | null): string;
  export function shrink_batch_stats(stats: Record<string, any>): Record<string, any>;
  ```
  Porting rules (each maps to a Python line range â€” follow them literally):
  - `failure_envelope` â† `payloads.py:90-121`. Flatten with
    `message.split("\n").map(l => l.trim()).filter(Boolean).join(" ")`.
  - Number formatting in the guard â† `f"{n:,}"`: use
    `n.toLocaleString("en-US")` (asserted in the test, so a locale surprise
    fails loudly).
  - `_guard_emitted_length` â† `payloads.py:424-426`: measure
    `JSON.stringify(failure_envelope("response_budget_exceeded", [], message)).length`.
    JS `JSON.stringify` does not escape non-ASCII (Python passes
    `ensure_ascii=False`), so lengths match â€” this is the B6 parity point, and
    the test above pins it with a smart-quoted path.
  - The outline-trim loop â† `payloads.py:468-479`: drop whole entries from the
    tail, re-render, stop when the emitted length fits or nothing is left.
  - `_shrink_critic_markup` / `_bubble_segments` / `_clamp_bubble` /
    `_changed_span` / `_has_orphaned_critic_delimiters` â†
    `payloads.py:124-198`. Keep `_ELISION = "..."`, `_MIN_BUBBLE_BODY = 8`,
    `_MIN_WARNING_CHARS = 26`, `_CRITIC_DELIM_LEN = 3` and the
    `"(+N more spans)"` note wording exactly.
  - `_fit_to_budget` â† `payloads.py:224-296`: the priority ladder is
    **context â†’ deepest-heading-only â†’ drop heading_path â†’ clamp warning â†’
    clamp bubble bodies â†’ drop pages â†’ drop preview**. Do not reorder; the
    ladder is what keeps a warned fan-out inside 40 tokens.
  - `_within_budget` â† `payloads.py:201-208`:
    `Math.floor(JSON.stringify(budgeted).length / 4) <= 40`, with `error`
    excluded from `budgeted`.
  - `_shrink_prose` â† `payloads.py:211-221`: re-clamp the **original** value at
    `cap = max(floor, Math.floor(cap * 4 / 5))` each step and re-measure.
  - `_minimal_edit` â† `payloads.py:299-331`, `_dedupe_skipped` â†
    `:343-356`, `_error_lines` â† `:334-340`.
  - Where Python iterates a dict, iterate `Object.entries` in insertion order â€”
    both languages preserve insertion order, so field order in the emitted JSON
    stays identical (the goldens depend on it).
- **Done when**:
  - `cd node && npm run build`.
  - `cd node/packages/core && npx vitest run src/payloads.test.ts` â€” all pass,
    including the four budget assertions.
  - `cd node && npm run test` â€” zero failures.

## Task 2 â€” Conformance fixtures, golden capture, and the token-budget harness (spec Â§8.3)
- **Status**: COMPLETED
- **Failed Verify Cycles**: 1
- **Attempt Ledger**:
  - attempt 1: add conformance fixtures, golden capture script, and test harness -> FAIL (conformance harness file_path banner normalization discrepancy on Windows where resolve('/fixtures/<name>.docx') prepends drive letter e.g. D:\fixtures\...)
  - attempt 2: normalize path banners for cross-platform golden comparison -> PASS

- **Goal**: create the shared fixture set and the golden files that every later
  task asserts against, so "identical to Python" is a runnable test rather than a
  claim.
- **Difficulty**: HARD (cross-language, and it defines the contract the rest of
  the plan is verified by).
- **Files**:
  - create `shared/conformance/build_fixtures.mjs` (Node; generates the `.docx`
    files so both engines read identical bytes)
  - create `shared/conformance/fixtures/` (generated `.docx`, committed)
  - create `shared/conformance/capture_goldens.py` (Python; writes goldens)
  - create `shared/conformance/goldens/` (committed `.txt`)
  - create `shared/conformance/README.md` (how to regenerate; 20 lines max)
  - create `node/packages/mcp-server/src/conformance.test.ts`
  - create `node/packages/mcp-server/src/conformance-utils.ts` (golden loader +
    `approxTokens`)
- **Test first**: `conformance.test.ts` is itself the test. Write it against the
  goldens **before** any builder work, so it fails for the right reason (missing
  Node capability), not for a missing file. Structure:
  ```ts
  import { describe, it, expect } from "vitest";
  import { readFileSync, existsSync } from "node:fs";
  import { resolve } from "node:path";

  const GOLDEN_DIR = resolve(import.meta.dirname, "../../../../shared/conformance/goldens");
  export function golden(name: string): string | null {
    const p = resolve(GOLDEN_DIR, name);
    return existsSync(p) ? readFileSync(p, "utf-8") : null;
  }
  export const approxTokens = (s: string) => Math.floor(s.length / 4);
  ```
  Each conformance case does `const want = golden("ledger_multi_author.txt");`
  and `if (want === null) return expect.fail("golden missing â€” run shared/conformance/capture_goldens.py")`.
  Never `it.skip` silently.
- **Change**:
  1. `build_fixtures.mjs` â€” build six fixtures with `@adeu/core` only (no Word,
     no benchmark corpus, per spec Â§8.3). Reuse the doc-construction pattern from
     `node/packages/mcp-server/src/mcp.schema-gaps.test.ts:87-115` (load
     `shared/fixtures/initial.docx`, clear the body, append paragraphs) and drive
     tracked changes through `new RedlineEngine(doc, author).process_batch([...])`
     so the revisions are real OOXML:
     - `multi_author.docx` â€” 3 authors ("Jane Doe", "Bob Smith", "Acme LLP"),
       â‰¥2 paired ins/del replacements, one format-only change, one edit wholly
       inside a foreign insertion, â‰¥6 changes total.
     - `comments_threads.docx` â€” 3 top-level comments + 2 replies (use a
       `modify` with `comment`, then `reply` actions).
     - `tables_cells.docx` â€” a 3Ã—3 table with one cell revised and one empty
       cell carrying a `{#cell:â€¦}` anchor.
     - `unicode.docx` â€” smart quotes `â€™ â€œ â€ â€”` and a non-ASCII author name.
     - `long_5pages.docx` â€” >4 synthetic pages (>76,000 chars projected, so the
       A3 guard fires) with L1/L2 headings.
     - `dense_175.docx` â€” â‰¥175 tracked changes for the ledger paging and
       token-budget cases.
     Write each to `shared/conformance/fixtures/`. Determinism matters: seed all
     text from literals, never `Date.now()` or randomness, so regenerating does
     not churn the goldens.
  2. `capture_goldens.py` â€” for each fixture, project it with the Python engine
     and call the Python builders **with `is_cli=False`** (the MCP flavour Node
     must match), writing `BuilderResult.content` to
     `shared/conformance/goldens/<case>.txt`. Cases to capture (names are the
     contract used by later tasks):
     `ledger_multi_author`, `ledger_comments_threads`, `ledger_tables`,
     `ledger_author_filter`, `ledger_page_filter`, `ledger_dense_offset0`,
     `ledger_dense_offset300`, `range_2_4`, `range_cap_1_12`,
     `range_past_end`, `guard_long5`, `search_default`,
     `search_max2_offset2`, `search_full_paragraph`, `outline_l1`.
     Sketch (the executor fills in per-case arguments):
     ```python
     # run from python/: uv run python ../shared/conformance/capture_goldens.py
     from adeu.redline.engine import RedlineEngine
     from adeu.mcp_components import _response_builders as rb
     ...
     res = rb.build_changes_response(text, str(path), comments_data=comments, is_cli=False)
     (GOLDEN_DIR / "ledger_multi_author.txt").write_text(res.content, encoding="utf-8", newline="\n")
     ```
     `file_path` must be written into the goldens as a **stable placeholder**
     (e.g. always pass `"/fixtures/multi_author.docx"` as `file_path`) so the
     goldens do not embed a machine-specific absolute path. Node tests pass the
     same placeholder string. This is the single largest source of spurious
     golden diffs â€” get it right in this task.
     Normalise line endings to `\n` on write, and have the Node loader compare
     after `.replace(/\r\n/g, "\n")`.
  3. `conformance.test.ts` â€” one `it` per golden case, each calling the Node
     builder that owns it. In this task **all of them fail** (the builders do not
     exist yet); mark the file with a top comment listing which task turns each
     case green:
     `range_*` â†’ Task 3, `ledger_*` â†’ Task 4, `guard_long5` â†’ Task 13,
     `search_*` â†’ Task 12, `outline_l1` â†’ Task 17.
     To keep the suite green after every task (plan rule), gate the whole file on
     an env flag for now: `describe.skipIf(!process.env.ADEU_CONFORMANCE)`, and
     **remove the gate in Task 22** once every case is implemented. Document the
     flag in `shared/conformance/README.md`.
  4. Token-budget assertions live in this file too (spec Â§8.3): ledger
     `<= 18` tokens/change on `dense_175`; minimal report `<= 40` tokens/edit
     (already unit-tested in Task 1 â€” re-assert here on a real batch); batch
     failure `<= 500` tokens; guard refusal `<= 800` tokens.
- **Done when**:
  - `node shared/conformance/build_fixtures.mjs` writes six `.docx` files;
    running it twice produces byte-identical files (`git status` clean on the
    second run).
  - `cd python && uv run python ../shared/conformance/capture_goldens.py` writes
    15 golden `.txt` files, each non-empty.
  - `cd node && npm run test` â€” zero failures (conformance file gated off).
  - `cd node/packages/mcp-server && ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts`
    fails with "not implemented"-style assertion errors only â€” never with
    missing-golden or crash errors.

---

# Phase 1 â€” P0 (spec Â§9: the turn-killers and the correctness gaps)

## Task 3 â€” A6: native page ranges (`page: "2-6"`)
- **Status**: COMPLETED

- **Goal**: one call returns up to 8 synthetic pages, with Python's exact
  banners, cap note and early-stop note.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/mcp-server/src/response-builders.ts` (add
    `build_page_range_response`; `_build_page_banner` at `:151`,
    `_build_appendix_pointer` at `:146` already exist and are reused verbatim)
  - modify `node/packages/mcp-server/src/index.ts` (`read_docx` handler,
    `:519-568`; description constant `READ_DOCX_TAIL` at `:215`)
  - create `node/packages/mcp-server/src/page-range.test.ts`
- **Test first** â€” `page-range.test.ts` (unit, calls the builder directly, no
  server spawn; build a multi-page body with the `makeMultiPageBody()` helper
  pattern from `response_builders.test.ts:18-35`):
  1. mid-range: `build_page_range_response(body, 2, 4, "/fixtures/x.docx")`
     returns page content for pages 2, 3, 4 in order; the text contains
     `> **Page 2 of N**`, `> **Page 3 of N**`, `> **Page 4 of N**` and no
     `> **Page 1 of N**`; blocks joined by `\n\n`.
  2. cap: with a â‰¥12-page body, `(1, 12)` renders exactly 8 pages and appends
     ``> **Range capped at 8 pages.** Continue with `page="9-12"`.``
  3. early stop: with a 5-page body, `(4, 20)` renders pages 4â€“5 and appends
     ``> **[range stopped at page 5: the document has 5 page(s)]**``
  4. cap note is absent when `last === end`; early-stop note is absent when
     `end <= total_pages`; the two notes are mutually exclusive.
  5. `start < 1` throws
     ``Invalid page number 0: page numbers must be positive integers.``
  6. `start > end` throws ``end page (2) cannot be less than start page (6)``
  7. `start > total_pages` throws ``Page 9 out of range (doc has 5 pages).``
  8. appendix pointer: a body containing the appendix marker yields a trailing
     appendix pointer paragraph (stripped, joined with `\n\n`); absent otherwise.
  9. `no_chrome: true` renders `[p2/5]\n\n` markers instead of banners, no
     File-Path line, no cap/appendix notes.
  10. token parity (spec A6 acceptance): the range response for pages 2â€“4 is
      shorter than three separate `build_paginated_response` calls concatenated
      (per-call chrome saved), and each page's `page_content` appears
      byte-identically in both.
  - Handler-level test in `page-range.test.ts` is not possible (the tools are
    registered at module import); add the schema/description assertions to the
    existing live-server suite instead: extend
    `node/packages/mcp-server/src/mcp.schema-gaps.test.ts` with a case asserting
    `read_docx`'s description mentions `'2-6'` (page ranges are discoverable) and
    a `tools/call` with `page: "2-3"` on a multi-page fixture returns two page
    banners.
- **Change**:
  1. In `response-builders.ts`, port `python/src/adeu/mcp_components/_response_builders.py:485-555`:
     ```ts
     export function build_page_range_response(
       text: string,
       start: number,
       end: number,
       file_path: string,
       bundle?: ProjectionBundle,
       no_chrome: boolean = false,
     ): ToolResult {
       if (start < 1)
         throw new Error(`Invalid page number ${start}: page numbers must be positive integers.`);
       if (start > end)
         throw new Error(`end page (${end}) cannot be less than start page (${start})`);
       const [body, appendix] = bundle ? [bundle.body, bundle.appendix] : split_structural_appendix(text);
       const has_appendix = Boolean(appendix.trim());
       const result = bundle ? bundle.pagination : paginate(body, "");
       const total_pages = result.total_pages;
       if (start > total_pages)
         throw new Error(`Page ${start} out of range (doc has ${total_pages} pages).`);
       const last = Math.min(end, start + PAGE_RANGE_MAX_PAGES - 1, total_pages);
       const page_blocks: string[] = [];
       for (let p = start; p <= last; p++) {
         const selected = result.pages[p - 1];
         const banner = no_chrome
           ? (selected.total_pages > 1 ? `[p${selected.page}/${selected.total_pages}]\n\n` : "")
           : _build_page_banner(selected.page, selected.total_pages);
         page_blocks.push(`${banner}${selected.page_content}`);
       }
       const ui_parts: string[] = [page_blocks.join("\n\n")];
       if (!no_chrome) {
         if (last < end && last < total_pages) {
           ui_parts.push(
             `> **Range capped at ${PAGE_RANGE_MAX_PAGES} pages.** Continue with \`page="${last + 1}-${end}"\`.`,
           );
         } else if (end > total_pages) {
           ui_parts.push(
             `> **[range stopped at page ${total_pages}: the document has ${total_pages} page(s)]**`,
           );
         }
         const pointer = _build_appendix_pointer(has_appendix);
         if (pointer) ui_parts.push(pointer.trim());
       }
       const ui_markdown = ui_parts.join("\n\n");
       const llm_content = no_chrome ? ui_markdown : `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
       return { content: [{ type: "text", text: llm_content }],
         structuredContent: { markdown: ui_markdown, file_path: resolve(file_path), title: basename(file_path) } };
     }
     ```
     Import `PAGE_RANGE_MAX_PAGES` from `@adeu/core` (Task 0).
  2. In `index.ts`'s `read_docx` handler, replace the ad-hoc page parsing at
     `:531-552` with `parse_page_arg` (Task 0), mirroring
     `python/src/adeu/mcp_components/tools/document.py:501-544`:
     - `search_query` present â†’ unchanged: pass the raw `page` to
       `build_search_response` (search keeps its own single-page/`all` semantics;
       a range string must fail there with search's own
       `Invalid page value: â€¦` message â€” covered in Task 12).
     - `mode === "appendix"`: `kind === "range"` â†’ error
       ``Page range pagination is only supported in 'full' mode, not 'appendix' mode.``;
       `kind === "all"` â†’ error
       ``Invalid page parameter: '<page>'. Provide a positive integer.``
     - `mode === "full"`, `kind === "all"` â†’ existing
       `build_full_document_response` (Task 13 adds the guard here).
     - `mode === "full"`, `kind === "range"` â†’ `build_page_range_response(text, start, end, file_path, bundle)`.
     - `kind === "single"` â†’ existing `build_paginated_response`.
     - Invalid page values: keep returning the `isError: true` text-content shape
       already used at `:539-549` (do not start throwing â€” existing tests assert
       that shape), but source the message from `parse_page_arg`'s error.
  3. Update `READ_DOCX_TAIL` (`index.ts:215`) so ranges are discoverable â€” the
     tool description is the only channel guaranteed to reach the model
     (comment at `:212-214`): replace the ``page`: a positive integerâ€¦` sentence
     with:
     ``\`page\`: a positive integer (1-indexed, default 1), a page RANGE like '2-6' (returns up to 8 pages in one call, then names the next range), or 'all'. Pages are synthetic length-based chunks sized for LLM consumption, NOT printed Word pages. In mode='full', page='all' returns the whole body with no page chrome. With \`search_query\`, \`page\` instead restricts matches to that page (default: search all pages).``
     Keep `PROCESS_BATCH_*` descriptions untouched; the 2048-char client
     truncation budget noted at `index.ts:218-224` applies to
     `process_document_batch`, but re-check `READ_DOCX_COMMON_DESC + READ_DOCX_TAIL + buildTag`
     stays under 2048 chars and assert it in the schema-gaps test.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/page-range.test.ts` â€” 10
    cases pass.
  - `cd node/packages/mcp-server && ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts -t "range_"`
    â€” `range_2_4`, `range_cap_1_12`, `range_past_end` match the Python goldens
    byte-for-byte.

## Task 4 â€” A1: the changes ledger builder
- **Status**: COMPLETED
- **Failed Verify Cycles**: 1
- **Attempt Ledger**:
  - attempt 1: implement changes ledger builder build_changes_response -> FAIL (comments_data object prototype lookup for IDs like "toString", and regex \w/\d ASCII vs Unicode digits/letters in TAG_RE/isDigits)
  - attempt 2: fix prototype pollution and Unicode digit/char matching in ledger -> PASS

- **Goal**: port `build_changes_response` so a review agent enumerates every
  tracked change and comment in one bounded call (â‰¤18 tokens/change).
- **Difficulty**: HARD (440 lines of bubble parsing; the pairing and
  snippet-attribution logic is subtle and golden-pinned).
- **Files**:
  - create `node/packages/mcp-server/src/ledger.ts`
  - create `node/packages/mcp-server/src/ledger.test.ts`
- **Test first** â€” `ledger.test.ts`, driven by projections of the Task 2
  fixtures (project with `_extractTextFromDoc(doc, false, false)` â€” the same
  raw projection `read_docx` uses; see `index.ts:478-486` for the call shape):
  1. header shape, exact three lines:
     ```
     > **Changes ledger** â€” 6 change(s), 3 comment(s) across 2 page(s).
     > Distribution â€” p1: 7, p2: 2
     > Authors â€” Acme LLP, Bob Smith, Jane Doe
     ```
     (authors sorted ascending; `none`/`None` fallbacks when empty).
  2. change line shape:
     `` Chg:12  ins  Jane Doe  p3  "â€¦snippetâ€¦"  (pairs Chg:13) `` â€” two spaces
     between fields, snippet in straight double quotes, whitespace collapsed to
     single spaces, snippet clamped at 48 chars as `slice(0,45) + "..."`.
  3. comment line shape:
     `` Com:5  Bob Smith  p2  "text"  (reply to Com:4) `` â€” comment text clamped
     at 120 chars as `slice(0,117) + "..."`.
  4. `ins`/`del`/`fmt` classification: explicit `[Chg:N insert|delete|format]`
     tags win; a tag with no type falls back to
     `del if any {--â€¦--} else fmt if any {==â€¦==} else ins`.
  5. pairing is symmetric: if the bubble says `(pairs with Chg:13)` then Chg:12
     lists `(pairs Chg:13)` **and** Chg:13 lists `(pairs Chg:12)`; multiple
     partners render sorted numerically, comma-separated.
  6. `existing_change_ids` filter: an id present in a bubble but absent from the
     engine's `_existing_change_ids()` is dropped, and an id present in the
     engine but not in any bubble appears with `del`, `Unknown`, `p1`, empty
     snippet (position 999999, i.e. last).
  7. `author_filter`: case-insensitive substring match on the author field;
     filtering also shrinks the header counts, the distribution and the roster.
  8. `page` filter: `3` keeps only entries on page 3; `"2-4"` keeps pages 2â€“4;
     `"all"`/omitted keeps everything; out-of-range single or range start throws
     ``Page 9 out of range (doc has 5 pages).``
  9. paging: on `dense_175`, `offset = 0` renders 300 entries max and, when more
     remain, appends
     ``> **Showing entries 1-300 of 412.** Continue with `read_docx(file_path="â€¦", mode="changes", changes_offset=300)`.``
     â€” argument order exactly `file_path`, `mode`, `changes_author?`, `page?`,
     `changes_offset`; a negative offset is coerced to 0.
  10. table-cell revisions appear as ordinary entries with the cell's page.
  11. token budget: on `dense_175`,
      `approxTokens(content) / changeCount <= 18`.
  12. ordering: entries sort by `(position, kind, numeric id)` â€” i.e. document
      order, changes before comments at the same position (`"chg" < "com"`).
  13. `no_chrome: true` emits only the entry lines; with **no** entries it emits
      the bare `` `N change(s), M comment(s)` `` line, never an empty string
      (`_response_builders.py:1796-1803`).
- **Change**: port `python/src/adeu/mcp_components/_response_builders.py:1372-1815`
  literally, in this order (each Python helper becomes a module-private TS
  function with the same name minus the leading underscore where exported):
  - `_parse_com_header` â† `:1372-1388`. The two Python regexes translate directly;
    `re.DOTALL` â†’ the `s` flag, `\Z` â†’ `$` (with no `m` flag JS `$` is
    end-of-string, so this is equivalent).
  - `LedgerEntry` interface â† `_LedgerEntry` `:703-713`
    (`kind, cid, change_type, author, page, snippet, pair_ids, reply_to_id, position`).
  - the bubble scan â† `:1421-1646`: iterate `/\{>>([\s\S]*?)<<\}/g` over the
    body; for each bubble compute `p_num = offset_to_page(b_start, page_offsets)`
    (Task 0 export), collect preceding wrapper snippets with
    `/(\{\+\+|\{--|\{==)([\s\S]*?)(?:\+\+\}|--\}|==\})/g` over
    `body.slice(Math.max(0, b_start - 100000), b_start)`, then the `TAG_RE`
    header-token logic **including** the `first_com_delim_pos` rule (a `[Chg:N]`
    inside a comment body counts as a header only when it opens its own line).
  - the shared-`rest` fill-down (`:1495-1509`), the per-type snippet indexing
    (`:1513-1554`), the `rfind` fallback when a type has no snippet
    (`:1556-1566`), the pairing extraction (`:1571-1586`), the author cleanup
    regex `/\s*\((?:pairs(?:\s+with)?|reply\s+to)\s+.*?\)/` (`:1588`), and the
    comment branch (`:1606-1646`).
  - the `comments_data` sweep for comments with no bubble (`:1648-1671`) and the
    `existing_change_ids` sweep (`:1673-1685`).
  - filtering, header, slicing at 300, continuation note, `no_chrome`
    (`:1687-1815`).
  Signature (MCP flavour only â€” `is_cli` is not ported; Assumption 2):
  ```ts
  export function build_changes_response(
    text: string,
    file_path: string,
    opts?: {
      comments_data?: Record<string, any> | null;
      author_filter?: string | null;
      page?: number | string | null;
      offset?: number;
      bundle?: ProjectionBundle;
      existing_change_ids?: Iterable<string> | null;
      no_chrome?: boolean;
    },
  ): ToolResult;
  ```
  Python-to-TS gotchas to respect:
  - Python `sorted(set(...))` on author strings is codepoint order; use
    `[...set].sort()` (JS default sort is UTF-16 codepoint order â€” equivalent for
    the fixture set; the unicode fixture pins it).
  - `float("inf")` â†’ `Number.POSITIVE_INFINITY`.
  - Python `dict` iteration order = insertion order; `Map` preserves it â€” use
    `Map`, never a plain object, for `chg_entries`/`com_entries`, because
    numeric-looking string keys in a JS object are reordered numerically and that
    would silently change output order.
  - `str.removeprefix("Chg:")` â†’ `s.startsWith("Chg:") ? s.slice(4) : s`.
  - `re.sub(r"\s+", " ", x).strip()` â†’ `x.replace(/\s+/g, " ").trim()`.
- **Done when**:
  - `cd node && npm run build`.
  - `cd node/packages/mcp-server && npx vitest run src/ledger.test.ts` â€” all 13
    cases pass.
  - `ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts -t "ledger_"` â€”
    all seven ledger goldens match byte-for-byte.
  - `cd node && npm run test` â€” zero failures.

## Task 5 â€” A1 wiring: `read_docx` gains `mode='changes'`, `changes_author`, `changes_offset`
- **Status**: COMPLETED
- **Failed Verify Cycles**: 1
- **Attempt Ledger**:
  - attempt 1: wire mode='changes' in read_docx -> FAIL (changes_offset schema missing .int() constraint allowing fractional floats like 1.5, and test case 6 in mcp.schema-gaps.test.ts used 1-page doc instead of multi-page fixture for page="2-3")
  - attempt 2: enforce integer constraint on changes_offset and fix multi-page test -> PASS

- **Goal**: make the ledger reachable, cheaply, from the MCP surface, with
  Python's exact parameter names and refusals.
- **Difficulty**: EASY (depends on Tasks 3 and 4)
- **Files**:
  - modify `node/packages/mcp-server/src/index.ts` (`read_docx` schema `:352-412`,
    handler `:416-580`, `READ_DOCX_TAIL` `:215`)
  - modify `node/packages/mcp-server/src/mcp.schema-gaps.test.ts` (advertise +
    live-call assertions)
- **Test first** â€” extend `mcp.schema-gaps.test.ts` (it already spawns the built
  server and speaks JSON-RPC; reuse `rpc`/`buildDoc` at `:61-115`):
  1. `tools/list` shows `read_docx.inputSchema.properties.mode.enum` containing
     `"changes"` alongside `full`/`outline`/`appendix`.
  2. `changes_author` and `changes_offset` are advertised, with
     `changes_offset` defaulting to 0.
  3. `read_docx.description` mentions `mode='changes'` (discoverability).
  4. `tools/call read_docx {mode:"changes"}` on a fixture with tracked changes
     returns text starting `> **File Path:**` and containing
     `> **Changes ledger** â€”` and at least one `Chg:` line.
  5. `mode:"changes"` + `clean_view:true` returns
     `isError: true` with text containing
     ``--clean-view cannot be used with mode='changes'.``
     (Python's wording, `tools/document.py:428`).
  6. `mode:"changes"` + `page:"2-3"` filters to those pages without error
     (contrast with `mode:"appendix"`, which rejects ranges).
- **Change** (mirror `python/src/adeu/mcp_components/tools/document.py:426-465`):
  1. Schema: `mode: z.enum(["full", "outline", "appendix", "changes"])`; add
     ```ts
     changes_author: z.string().optional()
       .describe("For mode='changes' only: filter tracked changes ledger by author name."),
     changes_offset: z.coerce.number().default(0)
       .describe("For mode='changes' only: entry offset for paginating tracked changes ledger."),
     ```
     Use `z.coerce.number()` for `changes_offset` to match the existing
     `outline_max_level` treatment (`:388-391`) â€” some clients send numbers as
     strings.
  2. Handler, placed **after** the `search_query` branch and **before** the
     `mode === "appendix"` branch (Python's order, which matters: a
     `search_query` wins over `mode`):
     ```ts
     if (mode === "changes") {
       if (clean_view) {
         return { isError: true, content: [{ type: "text",
           text: "Error executing tool read_docx: --clean-view cannot be used with mode='changes'." }] };
       }
       const entry2 = await getEntry();
       let comments_data: Record<string, any> | null = null;
       let existing_change_ids: string[] | null = null;
       try {
         const buf = readBytes();
         const doc = await loadDocxOrThrow(buf, file_path);
         comments_data = extract_comments_data(doc.pkg);
         existing_change_ids = new RedlineEngine(doc)._existing_change_ids_public();
       } catch {
         // Best-effort enrichment, exactly as Python (document.py:436-451):
         // a ledger without comment authors still beats no ledger.
       }
       const res = build_changes_response(entry2.raw_text, file_path, {
         comments_data,
         author_filter: changes_author ?? null,
         page: page ?? null,
         offset: changes_offset,
         bundle: entry2.raw_bundle,
         existing_change_ids,
       });
       return res as any;
     }
     ```
  3. `_existing_change_ids` is **private** in `engine.ts:4457`. Do not widen it
     casually: add a narrow public accessor next to it and use that everywhere
     (Task 21 also needs it):
     ```ts
     /** Public, read-only view of the document's tracked-change ids â€” the ledger
      *  (A1) filters against it so a stale bubble id never reaches the agent. */
     public existing_change_ids(): string[] { return this._existing_change_ids(); }
     ```
     and export nothing new from `core/src/index.ts` (`RedlineEngine` is already
     exported).
  4. `READ_DOCX_TAIL`: add the mode line
     ``- 'changes': a ledger of every tracked change and comment (id, type, author, page, snippet) â€” start here for review work instead of reading pages. Filter with changes_author, page, and changes_offset.``
     Re-assert the 2048-char description budget.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/mcp.schema-gaps.test.ts`
    â€” the six new cases pass.
  - Manual sanity (record the output in the task's commit message):
    `node node/packages/mcp-server/dist/index.js` is not needed â€” instead assert
    via the test above; do not add a manual step to the definition of done.

## Task 6 â€” B9: uniform failure envelope with 0-based machine-readable blame
- **Status**: COMPLETED

- **Goal**: every batch failure â€” schema-level and engine-level â€” carries
  `{"error": code, "failed": [{"index": <0-based>, "reason": â€¦}], "message": â€¦}`,
  and the indices refer to positions in the **caller's `changes` array**, not to
  a per-bucket counter.
- **Difficulty**: HARD (the index-space fix touches the batch orchestrator and
  every error string that names an edit or action number).
- **Files**:
  - modify `node/packages/core/src/engine.ts` (`BatchValidationError` `:190-197`;
    `process_batch` `:3385-3415`; `_process_batch_internal` `:3417-3665`;
    `apply_review_actions` `:4591-4699`; `validate_review_actions` `:3280+`;
    `validate_action_pairing`)
  - modify `node/packages/mcp-server/src/index.ts` (batch handler `:830-919`)
  - create `node/packages/core/src/engine.failure-envelope.test.ts`
  - create `node/packages/mcp-server/src/batch-envelope.test.ts`
- **Test first**:
  - `engine.failure-envelope.test.ts` (core):
    1. `extract_failed_indices(["- Edit 5 Failed: Target text not found in document:\n  \"x\""])`
       â†’ `[[4, 'Target text not found in document:\n  "x"']]` â€” index is
       `N - 1`, reason is everything after the first `"Failed: "`.
    2. Matches `Action 3`, `Note: Action 2`, case-insensitively; an unmatched
       string yields `[0, <trimmed whole string>]`
       (`python/src/adeu/redline/engine.py:70-83`).
    3. `new BatchValidationError(["- Edit 2 Failed: boom"]).failed` â†’
       `[{index: 1, reason: "boom"}]`-equivalent tuple list; an explicitly
       passed `failed` argument overrides derivation.
    4. **Index space**: a batch `[accept(bad id), modify(bad target)]` produces
       errors naming `Action 1` and `Edit 2` (not `Edit 1`), and
       `stats.failed`/`error.failed` indices are `[0, 1]`. This is the parity fix
       â€” today Node numbers each bucket from 1 (`engine.ts:3545` passes the
       edits-array index; `:4622` passes the actions-array index).
    5. A three-element batch `[modify(ok), accept(bad), modify(bad)]` reports
       failures at indices `1` and `2`.
    6. `process_batch(changes, original_indices)` honours an explicit index map:
       `original_indices = [3, 7]` makes the two edits report as `Edit 4` and
       `Edit 8` with `failed` indices `[3, 7]` (this is what the MCP
       schema-salvage path needs in Task 7).
  - `batch-envelope.test.ts` (mcp-server, live server via the
    `mcp.schema-gaps.test.ts` RPC harness â€” extract that harness into
    `node/packages/mcp-server/src/test-rpc.ts` if duplicating it would exceed
    ~40 lines, otherwise copy the 50-line block; prefer extraction):
    7. A batch whose only edit has an unmatchable `target_text` returns
       `isError: true`, text starting `Batch rejected. Some edits failed
       validation:` and ending with a fenced ```json block whose parsed value is
       `{error:"batch_validation_failed", failed:[{index:0, reason:â€¦}], message:â€¦}`.
    8. `message` ends with the exact `BATCH_RECOVERY_PROTOCOL` sentence (B2).
    9. Budget: for a 20-edit batch with exactly 1 bad edit,
       `approxTokens(responseText) <= 500` (spec B2/Â§8.3).
    10. A malformed-`type` batch (the `typeErrors` path at `index.ts:806-829`)
        returns an envelope with `error: "invalid_changes_file"` and one
        `failed[]` entry per malformed index, 0-based.
- **Change**:
  1. `core/src/engine.ts`:
     - add, next to `BatchValidationError`:
       ```ts
       /** Mirrors python/src/adeu/redline/engine.py _extract_failed_indices (:70-83). */
       export function extract_failed_indices(errors: string[]): [number, string][] {
         const pattern = /^-\s*(?:Action|Edit|Note: Action)\s+(\d+)\b/i;
         const failed: [number, string][] = [];
         for (const err of errors) {
           const first_line = err ? err.split("\n")[0] : "";
           const m = pattern.exec(first_line);
           if (m) {
             const idx = parseInt(m[1], 10) - 1;
             const parts = err.split("Failed: ");
             const reason = parts.length > 1 ? parts.slice(1).join("Failed: ").trim() : err.trim();
             failed.push([idx, reason]);
           } else {
             failed.push([0, err.trim()]);
           }
         }
         return failed;
       }
       ```
       Note `split("Failed: ", 1)` in Python keeps the remainder; JS `split` with
       a limit truncates â€” hence the `slice(1).join(...)` above.
     - `BatchValidationError` gains a second constructor argument:
       ```ts
       constructor(errors: string[], failed?: [number, string][]) {
         super("Batch validation failed:\n" + errors.join("\n"));
         this.name = "BatchValidationError";
         this.errors = errors;
         this.failed = failed ?? extract_failed_indices(errors);
       }
       public failed: [number, string][];
       ```
     - `process_batch(changes, original_indices?: number[], partial: boolean = false)`
       â€” signature parity with `engine.py:2551-2556`; pass both through to
       `_process_batch_internal`. `partial` is wired in Task 7; in this task it is
       accepted and ignored except for being threaded through (keep the parameter
       so Task 7 is a small diff).
     - In `_process_batch_internal`, build index-carrying buckets before the
       existing `actions`/`edits` split (mirror `engine.py:2675-2687`):
       ```ts
       const idx_of = (i: number) => (original_indices ? original_indices[i] : i);
       const actions_with_idx = changes.map((c, i) => ({ c, i: idx_of(i) }))
         .filter(({ c }) => c !== null && typeof c === "object" && ["accept","reject","reply"].includes((c as any).type));
       const edits_with_idx = changes.map((c, i) => ({ c, i: idx_of(i) }))
         .filter(({ c }) => c === null || typeof c !== "object" || !["accept","reject","reply"].includes((c as any).type));
       ```
       Then: `validate_review_actions(actions, action_indices)`,
       `validate_action_pairing(actions, action_indices)`,
       `apply_review_actions(actions, action_indices)` all take an optional
       `indices?: number[]` and use `indices ? indices[pos] : pos` wherever they
       currently write `pos + 1`; `validate_edits([edit], orig_idx)` replaces
       `validate_edits([edit], i)` at `:3545`; the apply-stage fallback message at
       `:3573-3575` uses `orig_idx + 1`.
     - Collect `failed_list: [number, string][]` alongside the existing
       `sequential_errors`, and pass it to every `throw new BatchValidationError(...)`
       in the method (`:3474`, `:3486`, `:3585`), using
       `extract_failed_indices` for the action paths (which is what Python does at
       `engine.py:2693,2700,2706`) and the known `orig_idx` for the per-edit paths.
     - Add to the returned stats object (`:3642-3664`):
       `status: "ok"` (Task 7 makes it `"partial"` when applicable) and
       `failed: failed_list.map(([index, reason]) => ({ index, reason, error: reason }))`
       â€” three keys, exactly as `engine.py:2864`.
     - `core/src/index.ts`: export `extract_failed_indices`.
  2. `mcp-server/src/index.ts` batch handler:
     - Replace the prose-only rejection at `:866-874` with:
       ```ts
       const env = failure_envelope(
         "batch_validation_failed",
         (e as BatchValidationError).failed,
         "Batch rejected. Some edits failed validation.",
         e.errors,
       );
       return { isError: true, content: [{ type: "text", text:
         "Batch rejected. Some edits failed validation:\n\n" + e.errors.join("\n\n") +
         "\n\n```json\n" + JSON.stringify(env) + "\n```" }] };
       ```
       (shape and wording from `python/src/adeu/mcp_components/tools/document.py:710-717`;
       `JSON.stringify` without spacing matches Python's default separators
       closely enough that the goldens compare on the parsed object, not the raw
       string â€” assert the parsed object in tests).
     - Do the same for the `typeErrors` branch at `:819-829` with code
       `"invalid_changes_file"`, indices from the loop counter `i` (already
       0-based â€” keep it; the human line stays `- Change ${i + 1}: â€¦`).
     - Import `failure_envelope` from `@adeu/core`.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures. Existing tests
    that assert the old prose-only rejection text still pass (the prose is
    unchanged; the JSON block is appended). If any test asserts the response
    ends with the prose, update that single assertion and note it in the commit.
  - `cd node/packages/core && npx vitest run src/engine.failure-envelope.test.ts`
    â€” six cases pass.
  - `cd node/packages/mcp-server && npx vitest run src/batch-envelope.test.ts` â€”
    four cases pass, including the â‰¤500-token budget.

## Task 7 â€” B5: explicit-salvage contract (`partial`) with a PARTIAL-leading response
- **Status**: COMPLETED

- **Goal**: apply everything that validates, make the failures impossible to
  miss, and keep strict all-or-nothing available.
- **Difficulty**: HARD (transactional semantics; integrity guardrail in
  `docs/improvement_spec.md` Â§10 applies â€” no silent partial application).
- **Files**:
  - modify `node/packages/core/src/engine.ts` (`_process_batch_internal`
    `:3417-3665`)
  - modify `node/packages/mcp-server/src/index.ts` (batch tool schema + handler)
  - create `node/packages/core/src/engine.partial.test.ts`
  - modify `node/packages/core/src/engine.atomic.test.ts` (exists â€” confirm the
    atomic contract still holds; extend, do not weaken)
- **Test first** â€” `engine.partial.test.ts` (core, using `createTestDocument`/
  `addParagraph` from `src/test-utils.ts`):
  1. `partial: false` (default) unchanged: a 2-edit batch with one bad target
     throws `BatchValidationError`, the document is byte-identical to before, and
     `error.failed` names index 1.
  2. `partial: true`, same batch: **no throw**; the good edit is in the document;
     `stats.status === "partial"`; `stats.edits_applied === 1`;
     `stats.edits_skipped === 1`; `stats.failed === [{index: 1, reason: â€¦, error: â€¦}]`;
     the failed edit's report has `status: "failed"` and its full `error`.
  3. `partial: true` with **all** edits bad: `status === "partial"`,
     `edits_applied === 0`, and the caller (MCP layer, case 8) turns that into a
     failure envelope rather than a PARTIAL success.
  4. Sequential dependency: `[modify Aâ†’B, modify Bâ†’C]` where the first fails â€”
     the second is reported as failed too (its target never appeared), with both
     failures listed coherently and no rollback of unrelated applied edits.
  5. A skipped **action** (`accept` on a stale id) with `partial: true` does not
     throw (`engine.py:2705-2709`), is counted in `actions_skipped`, and appears
     in `failed` with its original index.
  6. Integrity: after any `partial: true` run, `doc.save()` produces a document
     that loads cleanly and whose applied edit is a real tracked change (assert
     the `w:ins`/`w:del` exists, as `engine.batch.test.ts` does).
  7. `stats.status === "ok"` when nothing failed, in both modes.
- **Test first** â€” MCP side, extend `batch-envelope.test.ts`:
  8. Default `partial` is `true` on the tool (`tools/list` shows the default),
     and a 2-edit batch with one bad edit returns a **success** response
     (`isError` unset) whose text begins:
     ```
     PARTIAL: applied 1 of 2 changes. 1 failed validation:

     - Change #2 Failed: <reason>
     ```
     followed by `Batch complete. Saved to: <path>` â€” and the output file exists
     on disk with the good edit applied.
  9. `partial: false` on the same batch returns the Task 6 failure envelope and
     writes **no** output file.
  10. When `partial: true` but nothing applied, the response is the failure
      envelope (never a PARTIAL header claiming a saved file) â€”
      `tools/document.py:645-661,739-743`.
- **Change**:
  1. `engine.ts` `_process_batch_internal` (mirror `engine.py:2705-2860`):
     - Action skips: `if (skipped_actions > 0) { failed_list.push(...extract_failed_indices(this.skipped_details)); if (!partial) throw new BatchValidationError(this.skipped_details, failed_list); }`
     - Edit loop: keep collecting `sequential_errors` + `failed_list`; append the
       `sequential_context_hint` only when `applied_so_far > 0 && !partial`
       (Python gates the hint on `not partial`, `engine.py:2811`).
     - The apply-stage failure `break` at `:3580` becomes
       `if (!partial) break;` â€” in salvage mode continue to the next edit, as
       Python does. **Risk noted in Risks Â§**: Node's comment at `:3567-3572`
       warns later edits then validate against a partially mutated document;
       Python accepts this and the per-edit reports carry the outcome. Test 4
       and test 6 exist to pin that this stays coherent.
     - Wrap-up: `if (!partial && sequential_errors.length > 0) { restore snapshot; throw â€¦ }`
       (today's behaviour). With `partial: true`, skip the throw and the restore.
     - `status: (partial && failed_list.length > 0) ? "partial" : "ok"`.
  2. `mcp-server/src/index.ts`:
     - Schema: add
       ```ts
       partial: z.boolean().default(true)
         .describe("Whether to apply valid edits when some fail (salvage mode). Defaults to true."),
       ```
       (wording from `tools/document.py:1511-1514`).
     - Handler: `stats = engine.process_batch(sanitizedChanges, undefined, partial)`.
     - After a successful `process_batch`, compute the partial header before the
       existing `formatBatchResult` call, porting
       `tools/document.py:734-770`:
       ```ts
       const applied_count = (stats.edits_applied || 0) + (stats.actions_applied || 0);
       const engine_failed: Array<{ index: number; reason: string }> = stats.failed || [];
       const is_partial_success = partial && engine_failed.length > 0 && applied_count > 0;
       let partial_header = "";
       if (is_partial_success) {
         const fails = [...engine_failed].sort((a, b) => a.index - b.index);
         const max_idx = fails.reduce((m, f) => Math.max(m, f.index), 0);
         const total_n = Math.max(max_idx + 1, sanitizedChanges.length);
         partial_header = `PARTIAL: applied ${applied_count} of ${total_n} changes. ${fails.length} failed validation:\n\n`;
         for (const f of fails) partial_header += `- Change #${f.index + 1} Failed: ${f.reason}\n`;
         partial_header += "\n";
       }
       ```
     - When `partial` is true but `applied_count === 0` and failures exist,
       return the Task 6 failure envelope instead of writing output â€” check this
       **before** `doc.save()`/`writeFileSync` so no file is produced.
     - Mention salvage in `PROCESS_BATCH_COMMON_DESC` (`index.ts:225-226`): the
       current text promises "Any validation failure rejects the whole batch
       transactionally â€” nothing is applied", which becomes a lie under the new
       default. Replace that clause with:
       ``By default valid changes are applied even if others fail (salvage): the response then LEADS with `PARTIAL: applied K of N` and lists every change that did NOT land â€” resubmit only those, corrected. Pass partial=false for strict all-or-nothing.``
       Re-check the 2048-char budget for
       `PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC + buildTag` and
       assert it in `mcp.schema-gaps.test.ts` (a case already exists for the
       client-compat budget â€” extend it).
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures;
    `engine.atomic.test.ts` still passes untouched in substance.
  - `cd node/packages/core && npx vitest run src/engine.partial.test.ts` â€” seven
    cases pass.
  - `cd node/packages/mcp-server && npx vitest run src/batch-envelope.test.ts` â€”
    the three new cases pass.

## Task 8 — B1/E2: minimal report rendering (drop the duplicate preview, add the comment)
- **Status**: COMPLETED
- **Failed Verify Cycles**: 3
- **Attempt Ledger**:
  - attempt 1: implement minimal batch report rendering -> FAIL (formatter.qa.test.ts missing trailing newline at EOF, report-minimal.test.ts test name misstated measured token budget as 57 instead of 47)
  - attempt 2: fix trailing newline and update token budget test description -> FAIL (report-minimal.test.ts double trailing newline at EOF)
  - attempt 3: ensure single trailing newline in report-minimal.test.ts -> FAIL (formatBatchResult renders 84 tokens/edit for 200-char previews on raw stats, breaching <= 60 tokens/edit budget ceiling)
  - attempt 4 (escalation): step 6's `<= 60` rendered ceiling corrected to `<= 85`; test 6 now measures raw stats with a true 200-char preview -> PASS

- **Deviation (attempt 4, step 6 ceiling)**: step 6's `<= 60` tokens/edit is
  arithmetically unreachable under step 3 and Python parity, so the ceiling — not
  the renderer — was wrong. A 200-char CriticMarkup preview is 50 approx-tokens
  and `*Preview (CriticMarkup):*\n> ` adds 7; even with `**Path:**` and
  `**Mode:**` dropped, one edit costs 64. The full render is 84 (edit header 7,
  path 5, mode 10). The three ways to reach 60 were all rejected:
  1. clamping `report.critic_markup` in `formatBatchResult` contradicts
     `tools/document.py:813-821`, where Python renders the preview in full on
     purpose ("a shortened preview is not verification, and a cut through a
     bubble is not even valid CriticMarkup") — a Node-only clamp breaks the
     Dual-Engine Parity invariant. `clamp_text(markup, PREVIEW_TEXT_CAP)`
     specifically is a no-op here: `PREVIEW_TEXT_CAP` is 200
     (`core/src/utils/text.ts:15`, `utils/text.py:16`).
  2. wiring `shrink_batch_stats` into the renderer is forbidden by step 3, and
     in Python it is opt-in and JSON-only (`cli.py:1465` under `--report
     minimal`, `serve.py:345` under `report_style`) — never applied to rendered
     markdown.
  3. measuring the budget on `shrink_batch_stats(rawStats)` (attempt 3) scored a
     shape the MCP tool never emits.
  The `<= 40` tokens/edit JSON budget of Task 1 is untouched and still enforced
  at `conformance.test.ts:239`. Observation for a follow-up task, deliberately
  not fixed here: `formatBatchResult` emits `*Error:*` before `*Warning:*` while
  `tools/document.py:809-812` emits warning first, so step 1's parenthetical
  ("Node already does") is wrong; and `index.ts:1448` splits the preview on the
  literal two-character sequence `\n` rather than a newline, so multi-window
  previews are not re-prefixed with `> `. Both predate this task.

- **Goal**: the MCP batch report stops echoing the caller's input and stops
  billing twice for one span, matching Python's MCP renderer.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/mcp-server/src/index.ts` (`formatBatchResult`
    `:1228-1301`)
  - modify `node/packages/core/src/engine.ts` (`version` field `:3663`)
  - modify `node/packages/core/tsup.config.ts` (version define)
  - create `node/packages/mcp-server/src/report-minimal.test.ts`
  - modify whichever existing tests assert the two-preview shape â€” expect
    `node/packages/mcp-server/src/formatter.test.ts` and
    `formatter.qa.test.ts` (both small; read them first)
- **Test first** â€” `report-minimal.test.ts` (pure unit on `formatBatchResult`,
  which is already exported at `index.ts:1228`; importing it does spin up the
  module's server registration, which existing tests already tolerate â€” see
  `formatter.test.ts`):
  1. An applied edit renders `### Edit 1 âœ… [applied] (p1)`, `**Path:** â€¦`,
     ``**Mode:** `strict` (1 occurrence modified)``, and exactly **one**
     `*Preview (CriticMarkup):*` block. `*Preview (Clean):*` must not appear
     anywhere in the output.
  2. An edit carrying `comment` renders `**Comment:** "<text>"` between the Mode
     line and any warning (`tools/document.py:806-807`).
  3. `occurrences_modified: 0` on an applied edit renders
     `(0 occurrences modified)` â€” pluralisation follows `occ !== 1`
     (Python semantics, `tools/document.py:797-799`), replacing Node's current
     `report.occurrences_modified || (status === "applied" ? 1 : 0)` fudge at
     `:1268-1270`.
  4. A failed edit renders `âŒ [failed]` and its full `*Error:*` text.
  5. `stats.author_impersonation_warning`, when present, renders as
     `*Warning:* <text>` immediately after the `Saved to:` line
     (`tools/document.py:772-774`). Task 18 makes the engine produce it; this
     task only renders it if present, so the assertion uses a hand-built stats
     object.
  6. Token budget: for a stats object with 10 applied edits carrying 200-char
     previews, `approxTokens(formatBatchResult(...)) / 10 <= 60` â€” the rendered
     markdown is bulkier than the JSON `shrink_batch_stats` shape, so 40 is the
     JSON budget (Task 1) and 60 the rendered ceiling. Record the measured value
     in the test name so a regression is visible.
  7. `stats.version` is a non-empty string and is **not** `"1.18.2"`.
- **Change**:
  1. `formatBatchResult`:
     - delete the `report.clean_text` block (`:1283-1285`);
     - always emit the Mode line with Python's occurrence wording:
       ```ts
       const occ = report.occurrences_modified ?? 0;
       res += `**Mode:** \`${report.match_mode || "strict"}\` (${occ} occurrence${occ !== 1 ? "s" : ""} modified)\n`;
       ```
     - add the comment line after Mode:
       ```ts
       if (report.comment) res += `**Comment:** "${report.comment}"\n`;
       ```
     - keep the existing `warning`/`error` order (Python emits warning then
       error â€” Node already does);
     - after the `Saved to:`/counters lines, insert the impersonation warning
       when `stats.author_impersonation_warning` is set;
     - keep the `Notes:` vs `Skipped Details:` header logic at `:1290-1299`
       (Python's `batch_details_header` does the same) but prefix it with a blank
       line (`res += "\n\n" + header + â€¦`) to match `tools/document.py:825`.
  2. `engine.ts:3663`: `version: CORE_VERSION,` where
     ```ts
     // core/src/version.ts
     /** Injected by tsup `define` at build time (mirrors mcp-server/tsup.config.ts);
      *  "unknown" when running the TS sources directly, e.g. under vitest. */
     export const CORE_VERSION = process.env.ADEU_CORE_VERSION || "unknown";
     ```
     and in `core/tsup.config.ts` add
     ```ts
     import { readFileSync } from "node:fs";
     const pkg = JSON.parse(readFileSync("package.json", "utf-8"));
     // â€¦inside defineConfig:
     define: { "process.env.ADEU_CORE_VERSION": JSON.stringify(pkg.version) },
     ```
     This keeps `scripts/bump.py` unchanged (it already bumps
     `core/package.json`) and makes drift impossible.
  3. Do **not** wire `shrink_batch_stats` into the MCP renderer: the rendered
     markdown IS the minimal form here (Assumption 4). Add a one-line comment in
     `formatBatchResult` saying so and pointing at `payloads.ts` for structured
     consumers, so the next reader does not "fix" it.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures. Any existing
    assertion on `*Preview (Clean):*` is deleted (that is the behaviour being
    removed); list the touched test files in the commit message.
  - `cd node/packages/mcp-server && npx vitest run src/report-minimal.test.ts` â€”
    seven cases pass.
  - `node -e "process.stdout.write(String(require('./node/packages/core/dist/index.cjs').identifyEngine()))"`
    still prints `adeu-core-node` (smoke check that the new `define` did not
    break the CJS build).

## Task 9 â€” B3: comment-only `modify` normalised at the Node MCP boundary
- **Status**: COMPLETED
- **Failed Verify Cycles**: 3
- **Attempt Ledger**:
  - attempt 1: normalize comment-only modify in coerceChangeItemInPlace and write tests -> FAIL (verifier audit: test suite must strictly prove boundary normalization is active and necessary for coerceChangeItemInPlace and schema validation)
  - attempt 2: rewrite comment-only-modify.test.ts to test unit functions directly -> FAIL (verifier audit: integration tests were removed, leaving criterion 7 unasserted on saved XML; all 6 specified cases needed)
  - attempt 3 (escalated): keep 4 unit test cases and restore 2 live RPC test cases in comment-only-modify.test.ts -> FAIL (verifier audit: test comment misstated causal location and new_text:null live boundary test missing)
  - attempt 4 (escalated fix): keep all 6 test cases, update comment explaining dual-normalization, and add 7th live RPC case for new_text:null boundary coercion -> PASS



- **Goal**: a `{"type":"modify","target_text":â€¦,"comment":â€¦}` batch behaves
  identically on Node MCP, Python MCP and the Python CLI â€” including the
  heading-hash handling that only runs when `new_text` is present.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/mcp-server/src/index.ts`
    (`coerceChangeItemInPlace` `:58-86`; `CHANGE_ITEM_SCHEMA` `new_text`
    description `:607-612`; `PROCESS_BATCH_OPERATIONS_DESC` `:227-228`)
  - create `node/packages/mcp-server/src/comment-only-modify.test.ts`
- **Test first** â€” `comment-only-modify.test.ts`:
  1. Unit on the exported coercion (export `coerceChangeItemInPlace` if it is not
     already exported â€” it is currently module-private at `index.ts:58`; export it,
     no behaviour change): `{type:"modify", target_text:"X", comment:"why"}`
     becomes `{â€¦, new_text:"X"}`.
  2. `new_text: ""` is **untouched** (empty string means delete;
     delete-with-explanation is a legitimate distinct intent â€”
     `python/src/adeu/models.py:441-443`).
  3. `comment` whitespace-only, or absent â†’ `new_text` stays absent (so the
     engine's "modify requires new_text" error still fires,
     `engine.ts:2824-2828`).
  4. `type` absent with `target_text` + `comment` â†’ **no** inference (ambiguous
     with `delete_row`, `models.py:410-414`); the existing
     `typeErrors` boundary guard reports it per index.
  5. Heading parity: `{type:"modify", target_text:"## Term", comment:"why"}`
     normalised at the boundary reaches
     `_process_batch_internal`'s `stripMatchingHeadingHashes` (`engine.ts:3419-3433`)
     with both fields present â€” assert via a live batch on a fixture whose body
     has a `## Term` heading that the resulting document carries the comment and
     no tracked deletion of the heading text.
  6. Regression (the guardrail from spec Â§10): a comment-only modify never
     produces a `w:del` â€” assert the saved XML has no `w:del` for the target text.
- **Change**:
  1. In `coerceChangeItemInPlace`, after the `match_mode` normalisation, add the
     port of `_normalize_comment_only_modify_in_place`
     (`python/src/adeu/models.py:427-454`):
     ```ts
     // MCP-boundary tolerance (parity with Python models.py:427): the published
     // schema makes new_text optional, so a schema-following model that only
     // wants to annotate sends target_text + comment. The lossless reading is
     // the pure-comment form (new_text == target_text) â€” never a bounce, and
     // never a tracked deletion. An explicit "" is left alone: empty means
     // delete, and delete-with-rationale is a distinct intent.
     if (item.type === "modify" && (item.new_text === undefined || item.new_text === null)) {
       const target = item.target_text;
       const comment = item.comment;
       if (typeof target === "string" && target && typeof comment === "string" && comment.trim()) {
         item.new_text = target;
       }
     }
     ```
     The engine-level normalisation at `engine.ts:2817-2831` stays as the
     defence-in-depth path for direct library callers â€” do not remove it.
  2. Update the `new_text` field description in `CHANGE_ITEM_SCHEMA` to state the
     rule: append
     ``Omit it (with a `comment`) to annotate without changing the text; an explicit empty string deletes.``
  3. Add the same sentence to `PROCESS_BATCH_OPERATIONS_DESC`'s `'modify'` bullet
     â€” clients drop parameter descriptions in transit (`index.ts:212-214`), so the
     prose is the channel that reaches the model. Re-check the 2048-char budget.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/comment-only-modify.test.ts`
    â€” six cases pass.

## Task 10 â€” B4: honour `comment` on accept/reject  âš  PARITY-LEADING
- **Status**: COMPLETED


- **Goal**: a rejection or acceptance rationale becomes a visible margin comment
  instead of being silently discarded, and the resulting comment id is reported.
- **Difficulty**: HARD (DOM anchoring after the anchored content has just been
  resolved; the fallback path is the whole point)
- **âš  Release gate**: Python has **not** implemented B4
  (`python/src/adeu/models.py:169,179` still document `comment` as "Optional
  rationale"; `apply_review_actions` never reads it). Implementing it on Node
  first creates a deliberate, temporary divergence. **This task's commit message
  must state that 2.3.0 may not be published until the Python mirror lands**, and
  Task 22's release checklist re-asserts it. If the Python mirror is not planned,
  stop after the test in step 1 below (which proves the drop) and report â€” do not
  ship a one-engine semantic.
- **Files**:
  - modify `node/packages/core/src/engine.ts` (`apply_review_actions`
    `:4591-4699` and the accept/reject resolution block that follows;
    `_attach_comment` `:1733`)
  - modify `node/packages/mcp-server/src/index.ts` (`CHANGE_ITEM_SCHEMA`
    `comment` description `:620-625`)
  - create `node/packages/core/src/engine.review-comment.test.ts`
- **Test first** â€” `engine.review-comment.test.ts`:
  1. **Reproduce the drop first** (systematic-debugging rule): build a document
     with a tracked insertion by "Jane Doe", run
     `process_batch([{type:"reject", target_id:"Chg:1", comment:"out of scope"}])`,
     save, reload, and assert `extract_comments_data(doc.pkg)` is empty. This test
     is then inverted in the same commit â€” keep it as the regression assertion
     that the comment IS written.
  2. accept-with-comment on an insertion: after the batch, the reloaded document
     has exactly one comment whose `author` is the acting author and whose `text`
     is the rationale, anchored inside the paragraph that held the change
     (assert the paragraph contains a `w:commentRangeStart`).
  3. reject-with-comment on an **insertion** (the hard case â€” rejecting an
     insertion deletes the anchored text): the comment still exists and is
     anchored to a surviving run of the host paragraph
     ("nearest surviving run boundary", spec B4).
  4. reject-with-comment where the host paragraph is left with **no runs at all**:
     no crash, no comment, and a `- Note: Action N â€¦` line explains that the
     rationale could not be anchored. (Integrity over cleverness: never attach to
     an unrelated paragraph.)
  5. The comment id is reported: `stats.skipped_details` contains
     `- Note: Action 1 ('reject' on Chg:1) â€” rationale recorded as Com:<id>.`
     and `formatBatchResult` renders it under `Notes:` (the existing
     all-notes branch at `index.ts:1290-1299` already routes `- Note:` lines
     there).
  6. No comment field â†’ byte-identical behaviour to today (guard against
     regressing the review path): a document processed with
     `{type:"accept", target_id:"Chg:1"}` has no comments.
  7. `reply` actions are untouched (they already create comments through
     `comments_manager.addComment`).
- **Change**:
  1. `_attach_comment` returns the new comment id:
     `private _attach_comment(...): string | null` â€” return `comment_id` on
     success and `null` on every early return. All existing call sites ignore the
     return value, so this is additive.
  2. In `apply_review_actions`, inside the accept/reject branch:
     - **Before** resolving, capture the host paragraph:
       ```ts
       const host_p = (() => {
         let curr: Node | null = all_nodes[0] ?? direct_ppc[0] ?? null;
         while (curr) {
           if (curr.nodeType === 1 && (curr as Element).tagName === "w:p") return curr as Element;
           curr = curr.parentNode;
         }
         return null;
       })();
       ```
       (the same upward walk `_column_count_at` uses at `engine.ts:3262-3275`).
     - **After** a successful resolution (`applied++` path) and only when
       `action.comment && String(action.comment).trim()`:
       ```ts
       let anchor: Element | null = null;
       if (host_p) {
         anchor = Array.from(host_p.childNodes).find(
           (n) => (n as Element).tagName === "w:r",
         ) as Element | undefined ?? null;
       }
       if (host_p && anchor) {
         const cid = this._attach_comment(host_p, anchor, anchor, String(action.comment));
         if (cid) {
           this.skipped_details.push(
             `- Note: Action ${pos_label} ('${type}' on ${action.target_id}) â€” rationale recorded as Com:${cid}.`,
           );
         }
       } else {
         this.skipped_details.push(
           `- Note: Action ${pos_label} ('${type}' on ${action.target_id}) â€” the rationale could not be anchored ` +
           `(the resolved text left no surviving run); the ${type} itself succeeded.`,
         );
       }
       ```
       `pos_label` is the batch-global 1-based index from Task 6.
     - These are `- Note:` lines, so they land in the `Notes:` section and never
       inflate `actions_skipped` (the counters must keep meaning what they say â€”
       spec Â§10).
  3. MCP schema: change the `comment` field description from the current
     `"modify / accept / reject: attach a margin commentâ€¦"` to name where it
     lands: ``modify: attach a margin comment to the edited text. accept / reject: record the rationale as a margin comment anchored where the change was resolved (reported as Com:N).``
  4. Parity note in code: add a one-line comment above the new block â€”
     `// B4 (docs/improvement_spec.md Â§4): Node leads here; the Python mirror is required before release.`
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/core && npx vitest run src/engine.review-comment.test.ts`
    â€” seven cases pass, and the case-1 reproduction is present as the inverted
    regression assertion.
  - The commit message states the Python-mirror release gate verbatim.

---

# Phase 2 â€” P1 (cheap, high leverage, no semantic risk)

## Task 11 â€” E1: make `reasoning` optional on every tool
- **Status**: COMPLETED

- **Goal**: stop taxing every single call with a mandatory output-token string
  that is deleted unused.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/mcp-server/src/index.ts` (`reasoning` at `:353-357`,
    `:689-693`, `:934-938`, `:1043-1047`, `:1126-1130`)
  - modify `node/packages/mcp-server/src/mcp.schema-gaps.test.ts`
- **Test first** â€” extend `mcp.schema-gaps.test.ts`:
  1. For each of the five tools (`read_docx`, `process_document_batch`,
     `accept_all_changes`, `diff_docx_files`, `finalize_document`):
     `tools/list` shows `reasoning` **absent from** `inputSchema.required`
     (assert `!(tool.inputSchema.required ?? []).includes("reasoning")`).
  2. `tools/call read_docx` **without** `reasoning` succeeds and returns document
     text (today this is a `-32602` invalid-params failure).
  3. `tools/call process_document_batch` without `reasoning` applies the batch.
  4. `reasoning` is still accepted when sent (no client breaks) â€” one call with
     it present returns the same content as without it.
- **Change**: for each tool, replace
  `reasoning: z.string().describe(â€¦)` with
  `reasoning: z.string().optional().describe(â€¦)` and keep the handler's
  `void reasoning;` line (it documents the deliberate non-use, mirroring Python's
  `del reasoning`, `tools/document.py:1383`). Python defaults it to `""`
  (`tools/document.py:1380`); `.optional()` is the Zod equivalent that keeps the
  parameter out of `required[]`. Do **not** delete the parameter: reason-first UX
  is still advertised, it is just no longer mandatory.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/mcp.schema-gaps.test.ts`
    â€” the four new cases pass. Existing tests that send `reasoning` keep passing
    untouched.

## Task 12 â€” A2: search paging, snippet clamping and the response-size budget
- **Status**: COMPLETED
- **Failed Verify Cycles**: 2
- **Attempt Ledger**:
  - attempt 1: rewrite build_search_response with search budget, snippet clamping, and match_offset -> FAIL (verifier audit: max_matches and match_offset schema types published as number instead of integer; need .int() constraint)
  - attempt 2: add .int() constraint to max_matches and match_offset schemas in read_docx schema -> FAIL (verifier audit: window edges in render_entry cut astral characters straddling radius edge into lone surrogates)
  - attempt 3 (fix cycle 2): implement snapCodePointBoundary for window intervals in render_entry and add test case 6b for astral surrogate boundary handling -> PASS




- **Goal**: every match reachable via `match_offset`, snippets clamped to Â±120
  chars with a radius ladder, and a worst-case default search bounded to â‰¤1.5k
  tokens instead of 5â€“7k.
- **Difficulty**: HARD (the single most intricate port in this plan: radius
  ladder Ã— budget pass Ã— CriticMarkup re-attachment)
- **Files**:
  - modify `node/packages/mcp-server/src/response-builders.ts`
    (`build_search_response` `:398-718`; `balanceSnippetWindow` `:129-144` is
    replaced by the ordered-depth version; `emphasizedSnippetSpans` `:68-104`
    stays)
  - modify `node/packages/mcp-server/src/index.ts` (`read_docx` schema/handler)
  - create `node/packages/mcp-server/src/search-budget.test.ts`
  - modify `node/packages/mcp-server/src/response_builders.test.ts` (existing
    9-case suite: the `page`-semantics cases must keep passing; the "first 20
    matches" note text changes â€” update only those assertions)
- **Test first** â€” `search-budget.test.ts`:
  1. `max_matches` respected: 50 hits, `max_matches: 5` renders 5 entries and the
     note ``> **Note:** Only 5 matches shown (max_matches=5). Continue with `match_offset=5`.``
  2. `match_offset`: `match_offset: 5, max_matches: 5` renders hits 6â€“10 and the
     `### Match N` headings carry the **global** index (6..10), not 1..5.
  3. `match_offset` past the end renders no entries and
     ``> **Note:** No matches in this window (match_offset=99, total matches=50).``
  4. `max_matches: 0` renders no entries and
     ``> **Note:** No matches shown (max_matches=0, total matches=50). Pass `max_matches=N` with N >= 1 to see match snippets.``
     â€” never silently rewritten to 20.
  5. Negative `match_offset` is coerced to 0.
  6. Snippet clamp: a 4,000-char paragraph with one hit renders a snippet
     `<= ~2*120 + hit + markers` chars, prefixed and suffixed with `...` when
     context was trimmed.
  7. Radius ladder: 20 hits in long paragraphs produce
     ``> **Note:** Snippets trimmed to Â±60 chars to fit the response size budget.``
     (or a narrower rung) and the whole response satisfies
     `approxTokens(content) <= search_budget_tokens(20, rendered)`.
  8. Budget floor: when not even one Â±16 snippet fits,
     ``> **Note:** No matches shown in this window: not even one Â±16-char snippet fits the response size budget (max_matches=â€¦, total matches=â€¦). Raise `max_matches`, or pass `full_paragraph=true` to read the matching paragraph in full.``
  9. Dropped-tail note: when trailing hits are dropped,
     ``> **Note:** Snippets trimmed to Â±N chars and trailing matches dropped to fit the response size budget â€” continue from the `match_offset` above.``
     and the header's "N shown" plus the `match_offset=` in the continuation note
     agree with the number of entries actually rendered.
  10. `full_paragraph: true` renders whole paragraphs and no trim note.
  11. Bubble safety: a hit inside a multi-line `{>>â€¦<<}` bubble renders the whole
      bubble (no bare `<<}`/`{>>` fragment anywhere in the output), and a hit in
      the middle of a 4,000-char `{--â€¦--}` deletion renders wrapped in
      `{--`/`--}` plus the trailing `{>>[Chg:N delete]...<<}` header
      (`_enclosing_snippet_markup`, `_response_builders.py:282-331`).
  12. Worst case from the spec: on the `long_5pages` fixture, a default search
      (`max_matches: 20`) yields `approxTokens(content) <= 1500`.
  13. Header shapes: with `total > rendered` or `match_offset > 0`, the header is
      the `(N total, M shown)` variant; otherwise the plain variant
      (`_response_builders.py:997-1029`).
  14. A range string in search mode (`page: "2-4"`) throws
      ``Invalid page value: '2-4'. In search mode, `page` must be omitted (search all pages), `'all'`, or a positive integer document page number.``
- **Change**: rewrite `build_search_response` as a port of
  `python/src/adeu/mcp_components/_response_builders.py:716-1278`. Order of work
  (each step is independently testable; commit as one task but build in this
  order):
  1. Port the constants and `search_budget_tokens`
     (`_response_builders.py:158-202`): `SEARCH_TOKENS_PER_MATCH = 60`,
     `CHARS_PER_TOKEN = 4`, `SNIPPET_RADIUS_LADDER = [120, 60, 30, 16]`,
     `SEARCH_FIXED_CHROME_TOKENS = 120`, `SEARCH_ENTRY_CHROME_TOKENS = 22`,
     `SEARCH_MIN_SNIPPET_TOKENS = 13`.
  2. Replace `balanceSnippetWindow` with the ordered-depth two-sided version
     (`_response_builders.py:211-262`) returning `[start, end]`: walk delimiters
     in order with a per-pair depth counter, move `start` back to a missing
     opener, push `end` forward to a missing closer, loop until stable. The
     current Node version only ever widens `end` (`response-builders.ts:129-144`)
     and counts rather than orders, so it accepts `l1--}â€¦{--del`.
  3. Port `_merge_spans` (`:334-342`), `_trailing_bubble_header` (`:265-279`) and
     `_enclosing_snippet_markup` (`:282-331`). The Python regexes translate
     directly; `re.DOTALL` â†’ `s` flag; `body.rfind(x, 0, start)` â†’
     `body.lastIndexOf(x, start - x.length)` **(verify the off-by-one with a
     test â€” JS `lastIndexOf`'s second argument is the start of the match, Python's
     is an exclusive end bound)**.
  4. Port the resolution/validation head (`:749-841`), the no-match and
     page-filter-no-match paths (`:843-922`), `window_note_response` (`:927-962`)
     and the `max_matches < 1` / `match_offset >= total` short circuits
     (`:964-982`).
  5. Port `build_header` (`:986-1044`), `clean_breadcrumb` (`:1046-1061`) and
     `get_heading` (`:1063-1085`). Note `get_heading` scans to the **end of the
     hit's line** (`:1070-1072`) â€” Node's current version slices at the hit
     offset (`response-builders.ts:606`) and truncates a heading-internal match's
     path. That is a genuine bug fix, in parity's direction.
  6. Port `group_by_line` (`:1091-1109`) and `render_entry` (`:1111-1186`),
     including the `" ... "` interior separator, the head/tail `...` markers
     measured against the **edge** lines (`:1152-1158`), and the global
     `full_index_map` (`:1089` â€” key by array position, not `id(m)`, since JS has
     no object identity for regex matches: build the map while enumerating
     `matches_with_pages`).
  7. Port `compose` (`:1190-1201`) and the budget pass (`:1230-1269`) verbatim,
     including `fits()` counting `content_prefix.length` and the two note
     wordings.
  8. Keep Node's existing regex-downgrade behaviour
     (`response-builders.ts:417-454` â€” invalid pattern and
     `RegexTimeoutError` both downgrade to literal with a note); Python's `is_cli`
     strict branch is not ported (Assumption 2). The downgrade note survives
     `no_chrome` (`_response_builders.py:1195-1200`).
  9. New signature:
     ```ts
     export function build_search_response(
       text: string, search_query: string, search_regex: boolean,
       search_case_sensitive: boolean, page: number | string | undefined,
       file_path: string, bundle?: ProjectionBundle,
       opts?: { max_matches?: number; match_offset?: number; full_paragraph?: boolean; no_chrome?: boolean },
     ): ToolResult;
     ```
     Positional prefix unchanged so `response_builders.test.ts` keeps compiling.
  10. `index.ts` schema additions (names and descriptions copied from
      `tools/document.py:1357-1368`):
      ```ts
      max_matches: z.coerce.number().default(20)
        .describe("For search queries: maximum number of search matches to return (default 20)."),
      match_offset: z.coerce.number().default(0)
        .describe("For search queries: 0-based match offset to start search results from for pagination (default 0)."),
      full_paragraph: z.boolean().default(false)
        .describe("For search queries: return full paragraph for search matches instead of clamping snippets to Â±120 chars."),
      ```
      and pass them through in the search branch (`index.ts:506-518`).
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures; the updated
    assertions in `response_builders.test.ts` are limited to the truncation-note
    wording and are listed in the commit message.
  - `cd node/packages/mcp-server && npx vitest run src/search-budget.test.ts` â€”
    14 cases pass.
  - `ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts -t "search_"` â€”
    `search_default`, `search_max2_offset2`, `search_full_paragraph` match the
    Python goldens byte-for-byte.

## Task 13 â€” A3: response-budget guard on whole-document reads (ships only with A6)
- **Status**: COMPLETED
- **Failed Verify Cycles**: 3
- **Attempt Ledger**:
  - attempt 1: implement build_budget_guard_message and force parameter on read_docx -> FAIL (verifier audit: guard_long5 conformance golden fails due to missing end_page on OutlineNode, and clean_view=true budget guard lacks outline section)
  - attempt 2 (fix cycle 1): implement end_page on OutlineNode, render_outline_tree p4-p5 range formatting, clean_outline_nodes caching -> FAIL (verifier audit: text.length in budget guard includes structural appendix rather than body length, causing premature refusal for docs with appendix)
  - attempt 3 (fix cycle 2): measure bundle.body.length in index.ts for read_docx budget guard and update budget-guard.test.ts case 9 -> FAIL (verifier audit: bundle.body in split_structural_appendix carries trailing \n\n--- separator, leaving 5-char delta vs Python 551 chars)
  - attempt 4 (fix cycle 3): implement split_projection in shared.ts to strip trailing \n\n--- separator from bundle.body, making unicode.docx body 551 chars matching Python, and add tests for 551 chars body length and ADEU_MAX_RESPONSE_CHARS=553 serving -> PASS





- **Goal**: refuse an unbounded whole-document body read over the budget and
  answer with page count, estimated tokens, the L1 outline and the bounded-read
  recipe â€” at â‰¤800 tokens.
- **Difficulty**: HARD (must fire on exactly one path; a false positive breaks the
  text round-trip artifact)
- **Files**:
  - modify `node/packages/mcp-server/src/response-builders.ts` (add
    `build_budget_guard_message`)
  - modify `node/packages/mcp-server/src/index.ts` (`read_docx` schema: `force`;
    handler `page === "all"` branch `:522-530`)
  - create `node/packages/mcp-server/src/budget-guard.test.ts`
- **Prerequisite**: Task 3 (page ranges) **must** be merged first â€” spec A3's
  "âš  v3-validated constraint": a guard that offers only single pages turned
  whole-document needs into page-walking and was the largest cost regression of
  the v3 sweep. Do not implement this task before Task 3 is green.
- **Test first** â€” `budget-guard.test.ts`:
  1. Fires: `mode:"full"`, `page:"all"` on a projection longer than
     `response_budget_limit()` returns `isError: true` whose text contains
     `Refused unbounded full document read`, the page count, the six recipe
     lines, and an `Outline (L1 Headings):` section.
  2. Budget: `approxTokens(text) <= 800`.
  3. `force: true` returns the full body (guard bypassed).
  4. Exempt paths (assert **no** refusal): `mode:"outline"`, `mode:"appendix"`,
     `mode:"changes"`, and `search_query` with `page:"all"` â€” spec A3 acceptance.
  5. Under the limit: an ordinary document with `page:"all"` returns the whole
     body as today (the QA 2026-07-17 F1 round-trip artifact must not regress).
  6. `ADEU_MAX_RESPONSE_CHARS=1000` makes a small document trip the guard
     (env tunable), and an unparseable value falls back to 76000.
  7. A document with no L1 headings gets **no** outline section and no
     `(No headings detected)` placeholder.
- **Change**:
  1. `response-builders.ts` â€” port
     `python/src/adeu/mcp_components/_response_builders.py:654-700`:
     ```ts
     export function build_budget_guard_message(
       projected_text: string,
       file_path: string,
       nodes: OutlineNode[] | null,
       bundle?: ProjectionBundle,
     ): string {
       const [body] = bundle ? [bundle.body] : split_structural_appendix(projected_text);
       const pagination = bundle ? bundle.pagination : paginate(body, "");
       const list = nodes ?? [];
       const has_l1 = list.some((n) => n.level === 1);
       const outline = has_l1 ? render_outline_tree(list, 1, false) : "";
       return whole_doc_guard_message(
         projected_text.length,
         response_budget_limit(),
         file_path,
         outline,
         pagination.total_pages,
       );
     }
     ```
     (`whole_doc_guard_message` / `response_budget_limit` come from Task 1 via
     `@adeu/core`.) Note the total is measured on **`projected_text`**, not on
     `body` â€” Python does the same (`:695`), so the numbers match.
  2. `index.ts` schema: add
     ```ts
     force: z.boolean().default(false)
       .describe("For mode='full' with page='all': read the whole document even when it exceeds the response budget."),
     ```
  3. `index.ts` handler, in the `page === "all"` branch (`:522-530`), mirror
     `tools/document.py:512-529`:
     ```ts
     if (!force && text.length > response_budget_limit()) {
       const entry3 = await getEntry();
       return { isError: true, content: [{ type: "text", text:
         build_budget_guard_message(text, file_path, entry3.outline_nodes, bundle) }] };
     }
     ```
     `DocCacheEntry.outline_nodes` already exists (`doc-cache.ts:62`) so the
     outline costs nothing extra. For `clean_view: true`, the cache has no clean
     outline â€” pass `null` and accept a refusal with no outline section (Python's
     clean path has the same limitation).
  4. Add the guard to `READ_DOCX_TAIL`'s `page='all'` sentence so it is
     discoverable: ``page='all' returns the whole body with no page chrome; oversized documents are refused with an outline and a bounded-read recipe unless force=true.``
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/budget-guard.test.ts` â€”
    seven cases pass.
  - `ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts -t "guard_long5"`
    â€” matches the Python golden byte-for-byte.

## Task 14 — B7: fused-JSON hint on unrecognised `type`
- **Status**: COMPLETED
- **Failed Verify Cycles**: 1
- **Attempt Ledger**:
  - attempt 1: change CHANGE_ITEM_SCHEMA type to z.string().optional() and append FUSED_JSON_HINT -> FAIL (verifier audit: fused-json.test.ts case 5 needs to assert that FUSED_JSON_HINT is in the error message alongside checking no smaller/fewer edits wording)
  - attempt 2: added assertion in fused-json.test.ts test case 5 that res.content[0].text includes FUSED_JSON_HINT -> PASS




- **Goal**: when a `type` string carries `{`, `}` or `":`, the error names the
  cause (two edits fused during generation) and the fix (resubmit this edit
  alone).
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/mcp-server/src/index.ts` (`typeErrors` loop `:806-818`)
  - modify `node/packages/core/src/engine.ts` (the `validate_edit_strings` /
    invalid-type error path â€” locate it with
    `rg -n "unrecognized \"type\"|Invalid change format" node/packages/core/src/engine.ts`;
    UNVERIFIED which line owns the library-side message, executor must confirm)
  - create `node/packages/mcp-server/src/fused-json.test.ts`
- **Test first** â€” `fused-json.test.ts`:
  1. `{type: 'modify}],{comment:'}` in a batch produces a per-index error whose
     text ends with the exact `FUSED_JSON_HINT` sentence.
  2. The three observed fused shapes from the spec (`'modify}],{comment: â€¦'`,
     `'{"type"'`, `'accept":'`) all trigger the hint.
  3. A plain unknown type (`'modifyy'`) produces the existing message **without**
     the hint (no false positives).
  4. The failure envelope for a fused element carries the correct 0-based
     `failed[].index` (interaction with Task 6).
  5. The hint does **not** advise smaller batches (assert the response contains
     no "smaller" / "fewer edits" wording) â€” spec B7 is explicit that
     fresh-round failure is size-independent.
- **Change**: in the `typeErrors` loop, append the hint when
  `has_fused_json_marker(c.type)`:
  ```ts
  const fused = has_fused_json_marker(c.type) ? ` ${FUSED_JSON_HINT}` : "";
  typeErrors.push(`- Change ${i + 1}: missing or unrecognized "type". â€¦ Received keys: [${â€¦}].${fused}`);
  ```
  Import both from `@adeu/core` (Task 1). Mirror `python/src/adeu/cli.py:549-550`,
  which appends the same sentence to `has an unknown type: '<tag>'`. If the core
  engine has its own invalid-type message reachable without the MCP boundary,
  append the hint there too, using the same helper (one hint, one source).
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/fused-json.test.ts` â€” five
    cases pass.

## Task 15 â€” C1: multi-author guard message teaches the lawful recovery
- **Status**: COMPLETED


- **Goal**: the guard refusal names the change to accept, in copy-pasteable JSON,
  and stays inside the 280-char cap â€” matching Python byte-for-byte.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/core/src/engine.ts` (foreign-insertion guard
    `:3122-3176`; foreign-comment guard `:3183-3187`)
  - create `node/packages/core/src/engine.guard-message.test.ts`
- **Test first** â€” `engine.guard-message.test.ts` (build a doc, insert a tracked
  insertion as "Jane Doe", then run a batch as "Adeu AI (TS)"):
  1. A straddling `modify` fails with exactly:
     ``- Edit 1 Failed: Modification targets an active insertion from another author (Jane Doe (e.g. Chg:1)). Accept first with {"type": "accept", "target_id": "Chg:1"} or scope your edit outside of it.``
  2. `match_mode:"all"` **wholly inside** a foreign insertion gets the extra
     advice: ``â€¦ Accept first with {â€¦} or use match_mode="strict" or "first", or scope your edit outside of it.``
     (`engine.py:2342-2343`).
  3. Two ids from one author render as `(e.g. Chg:1, Chg:3)` â€” at most two;
     a second author adds ` (+1 more)`.
  4. Cap: with a 400-char author name the whole message is
     `<= GUARD_MESSAGE_CAP` (280 = `70 * 4`, `engine.py:49`) and the author name
     is clamped with `clamp_text` (Task 0), not the head or tail.
  5. Wholly-inside strict/first is still **allowed** (no error) â€” the existing
     allowance at `engine.ts:3164-3175` must not regress.
  6. The comment-range guard names ids:
     ``- Edit 1 Failed: match_mode="all" would sweep through a comment range from another author (Bob Smith (e.g. Com:2)). Target the commented text deliberately with match_mode "strict" or "first", or scope your edit outside of it.``
  7. Budget: `approxTokens(message) <= 70` for the common single-author,
     single-id case.
- **Change**: port `python/src/adeu/redline/engine.py:2320-2379`:
  - add `const GUARD_MESSAGE_CAP = 70 * 4;` near the top of `engine.ts` (beside
    the other caps) with a comment citing `engine.py:49`;
  - change `insAuthors: Set<string>` (`engine.ts:3122`) to
    `insAuthorsToIds: Map<string, Set<string>>`, populated with
    `s.ins_id` alongside the author at `:3138-3142`; same for
    `commentAuthors` â†’ `commentAuthorsToIds` using the comment ids at `:3147-3156`
    (ids there are already `Com:`-prefixed in Python's message â€” check what
    `mapper.comments_map` keys look like and prefix to `Com:<id>` to match the
    golden);
  - build the message with the head/tail/author-budget split exactly as Python
    (`:2346-2357`), then `clamp_text(msg, GUARD_MESSAGE_CAP)`;
  - ids sort numerically (`parseInt(x, 10) || 0`), authors sort lexicographically.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures. Existing tests
    asserting the old guard sentence (search:
    `rg -n "active insertion from another author" node/packages/*/src/*.test.ts`)
    are updated to the new text â€” that is the deliberate change.
  - `cd node/packages/core && npx vitest run src/engine.guard-message.test.ts` â€”
    seven cases pass.

## Task 16 â€” B6: pin the non-escaping of non-ASCII (regression guard only)
- **Status**: COMPLETED

- **Goal**: prove Node never `\uXXXX`-escapes legal text, so the Python fix stays
  parity-checked from both sides.
- **Difficulty**: EASY
- **Files**: create `node/packages/mcp-server/src/unicode-passthrough.test.ts`
- **Test first / Change** (this task is only a test):
  1. A batch report for an edit whose `new_text` is `â€™ â€œ â€ â€” â‚¬` renders those
     characters literally in `formatBatchResult` output â€” assert
     `text.includes("â€™ â€œ â€ â€” â‚¬")` and `!/\\u[0-9a-fA-F]{4}/.test(text)`.
  2. The Task 6 failure envelope JSON block, parsed and re-read, contains the
     literal characters; the raw block contains no `\u` escape.
  3. `build_changes_response` renders a smart-quoted comment body literally
     (uses the `unicode.docx` fixture from Task 2).
  4. A non-ASCII author name survives into the ledger's `Authors â€”` roster.
- **Done when**: `cd node/packages/mcp-server && npx vitest run src/unicode-passthrough.test.ts`
  â€” four cases pass; `cd node && npm run test` â€” zero failures.

---

# Phase 3 â€” P2 / P3 (contract changes needing version care, then the rest)

## Task 17 â€” A4: `no_chrome` on every builder (internal parameter)

- **Goal**: one switch that drops the File-Path header, page banners/footers and
  the appendix pointer, keeping a bare `[p3/16]` marker â€” the shape Python's
  builders already implement, needed for byte-parity of the `no_chrome` goldens
  and for any future Node CLI.
- **Difficulty**: EASY (mechanical, but every string is a golden)
- **Files**:
  - modify `node/packages/mcp-server/src/response-builders.ts`
    (`build_full_document_response` `:199`, `build_paginated_response` `:220`,
    `build_outline_response`/`render_outline_response` `:263`/`:297`,
    `build_appendix_response` `:338`, `render_outline_tree` `:168`;
    `build_page_range_response` and the ledger already take the flag from
    Tasks 3â€“4)
  - create `node/packages/mcp-server/src/no-chrome.test.ts`
- **Test first** â€” `no-chrome.test.ts`:
  1. `build_paginated_response(..., no_chrome: true)`: output is
     `[p2/5]\n\n<page_content>` â€” no `> **File Path:**`, no
     `> **Page 2 of 5** (synthetic page â€¦)`, no `Continues on page`, no appendix
     pointer. Single-page documents get no marker at all.
  2. Byte-identity of the payload: with chrome removed, the remaining text
     contains the page's `page_content` **byte-identically** to the chromed
     response (spec A4 acceptance).
  3. Token diff: `approxTokens(chromed) - approxTokens(terse) >= 20` for a
     multi-page document (the ~85â€“160-token-per-page chrome the spec measures).
  4. `build_full_document_response` with `no_chrome` drops only the File-Path
     line.
  5. `render_outline_tree` visible-empty case with `no_chrome` drops the
     `Call read_docx with â€¦` hint sentence
     (`_response_builders.py:372-375`).
  6. `build_appendix_response` with `no_chrome`: no banner/footer prose
     (verify against Python's shipped behaviour first â€” read
     `_response_builders.py:1281-1371` and match it exactly; if Python does not
     implement `no_chrome` for appendix, Node must not either, and this case
     asserts the chromed output instead).
  7. `render_outline_tree` renders `p3-p5` ranges when `node.end_page > node.page`
     (`_response_builders.py:399-400`) â€” **Node's `OutlineNode` has no
     `end_page`** (`node/packages/core/src/outline.ts` interface, verified).
     Either port `end_page` in `extract_outline` or, if that is a larger job than
     this task allows, assert current behaviour and record the gap in the Risks
     section for a follow-up plan. Decide by reading Python's
     `outline.py` `end_page` computation first; prefer the smaller diff.
- **Change**: add `no_chrome: boolean = false` as the last parameter of each
  builder and branch exactly as Python does at the cited lines. Do **not** add a
  tool parameter (Assumption 2/3). Where Node's chrome strings differ from
  Python's MCP strings, Python wins; diff the two files side by side for
  `build_page_banner` / `build_page_footer` / `build_appendix_pointer`
  (`pagination.py:449-509` vs `response-builders.ts:146-166`) and fix any drift
  found â€” a drift here silently breaks every page golden.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/mcp-server && npx vitest run src/no-chrome.test.ts` â€” all
    cases pass (case 7 either implemented or explicitly asserting today's
    behaviour with a `// GAP:` comment naming the follow-up).
  - `ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts -t "outline_l1"`
    â€” matches the Python golden.

## Task 18 â€” C2: author-impersonation warning

- **Goal**: setting the acting author to a name that already has pending
  revisions no longer silently bypasses the multi-author guard.
- **Difficulty**: EASY (Python already ships it â€” pure port)
- **Files**:
  - modify `node/packages/core/src/engine.ts` (`_process_batch_internal` head)
  - create `node/packages/core/src/engine.impersonation.test.ts`
- **Test first** â€” `engine.impersonation.test.ts`:
  1. A document with a pending insertion by "Jane Doe", edited by an engine
     constructed with author "Jane Doe": `stats.author_impersonation_warning ===`
     ``[!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.``
     (`engine.py:2668-2670`).
  2. A different acting author: the field is `null`/absent.
  3. A clean document: absent (no pending revisions at all).
  4. Editing **your own** earlier revisions still warns (Python's behaviour â€” the
     comparison is a plain membership test) but the batch **succeeds**; the
     warning is never an error. Spec C1 acceptance says normal same-author
     workflows stay silent, and Python's shipped behaviour warns â€” **Python
     wins** (Assumption 1); record the discrepancy in Risks.
  5. Comment authors count as pending-revision authors
     (`engine.py:2621-2627`).
  6. Rendered: `formatBatchResult` shows `*Warning:* [!] Warning: acting author â€¦`
     right after the `Saved to:` line (already implemented in Task 8).
- **Change**: port `get_pending_revision_authors` (`engine.py:2588-2654`) as
  `public get_pending_revision_authors(): Set<string>`:
  - walk every element of `this.doc.element` collecting the `w:author` attribute
    (use the existing DOM helpers in `docx/dom.ts` / `findAllDescendants` rather
    than a new traversal);
  - add comment authors from `extract_comments_data(this.doc.pkg)`, skipping
    `"Unknown"`;
  - walk the other `+xml` parts of the package (footnotes, endnotes, headers)
    collecting `w:author`, **skipping** the people/persona part
    (`application/vnd.ms-word.people+xml` or a partname containing `people`) â€”
    its entries survive accepting every revision, so they are metadata;
  - never throw: wrap the package walk in try/catch like Python does.
  Then in `_process_batch_internal`, before anything mutates:
  ```ts
  const pending_authors = this.get_pending_revision_authors();
  const author_impersonation_warning =
    this.author && pending_authors.has(this.author)
      ? `[!] Warning: acting author '${this.author}' matches an author with pending revisions in this document.`
      : null;
  ```
  and add `author_impersonation_warning` to the returned stats
  (`engine.py:2886`).
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/core && npx vitest run src/engine.impersonation.test.ts` â€”
    six cases pass.

## Task 19 â€” C3: `apply_text_revision` MCP tool on Node

- **Goal**: expose the strongest bulk primitive (whole-text diff â†’ tracked
  changes with a post-apply verification gate) over MCP, with the same interlocks
  as Python.
- **Difficulty**: HARD (the verification gate and the `.unverified.docx` sibling
  semantics are integrity-critical â€” spec Â§10 forbids weakening them)
- **Files**:
  - create `node/packages/core/src/text-revision.ts`
  - modify `node/packages/core/src/index.ts` (export the new API and
    `generate_edits_via_paragraph_alignment`, currently unexported at
    `diff.ts:543`)
  - modify `node/packages/mcp-server/src/index.ts` (register the tool)
  - create `node/packages/core/src/text-revision.test.ts`
  - create `node/packages/mcp-server/src/text-revision-tool.test.ts`
- **Test first** â€” `text-revision.test.ts` (core):
  1. Happy path: a 3-paragraph document + revised clean text with one sentence
     changed produces a saved buffer whose clean text equals the supplied text,
     with tracked changes present (`w:ins` and `w:del` exist), and
     `stats.verified === true`.
  2. CriticMarkup refusal: revised text containing `{++` throws with Python's
     exact message (`text_revision.py:69-73`), listing all five token forms.
  3. Paginated-extract refusal: text beginning with a
     `> **Page 2 of 5** â€¦` banner throws
     ``Text revision looks like page 2 of 5 of a paginated extract â€” it contains only part of the document, and applying it would delete every page not present. Re-extract the ENTIRE document first with --page all --clean-view.``
  4. Chrome stripping: text carrying the File-Path header, a `Page 1 of 1`
     banner and an appendix pointer is accepted with the chrome removed
     (`_strip_page_chrome`, `text_revision.py:76-92`).
  5. Major-deletion guard: revised text 60% shorter than a â‰¥2000-char original
     throws the `~60% shorter â€¦` message with the 50% threshold; a 60% cut of a
     <2000-char document is allowed (75% floor); `allow_major_deletions: true`
     bypasses both.
  6. Verification failure: a revision that the document structure cannot realise
     (delete a heading's text via text replacement) writes
     `<stem>.unverified.docx` **and nothing to the target path**, and the thrown
     error carries `unverified_path`, `output_path`, and stats with
     `verified: false`, `error: "verification_failed"`, `edits_applied: 0`, all
     per-edit reports rewritten to `status: "failed"` with
     `error: "Not applied: post-apply verification failed."`
     (`text_revision.py:250-277`).
  7. Default output path: `x.docx` â†’ `x_redlined.docx`; `x_redlined.docx` and
     `x_processed.docx` â†’ written in place (`text_revision.py:243-246`).
  8. Author resolution: explicit `author` wins; else `ADEU_AUTHOR`; else
     `"Adeu AI (TS)"` â€” **Node's engine default**, not Python's `"Adeu AI"`
     (Node's existing default is `engine.ts:481` and the MCP schema advertises it
     at `index.ts:704`; keeping it avoids changing attribution for existing Node
     users). Do **not** port Python's `getpass` machine-account logic: Node's MCP
     server has no CLI user concept, and inventing one would diverge silently.
     Record this as a declared cosmetic difference in
     `shared/conformance/README.md`.
- **Test first** â€” `text-revision-tool.test.ts` (mcp-server, live RPC):
  9. `tools/list` advertises `apply_text_revision` with parameters
     `file_path`, `revised_text`, `output_path?`, `author?`,
     `allow_major_deletions?`, `reasoning?` (optional per Task 11), and a
     description that names the clean-view input contract and the
     `allow_major_deletions` interlock.
  10. A successful call returns text containing `Saved to: <path>` and the
      applied/skipped counters (reuse `formatBatchResult`).
  11. A verification failure returns `isError: true` naming the
      `.unverified.docx` sibling and stating that it is NOT the requested
      document.
  12. A major-deletion refusal returns `isError: true` with the threshold message
      and does not write any file.
- **Change**:
  1. `core/src/text-revision.ts` â€” port `python/src/adeu/text_revision.py` minus
     the CLI-only parts, exporting:
     ```ts
     export class TextRevisionError extends Error {}
     export class TextRevisionVerificationError extends TextRevisionError {
       constructor(message: string, public unverified_path: string, public output_path: string, public stats: Record<string, any>) { super(message); }
     }
     export function check_criticmarkup(text: string): void;
     export function check_major_deletions(original_text: string, revised_text: string, allow_major_deletions?: boolean, source_name?: string | null): void;
     export function strip_page_chrome(text: string): { text: string; page: number | null; total: number | null };
     export function verify_clean_text(doc: DocumentObject, expected_text: string): [boolean, string | null];
     export async function apply_text_revision_core(opts: {
       doc: DocumentObject;            // caller loads (the MCP layer owns file IO + error shaping)
       input_path: string;
       revised_text: string;
       output_path?: string | null;
       author?: string | null;
       allow_major_deletions?: boolean;
     }): Promise<{ stats: Record<string, any>; output_path: string; out_bytes: Uint8Array; unverified?: { path: string; bytes: Uint8Array } }>;
     ```
     Keep the module **IO-free** apart from returning bytes: the MCP layer already
     owns `readFileBytesOrThrow` / `loadDocxOrThrow` / `writeFileSync` and their
     agent-facing error shapes (`index.ts:115-192`). This is the one deliberate
     structural difference from Python, which does its own file IO. Verification
     failure returns the `unverified` bytes **and throws** â€” so make it: build the
     result, and on failure throw `TextRevisionVerificationError` carrying the
     bytes on `stats.__unverified_bytes` **no** â€” instead give the error an
     `unverified_bytes: Uint8Array` field; the handler writes it. Explicit is
     better than a magic stats key.
     Use `generate_edits_via_paragraph_alignment` (diff.ts:543) for the edit
     generation, and `_extractTextFromDoc(doc, true, false)` for clean text
     (matching `_extract_clean_text_from_doc`, `text_revision.py:139-144`).
     Heading normalisation for verification: `text.replace(/^#+\s*/gm, "")`
     (`text_revision.py:149`).
     Constants to copy verbatim: `_CRITICMARKUP_TOKENS` (the five OPEN tokens
     only, `text_revision.py:18` â€” a bare closer is ordinary prose),
     `_MAJOR_DELETION_MIN_ORIGINAL_CHARS = 2000`, the four chrome regexes
     (`:20-23`), and every message string.
     Number formatting: `{rev_len:,}` â†’ `toLocaleString("en-US")`.
  2. `mcp-server/src/index.ts` â€” register the tool with
     `server.registerTool` (headless, no UI), description ported from
     `python/src/adeu/mcp_components/tools/document.py`'s `apply_text_revision`
     (read it at `:1100-1203` and copy the published wording, including the
     clean-view contract and the interlock sentence). Handler:
     load bytes â†’ `loadDocxOrThrow` â†’ `apply_text_revision_core` â†’
     `mkdirSync` + `writeFileSync` of `out_bytes` â†’ `docCache.invalidate(outPath)`
     (never `primeFromDoc`: only the batch pipeline's byte-equality gate is
     covered, per the comment at `index.ts:992-997`) â†’ return
     `formatBatchResult(stats, outPath) + overwriteNote(...)`.
     On `TextRevisionVerificationError`: write `unverified_bytes` to the sibling
     path, then return `{ isError: true, content: [{ type: "text", text: err.message }] }`.
     On `TextRevisionError`/`Error` from the guards: `isError: true` with the
     message, **no file written**.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures.
  - `cd node/packages/core && npx vitest run src/text-revision.test.ts` â€” eight
    cases pass.
  - `cd node/packages/mcp-server && npx vitest run src/text-revision-tool.test.ts`
    â€” four cases pass.

## Task 20 â€” A5: audit the structured-diff payload for default-valued fields

- **Goal**: make sure Node's ready-to-apply edit payloads do not carry
  default-valued fields that inflate the agent's next tool call by 25â€“40%.
- **Difficulty**: EASY (audit + at most a small filter)
- **Files**:
  - inspect `node/packages/core/src/diff.ts` (`DiffEdit` type;
    `generate_edits_from_text` `:274`, `generate_edits_via_paragraph_alignment`
    `:543`, `generate_structured_edits` `:970`)
  - inspect `node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/generateDiff.operation.ts`
    (the one Node surface that serialises edits to JSON)
  - create `node/packages/core/src/diff-payload.test.ts`
- **Test first** â€” `diff-payload.test.ts`:
  1. For a two-sentence change, every emitted `DiffEdit`, once
     `JSON.stringify`d, contains **no** `"match_mode":"strict"`, no
     `"regex":false`, and no boilerplate `"comment":"Diff: â€¦"` key.
  2. Round-trip: feeding the emitted edits straight into
     `RedlineEngine.process_batch` applies them all (`edits_skipped === 0`) â€”
     i.e. dropping the defaults changed nothing semantically.
  3. Size regression guard: record `JSON.stringify(edits).length` for a fixed
     fixture in the test as an upper bound with 10% headroom, so a future field
     addition is caught.
- **Change**: only if case 1 fails. The likely fix is to stop setting the field
  rather than to strip it afterwards â€” find where the default is written and omit
  it (`undefined` fields disappear from `JSON.stringify` automatically). Do not
  introduce a serializer layer. If case 1 already passes, keep the test as the
  regression guard and state in the commit message that A5 needed no code change
  on Node (the Python-side inflation was `indent=2` plus default fields in
  `cli.py`, which has no Node counterpart).
- **Done when**:
  - `cd node/packages/core && npx vitest run src/diff-payload.test.ts` â€” three
    cases pass.
  - `cd node && npm run build && npm run test` â€” zero failures.

## Task 21 â€” E3/E4: id-discovery and missing-file hints point at the ledger

- **Goal**: once the ledger exists (Task 4/5), every error that tells an agent how
  to find ids names it; and the missing-file helper stays parity-checked.
- **Difficulty**: EASY
- **Files**:
  - modify `node/packages/core/src/engine.ts` (`_action_not_found_error`
    `:4543-4589`, specifically `find_hint` at `:4553-4555`; constructor `:481`)
  - modify `node/packages/mcp-server/src/index.ts` (pass the hint when
    constructing engines: `:855`, plus the Task 5 ledger construction)
  - create `node/packages/core/src/engine.id-hint.test.ts`
  - modify `node/packages/mcp-server/src/index.test.ts` (E3 assertions; read it
    first â€” it is 2.4 kB)
- **Test first** â€” `engine.id-hint.test.ts`:
  1. Default (library) hint: an `accept` on a stale id yields an error containing
     ``Call `read_docx` with `mode='changes'` on the document again to list the current change (Chg:) and comment (Com:) ids â€” ids shift between document states.``
     when the engine was constructed with the MCP hint, and the current generic
     sentence otherwise.
  2. `new RedlineEngine(doc, author, { id_discovery_hint: "custom" })` puts
     `custom` in the message (constructor parity with
     `python/src/adeu/redline/engine.py:396-404`).
  3. The change/comment id mix-up branches (`:4557-4580`) keep their wording and
     still append the hint.
  4. Existing id-list capping at 20 (`_format_id_list`, `:4524-4531`) unchanged.
- **Test first** â€” E3 (mcp-server): assert the missing-file error for a relative
  path contains ``available files: [`` , the ``(+N more in `` suffix when the
  directory holds more than 10 `.docx` files, and the absolute-path sentence only
  for relative inputs. If `index.test.ts` already covers this (verify first),
  extend rather than duplicate; Node's helper at `:91-155` already mirrors
  Python's `suggest_sibling_docx`, so this is a regression guard.
- **Change**:
  1. `RedlineEngine` constructor gains an options bag:
     ```ts
     constructor(doc: DocumentObject, author: string = "Adeu AI (TS)", opts?: { id_discovery_hint?: string }) {
       â€¦
       this.id_discovery_hint = opts?.id_discovery_hint ?? null;
     ```
     Positional compatibility is preserved (every existing call site keeps
     working).
  2. `_action_not_found_error`: `const find_hint = this.id_discovery_hint || "<current generic sentence>";`
     â€” exactly Python's shape (`engine.py:4988`).
  3. `mcp-server/src/shared.ts` (1.3 kB) gains
     ```ts
     /** MCP callers cannot run a CLI, so id-discovery advice inside engine errors
      *  must point at the MCP tool (QA 2026-07-23 F11). Mirrors
      *  python/src/adeu/mcp_components/shared.py MCP_ID_DISCOVERY_HINT. */
     export const MCP_ID_DISCOVERY_HINT =
       "Call `read_docx` with `mode='changes'` on the document again to list the current change (Chg:) and comment (Com:) ids â€” ids shift between document states.";
     ```
     and every `new RedlineEngine(...)` in `mcp-server` passes it.
- **Done when**:
  - `cd node && npm run build && npm run test` â€” zero failures (update the
    existing tests that assert the old `Call \`read_docx\` on the document againâ€¦`
    sentence â€” the wording change is the point of E4).
  - `cd node/packages/core && npx vitest run src/engine.id-hint.test.ts` â€” four
    cases pass.

## Task 22 â€” Release: ungate conformance, bump to 2.3.0, docs

- **Goal**: turn the conformance suite on permanently, ship the version lockstep,
  and leave the repo's own documentation truthful.
- **Difficulty**: EASY (but it is the gate: nothing ships until this passes)
- **Files**:
  - modify `node/packages/mcp-server/src/conformance.test.ts` (remove the
    `ADEU_CONFORMANCE` gate)
  - modify `shared/conformance/README.md`
  - modify `AI_CONTEXT.md` and/or `GEMINI.md` **only** where they document the
    tool surface that changed (`read_docx` modes/params, `process_document_batch`
    `partial`, the new `apply_text_revision`) â€” read them first and make the
    minimal edit; do not restructure.
  - run `python scripts/bump.py minor` (repo root)
- **Test first**: the conformance suite itself, now ungated, plus
  `node scripts/check_release_consistency.mjs`.
- **Change**:
  1. Remove the `describe.skipIf(!process.env.ADEU_CONFORMANCE)` gate; delete the
     "which task turns this green" comment block and replace it with the
     declared-cosmetic-difference allowlist (spec Â§8.3 "modulo a declared
     allowlist"): currently just the `apply_text_revision` default author
     (`Adeu AI (TS)` vs `Adeu AI`) and the absence of `is_cli` flavours.
  2. `python scripts/bump.py minor` â†’ 2.3.0 across
     `python/pyproject.toml`, `node/packages/core/package.json`,
     `node/packages/mcp-server/package.json`,
     `desktop-extension/manifest.json`, `gemini-extension.json`,
     `python/server.json`, `node/packages/n8n-nodes-adeu/package.json`.
     **Do not** touch `nodes/Adeu/Adeu.node.json`'s `nodeVersion`/`codexVersion`
     (`AGENTS.md` n8n codex exception; `scripts/bump.py:19-23` already excludes it).
  3. Release checklist to state in the commit message, each item checked:
     - `cd node && npm ci && npm run build && npm run test && npm run lint`
     - `cd python && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`
       (proves the Node work did not break the Python side's shared fixtures)
     - `node scripts/check_release_consistency.mjs`
     - **B4 gate (Task 10)**: the Python mirror has landed, or Task 10 is
       reverted. Do not publish otherwise.
     - Spec Â§9's benchmark note: after publishing, the harness pin moves to
       2.3.0 â€” out of scope here, but say so in the commit body so the follow-up
       is not lost.
- **Done when**:
  - `cd node && npm run build && npm run test && npm run lint` â€” zero failures,
    conformance included and ungated.
  - `node scripts/check_release_consistency.mjs` exits 0.
  - `git diff --stat` shows no changes under `python/src/**` (this plan never
    edits the Python engine).

---

# Parallelisation

After Phase 0 (Tasks 0â€“2, strictly sequential: 1 depends on 0, 2 depends on 0),
these groups are independent and safe to run in parallel:

- **Group R (read surfaces)**: Task 3 â†’ Task 13 (13 requires 3); Task 4 â†’ Task 5
  (5 requires 3 and 4); Task 12; Task 17.
- **Group W (write surfaces)**: Task 6 â†’ Task 7 â†’ Task 8 (strictly sequential);
  Task 9; Task 10; Task 14 (needs 6 for the index assertions); Task 15; Task 18.
- **Group X (independent)**: Task 11; Task 16; Task 20; Task 21.
- Task 19 depends only on Phase 0 and Task 8 (it reuses `formatBatchResult`).
- Task 22 is last and depends on everything.

Two tasks in different groups never touch the same file except
`node/packages/mcp-server/src/index.ts` (Tasks 3, 5, 7, 9, 11, 12, 13, 14, 19,
21) and `node/packages/core/src/engine.ts` (Tasks 6, 7, 8, 10, 15, 18, 21). Those
two files are contention points: serialise work on them, or accept merge conflicts
in the tool-registration block and the batch orchestrator. Prefer running Group R
and Group W in sequence over racing them.

# Risks

1. **Golden churn from absolute paths.** The single most likely failure is
   goldens that embed a machine-specific path or `\r\n`. Mitigation is in Task 2:
   a fixed placeholder `file_path` and `\n` normalisation on both sides. If a
   conformance test fails with a diff that is only a path or line ending,
   fix the harness, never the builder.
2. **Python may move under the port.** The goldens are captured from the working
   tree, not a release. If `python/` changes mid-plan, re-run
   `capture_goldens.py` and re-check; a golden diff is a signal, not noise.
   Record the Python git SHA used for capture in
   `shared/conformance/README.md`.
3. **B5 salvage + apply-stage failures (Task 7).** Node currently `break`s after
   an apply-stage failure because later edits would validate against a partially
   mutated document (`engine.ts:3567-3572`). Salvage mode removes that break to
   match Python. If Task 7's tests 4 and 6 show incoherent reports or a corrupt
   save, the fallback is: in salvage mode, stop processing further edits after
   the first **apply-stage** failure (keep processing after validation-stage
   failures), report the remaining edits as failed with
   "not attempted: an earlier edit failed mid-apply", and note the deliberate
   divergence from Python in `shared/conformance/README.md`. Integrity outranks
   parity here (spec Â§10).
4. **B4 is a one-engine semantic (Task 10).** Rollback: revert the Task 10 commit;
   nothing else depends on it. The `_attach_comment` return-value change is safe
   to keep either way.
5. **C1/E4 wording changes break existing tests.** Expected and intended. Before
   editing, run
   `rg -n "active insertion from another author|Call \`read_docx\` on the document again" node/packages/*/src/*.test.ts`
   and update only the assertions on the changed sentence. If a test asserts the
   sentence indirectly (a snapshot), update the snapshot in the same commit.
6. **Search port regressions (Task 12).** This rewrite touches the most-tested
   builder in the repo (`response_builders.test.ts`, `repro_agy_search_query_filter.test.ts`,
   `repro_qa_round3_2026_07_24.test.ts`). Run those three files first after the
   rewrite; if the failures are about note wording only, update them; if they are
   about page filtering or occurrence counts, the port is wrong â€” the Python
   source is the arbiter.
7. **`z.coerce.number()` on `max_matches`/`changes_offset`** accepts `"abc"` as
   `NaN`. Guard in the handler (`Number.isFinite(x) ? x : default`) and add one
   test per parameter; a `NaN` slice silently returns an empty window.
8. **Description-length ceiling.** Tasks 3, 5, 7, 9, 12, 13 all add prose to tool
   descriptions. Real clients truncate at ~2048 chars (`index.ts:218-224`). Assert
   the budget in `mcp.schema-gaps.test.ts` after every such task; if a
   description would overflow, cut older prose that duplicates a parameter
   description, not the new guidance.
9. **`OutlineNode.end_page` gap (Task 17 case 7)** means `render_outline_tree` can
   never match a Python golden for a document whose heading spans pages. If the
   `outline_l1` golden fails only on `pN-pM` labels, port `end_page`; if that
   proves large, declare it in the allowlist and open a follow-up â€” do not fake
   the label.
10. **Rollback**: every task is one commit touching a bounded file set; nothing is
    destructive to user data. The only artifacts written outside the repo are test
    temp files under `os.tmpdir()` (existing pattern) and, for Task 19, the
    `.unverified.docx` sibling â€” which is deliberate, documented, and only
    written on verification failure.

PLAN COMPLETE
