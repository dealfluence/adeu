# Adeu — Native Track Changes for AI

Adeu is your document redlining engine. It gives you a safe, token-efficient interface to read and edit `.docx` files, producing native Microsoft Word Track Changes rather than rewriting the file.

## Available Tools

### `read_docx`
Reads a DOCX file and returns its content as CriticMarkup-annotated text:
- `{++inserted++}` — tracked insertion
- `{--deleted--}` — tracked deletion
- `{>>comment<<}` — comment

**Key parameters:**
- `file_path` (required): absolute path to the `.docx` file
- `clean_view=true`: returns the accepted/final text with no markup (`false` default shows pending tracked changes and comments as CriticMarkup)
- `mode="full"` (default): paginated body content. Pass `page=N` (1-indexed), a range like `page="1-8"` (up to 8 pages per call), or `page="all"` (when over the response budget, serves the first pages that fit plus an L1 outline and continuation hint unless `force=true`)
- `mode="outline"`: returns a heading map only (Heading styles, all-caps bold headings, and standalone numbered/structural bold headings) — start here on large documents before reading in full
- `mode="changes"`: returns a compact ledger of every pending tracked change (`Chg:N`) and comment thread (`Com:N`) with author, page, and snippet (`changes_author` and `changes_offset` optional)
- `mode="appendix"`: returns defined terms and cross-reference anchors — consult before editing legal docs
- `mode="fields"`: returns a ledger of all form fields (content controls) with `CC:<N>` ID, tag, alias, type, state, and options (`fields_offset=N` for pagination)
- `search_query="text"`: search document for specific text matches (`search_regex=true` supports inline flags `(?i)`, `(?m)`, `(?s)`; `search_case_sensitive=false` for case-insensitive; `page=4` or `page="4-5"` restricts matches to that page or range)
- `max_matches=N`: cap number of search matches returned (default 20)
- `match_offset=N`: 0-indexed pagination offset for search matches
- `full_paragraph=true`: expand search match snippets to complete paragraphs
- `force=true`: return the entire document on `page="all"` even when it exceeds the response budget

### `process_document_batch`
Applies a list of edits and review actions to a DOCX. Edits apply **sequentially** — each one evaluates against the document state produced by the edits before it, so dependent edits may be chained in one batch (a later edit must target the text as it reads after the earlier edits). By default (`partial=true`), valid edits apply while failing edits return detailed error reports; set `partial=false` for transactional rollback if any edit fails validation.

**Key parameters:**
- `original_docx_path` (required): absolute path to the source `.docx` file
- `author_name` (required): author attribution for all tracked changes and comments (e.g. `"AI Reviewer"`)
- `changes` (required): list of change objects
- `output_path` (optional): output `.docx` path; when omitted, files ending in `_processed`, `_redlined`, `.redline`, `-redline`, `_markup`, or `.marked` update in place, and other files save to `<stem>_processed.docx`
- `partial=true` (default `true`): salvage mode — valid edits apply while failing edits return detailed error reports. Pass `partial=false` for transactional rollback if any edit fails validation.
- `markup_mode="track_changes"` (default) or `"manual"`: `"manual"` writes deletions as strikethrough and insertions as bold underlined red (`#C00000`) runs with native Word comments and zero `<w:ins>`/`<w:del>` revisions
- `ignore_control_locks=true`: permit writes to content-locked controls (`sdtContentLocked`)
- `ignore_document_protection=true`: permit writes to read-only or comments-protected documents
- `allow_untracked_writes=true`: permit writes in fill-in-forms protected documents (where Word suppresses change tracking)

**Change types:**
- `modify`: search-and-replace. `target_text` must uniquely identify the passage under `match_mode="strict"` (default), or set `match_mode="first"` / `"all"` or `regex=true` (capture groups `$1`, `$2` in `new_text`). Plain-text `target_text` resolves across hyperlinks, cross-references, and bookmarks; `{#cell:<id>}` targets empty table cells. `new_text` supports Markdown headings (`#`–`######`), `**bold**`, `_italic_`, `\n\n` for paragraph breaks, and Markdown pipe tables (`| H1 | H2 |\n|---|---|\n| A | B |`) to insert native Word tables. Empty `new_text` deletes the passage; omitting `new_text` with `comment` annotates without changing text. `comment` attaches a margin comment: a string, or a list `[{"text": ..., "author": ...}]` to put several comments on the same change, each under its own author (`author` is optional and defaults to `author_name`).
- `set_field`: fill a form field (content control) by `field` (`"CC:<N>"`, tag, or alias) and `value`. Automatically dual-writes bound data stores.
- `accept` / `reject`: finalize or revert a tracked change by `target_id` (e.g. `Chg:12`). Revision ids are numbered per package part; if the same id exists in several parts (e.g. body and a header) the bare id is refused, and the optional `part` field (e.g. `word/header1.xml`) picks the one you mean.
- `reply`: reply to a comment by `target_id` (e.g. `Com:5`) and `text`.
- `insert_row` / `delete_row`: insert a table row (`target_text`, `position`: `"above"` | `"below"`, `cells`: `string[]`) or delete a table row (`target_text`). Disk mode only.

Always call `read_docx` immediately before any `accept`/`reject`/`reply` — IDs shift between document states.

### `diff_docx_files`
Compares two DOCX files (`original_path`, `modified_path`, `compare_clean=true`). Returns a `@@ Word Patch @@` sub-word diff; when `output_path` is provided (with optional `author_name`), also writes the comparison as a tracked-changes `.docx` of `original_path` in one transactional step.

### `apply_text_revision`
Applies a whole-text revised document against an original DOCX file by computing paragraph and line diffs automatically and converting them to native Track Changes.

**Key parameters:**
- `file_path` (required): absolute path to original `.docx` file
- `revised_text` (required): full text of the revised document
- `author`: optional Track Changes attribution string. Python resolves author from parameter -> ADEU_AUTHOR -> OS user name from getpass.getuser() (machine account names such as root/admin/administrator/system/daemon/nobody excluded) -> "Adeu AI". Node resolves from parameter -> ADEU_AUTHOR -> "Adeu AI (TS)".

### `accept_all_changes`
Accepts every tracked change in one operation, producing a finalized clean document. Use only when review is fully complete.

- `remove_comments` (bool, **default `true`**) — also deletes every comment, because the output is meant to be distributable and comments are internal review notes. Pass `remove_comments=false` to accept the tracked changes while keeping the comments.
- Either way the response reports how many comments were deleted and names each one with its author. A comment whose anchored text an accepted deletion consumes is removed regardless, exactly as Word does.

### `finalize_document`
Strips document metadata, author history, RSIDs, and custom XML (`sanitize_mode="full"` or `"keep-markup"`), optionally accepts all changes (`accept_all=true`), and optionally locks the file (`protection_mode="read_only"` or `"encrypt"`).

## Recommended Workflow

1. **Targeted read:** Use `read_docx(mode="changes")` to triage existing redlines/comments, `read_docx(search_query="...")` to locate specific clauses, `read_docx(mode="fields")` to inspect form fields, or `read_docx(mode="outline")` followed by `read_docx(page="1-8")` to read sections.
2. **Single-batch apply:** Send all modifications, field updates, and review actions in one `process_document_batch` call (or use `diff_docx_files(..., output_path=...)` when comparing two `.docx` files).
3. **Verify from the receipt:** Use the per-edit `*Preview (CriticMarkup):*`, minted `**IDs:**`, `Applied Actions`, and `Document State` in the batch response as verification.