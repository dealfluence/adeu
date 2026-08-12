# Conformance corpus (spec §8.3)

Six DOCX fixtures both engines read byte-for-byte, and 15 golden responses
captured from the **Python** builders with `is_cli=False` (the MCP flavour).
`node/packages/mcp-server/src/conformance.test.ts` asserts the Node builders
emit identical text, plus the four spec token budgets.

Regenerate (fixtures first — the goldens are derived from them):

```sh
node shared/conformance/build_fixtures.mjs                      # from repo root
cd python && uv run python ../shared/conformance/capture_goldens.py
```

Both scripts are deterministic: `build_fixtures.mjs` freezes `Date`,
`Math.random` and `TZ` before loading `@adeu/core` (the engine stamps revision
dates and mints hex ids from both, and fflate writes zip mtimes), so rerunning
leaves `git status` clean. `file_path` is always the placeholder
`/fixtures/<name>.docx`, never a real path — the Node tests pass the same
string.

The suite is gated on `ADEU_CONFORMANCE` until Task 22 removes the gate:

```sh
cd node/packages/mcp-server && ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts
```

| golden | fixture | builder call |
| --- | --- | --- |
| `ledger_multi_author` / `ledger_comments_threads` / `ledger_tables` | `multi_author` / `comments_threads` / `tables_cells` | `build_changes_response` defaults |
| `ledger_author_filter` | `multi_author` | `author_filter="Bob Smith"` |
| `ledger_page_filter` | `dense_175` | `page=2` |
| `ledger_dense_offset0` / `ledger_dense_offset300` | `dense_175` | `offset=0` / `offset=300` (350 entries, page size 300) |
| `range_2_4` / `range_past_end` | `long_5pages` (5 pages) | `2-4` / `4-9` (early-stop note) |
| `range_cap_1_12` | `dense_175` (9 pages) | `1-12` (eight-page cap note) |
| `guard_long5` | `long_5pages` (89,564 chars > 76,000) | `build_budget_guard_message` |
| `search_default` / `search_max2_offset2` / `search_full_paragraph` | `long_5pages` | `"Confidential Information"`, 101 hits |
| `outline_l1` | `long_5pages` | `outline_max_level=1` |
