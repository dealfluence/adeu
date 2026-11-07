# Adeu MCP Tool Reference

Load this when planning a non-trivial batch on the MCP path. The five Adeu MCP tools are listed below with their parameters and the patterns that actually work in practice.

## `read_docx`

Read a `.docx` and return text with inline CriticMarkup.

**Parameters:**
- `file_path` (str, required) — absolute path.
- `clean_view` (bool, default `false`) — `false` shows raw text with CriticMarkup for all pending changes; `true` shows the text as if every pending change were accepted.
- `mode` (`"full"` | `"outline"` | `"changes"` | `"appendix"` | `"fields"`, default `"full"`):
  - `full` — body content, paginated (or up to 8 pages per range window `page="1-8"`).
  - `outline` — heading map only (includes Heading styles, all-caps bold headings, and standalone numbered/structural bold headings). Start here for large docs to plan targeted reads. Defaults to L1–L2 headings; pass `outline_max_level=3..6` for deeper structure.
  - `changes` — compact ledger of every pending tracked change (`Chg:N`) and comment thread (`Com:N`) with author, page, and snippet. Start here when triaging, accepting, rejecting, or replying to existing markup.
  - `appendix` — defined terms, cross-reference targets, bookmarks, footnotes. Consult before editing legal/technical docs to avoid breaking references.
  - `fields` — ledger of form fields (content controls) with CC:<N> ID, tag, alias, class, state, option lists, and data bindings.
- `fields_offset` (int, default 0) — 0-indexed pagination offset for `mode="fields"`.
- `page` (int, `"N-M"` range, or `"all"`) — without `search_query`: which body page or 8-page range to show (defaults to 1). With `search_query`: which page or page range to restrict matches to (defaults to all).
- `search_query` (str) — substring or regex. Filters results to matching paragraphs across the requested page scope.
- `search_regex` (bool, default `false`) — interpret `search_query` as regex (supports inline flags `(?i)`, `(?m)`, `(?s)`).
- `search_case_sensitive` (bool, default `true`).
- `full_paragraph` (bool, default `false`) — in search mode, return the full matching paragraph rather than a snippet window.
- `outline_max_level` (int, 1–6, default `2`) — only for `mode="outline"`.
- `outline_verbose` (bool, default `false`) — include extra metadata in the outline.

**Pattern:** for any document longer than a few pages, the first call should be `mode="outline"`. Read the headings, decide which page or section to read in full, then call again with that page number.

## `process_document_batch`

Apply a list of edits to a `.docx`. Edits apply sequentially: each one evaluates against the document state produced by the edits before it, so you may chain dependent edits within one batch (a later edit targets the text as it reads after the earlier edits). By default valid edits apply even when others fail, and the response leads with `PARTIAL: applied K of N`; `partial=false` makes the batch all-or-nothing.

**Parameters:**
- `original_docx_path` (str, required).
- `author_name` (str, required) — appears in Track Changes (e.g. `"AI Reviewer"`).
- `changes` (array, required) — see the discriminated union below.
- `output_path` (str, optional) — defaults to in-place when the stem already ends in `_processed`, `_redlined`, `.redline`, `-redline`, `_markup`, or `.marked`; otherwise `<original>_processed.docx`.
- `partial` (bool, default `true`) — applies valid changes and reports invalid ones as skipped; pass `false` to reject the whole batch when any change fails.
- `markup_mode` (`"track_changes"` | `"manual"`, default `"track_changes"`) — `"manual"` renders deletions as inline strikethrough and insertions as bold underlined red (`#C00000`) text, with each edit's comment as a native Word comment, instead of `<w:ins>`/`<w:del>` revisions.
- `ignore_control_locks` (bool, default `false`) — permit writes to content-locked controls (`sdtContentLocked`).
- `ignore_document_protection` (bool, default `false`) — permit writes to read-only or comments-protected documents.
- `allow_untracked_writes` (bool, default `false`) — permit untracked fills in forms-protected documents where Word suppresses revision tracking.

### The `changes` discriminated union

