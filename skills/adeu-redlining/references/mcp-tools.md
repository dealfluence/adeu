# Adeu MCP Tool Reference

Load this when planning a non-trivial batch on the MCP path. The five Adeu MCP tools are listed below with their parameters and the patterns that actually work in practice.

## `read_docx`

Read a `.docx` and return text with inline CriticMarkup.

**Parameters:**
- `file_path` (str, required) — absolute path.
- `clean_view` (bool, default `false`) — `false` shows raw text with CriticMarkup for all pending changes; `true` shows the text as if every pending change were accepted.
- `mode` (`"full"` | `"outline"` | `"appendix"`, default `"full"`):
  - `full` — body content, paginated.
  - `outline` — heading map only. Start here for large docs to plan targeted reads. Defaults to L1–L2 headings; pass `outline_max_level=3..6` for deeper structure.
  - `appendix` — defined terms, cross-reference targets, bookmarks, footnotes. Consult before editing legal/technical docs to avoid breaking references.
- `page` (int or `"all"`) — without `search_query`: which body page to show (defaults to 1). With `search_query`: which page to restrict matches to (defaults to all).
- `search_query` (str) — substring or regex. Filters results to matching paragraphs across the requested page scope.
- `search_regex` (bool, default `false`) — interpret `search_query` as regex.
- `search_case_sensitive` (bool, default `true`).
- `outline_max_level` (int, 1–6, default `2`) — only for `mode="outline"`.
- `outline_verbose` (bool, default `false`) — include extra metadata in the outline.

**Pattern:** for any document longer than a few pages, the first call should be `mode="outline"`. Read the headings, decide which page or section to read in full, then call again with that page number.

## `process_document_batch`

Apply a list of edits to a `.docx`. Edits apply sequentially: each one evaluates against the document state produced by the edits before it, so you may chain dependent edits within one batch (a later edit targets the text as it reads after the earlier edits). If any edit fails validation, the whole batch is rejected transactionally.

**Parameters:**
- `original_docx_path` (str, required).
- `author_name` (str, required) — appears in Track Changes (e.g. `"AI Reviewer"`).
- `changes` (array, required) — see the discriminated union below.
- `output_path` (str, optional) — defaults to `<original>_processed.docx`.
- `dry_run` (bool, default `false`) — simulate without writing. Returns the same report.

### The `changes` discriminated union

Each entry must include a `type` field. The other fields depend on `type`.

#### `type: "modify"` — search-and-replace as a tracked change
- `target_text` (str, required) — the existing text to find.
- `new_text` (str, required) — the replacement. Markdown supported: headings `#`–`######`, `**bold**`, `_italic_` (underscores only), `\n\n` for paragraph breaks. Empty string deletes.
- `match_mode` (`"strict"` | `"first"` | `"all"`, default `"strict"`):
  - `strict` — target must match exactly once. Add context to disambiguate.
  - `first` — first occurrence in linear document order.
  - `all` — every occurrence.
- `regex` (bool, default `false`) — treat `target_text` as a regular expression. Capture groups available in `new_text` as `$1`, `$2`, …
- `comment` (str, optional) — margin comment attached to the change.

**Never put CriticMarkup syntax (`{++`, `{--`, `{>>`, `{==`) into `new_text`.** Use `comment` for comments.

#### `type: "accept"` / `type: "reject"` — resolve an existing tracked change
- `target_id` (str, required) — e.g. `"Chg:12"`. Must come from a `read_docx` call made *immediately before* this batch.
- `comment` (str, optional).

#### `type: "reply"` — reply to an existing comment
- `target_id` (str, required) — e.g. `"Com:5"`.
- `text` (str, required) — the reply body.

#### `type: "insert_row"` — add a table row (disk mode only, not Live Word)
- `target_text` (str, required) — a cell value to anchor on (any cell in the reference row).
- `position` (`"above"` | `"below"`).
- `cells` (string[]) — values for the new row, left to right.

#### `type: "delete_row"` — remove a table row (disk mode only)
- `target_text` (str, required) — a cell value to anchor on.

**Adding or removing columns is not supported and must not be attempted via text replacement.**

### Report shape

`process_document_batch` returns a per-edit report. Each edit reports `status` (`applied` | `failed`), `pages`, `heading_path`, `occurrences_modified`, and either a preview (`critic_markup`, `clean_text`) or an `error` / `warning`. Use this to verify the batch did what you intended.

## `accept_all_changes`

Accept every tracked change and remove every comment in one shot. Produces a clean finalized document.

**Parameters:** `docx_path` (required), `output_path` (optional, defaults to `<original>_clean.docx`).

## `diff_docx_files`

Compare two `.docx` files. Returns Adeu's custom `@@ Word Patch @@` sub-word diff format — *not* a standard unified diff.

**Parameters:**
- `original_path`, `modified_path` (both required).
- `compare_clean` (bool, default `true`) — `true` compares accepted state; `false` compares raw text including pending changes.

## `finalize_document`

Strip metadata, lock the document, and prepare for external distribution.

**Parameters:**
- `file_path` (required), `output_path` (optional, defaults to `<original>_final.docx`).
- `sanitize_mode` (`"full"` | `"keep-markup"`) — `full` removes all redline markup; `keep-markup` only redacts author metadata.
- `accept_all` (bool) — auto-accept pending changes before finalizing.
- `protection_mode` (`"read_only"` | `"encrypt"`) — native OOXML locking. **Note:** the Node MCP server does not support `"encrypt"` and falls back to `"read_only"`. Use the Python server if encryption is required.
- `author` (str) — replace remaining markup authorship with this name.
- `password`, `export_pdf` — ignored by the Node server. Python server supports them when system dependencies are present.

## ID volatility — critical

`Chg:N` and `Com:N` are session-bound. They shift on every save and on every state-changing edit. The rule: **always call `read_docx` immediately before any batch that contains `accept` / `reject` / `reply`.** Do not reuse IDs from earlier in the conversation.

## Common patterns

- **Targeted single edit:** `read_docx` with `search_query` → identify exact match → `process_document_batch` with one `modify` in `strict` mode.
- **Global rename:** `process_document_batch` with one `modify` using `match_mode="all"`.
- **Resolve all pending comments:** `read_docx` (fresh) → batch of `reply` actions, one per `Com:N`.
- **Two-stage rename + downstream edit:** Batch 1 = the rename. Re-read. Batch 2 = the downstream edit. Never combined.
- **Pre-distribution scrub:** `finalize_document` with `accept_all=true`, `sanitize_mode="full"`, `protection_mode="read_only"`.