Each entry must include a `type` field. The other fields depend on `type`.

#### `type: "modify"` — search-and-replace as a tracked change
- `target_text` (str, required) — the existing text to find (keep to the shortest unique sub-clause of 5–20 words).
- `new_text` (str, required) — the replacement. Markdown supported: headings `#`–`######`, `**bold**`, `_italic_` (underscores only), `\n\n` for paragraph breaks, and Markdown pipe tables (`| H1 | H2 |\n|---|---|\n| C1 | C2 |`) to insert native Word `<w:tbl>` tables. Empty string deletes.
- `match_mode` (`"strict"` | `"first"` | `"all"`, default `"strict"`):
  - `strict` — target must match exactly once. Add context to disambiguate.
  - `first` — first occurrence in linear document order.
  - `all` — every occurrence.
- `regex` (bool, default `false`) — treat `target_text` as a regular expression. Capture groups available in `new_text` as `$1`, `$2`, …
- `comment` (str, optional) — margin comment attached to the change.

**Never put CriticMarkup syntax (`{++`, `{--`, `{>>`, `{==`) into `new_text`.** Use `comment` for comments.

#### `type: "set_field"` — fill or toggle a form field (content control)
- `field` (str, required) — `"CC:<N>"` ordinal ID, tag, or alias.
- `value` (str, required) — value to set (e.g. date `"YYYY-MM-DD"`, dropdown choice, or `"true"`/`"false"` for checkboxes).
- `match_mode` (`"strict"` | `"first"` | `"all"`, default `"strict"`).
- `comment` (str, optional).

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

`process_document_batch` returns a complete receipt: `Document State` (remaining tracked changes and comments), `Applied Actions` (with resolved `Chg:N` / `Com:N` IDs), and per-edit reports (`status`, `pages`, `heading_path`, `part` when the edit landed outside the body, e.g. `header (word/header1.xml)`, `occurrences_modified`, minted `**IDs:**`, and `*Preview (CriticMarkup):*` or `error` / `warning`). Treat this receipt as verification without a redundant `read_docx` call.

A `modify` that adds a heading or a pipe table inside a page header or footer is refused: those parts repeat on every page. The error names the first (header) or last (footer) body paragraph to target instead.

## `accept_all_changes`

Accept every tracked change in one shot. Produces a finalized clean document.

**Parameters:** `docx_path` (required), `output_path` (optional, defaults to `<original>_clean.docx`), `remove_comments` (bool, **default `true`**).

`remove_comments` defaults to **true**: the output is meant to be distributable and comments are internal review notes that must not reach a counterparty. This is destructive — it deletes other people's comments too. Pass `remove_comments=false` when the review conversation is still live and the user only wants the redlines resolved.

The response reports how many comments were deleted and names each one with its author. **Read that line and tell the user** — they may not realise accepting changes also cleared the reviewer's notes. A comment whose anchored text an accepted deletion consumes is removed either way (Word behaves the same).

## `diff_docx_files`

Compare two `.docx` files. Returns Adeu's custom `@@ Word Patch @@` sub-word diff format; with `output_path` it also writes a redlined `.docx`. This is the way to produce a comparison redline: one call, every difference tracked.

**Parameters:**
- `original_path`, `modified_path` (both required).
- `compare_clean` (bool, default `true`) — `true` compares accepted state; `false` compares raw text including pending changes.
- `output_path` (str, optional) — writes a redlined `.docx` of `original_path` containing all differences as native Word tracked changes. All or nothing: if any computed change cannot be tracked, the call fails and writes nothing. Must differ from both inputs (refused otherwise).
- `author_name` (str, default `"Comparison"`) — author name for tracked changes written when `output_path` is specified.

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
- **Rename + downstream edit:** one batch — the rename first, then a `modify` whose `target_text` is the renamed text (batches apply sequentially).
- **Compare two versions:** `diff_docx_files` with `output_path`; no batch needed.
- **Pre-distribution scrub:** `finalize_document` with `accept_all=true`, `sanitize_mode="full"`, `protection_mode="read_only"`.