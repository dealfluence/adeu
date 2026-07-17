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
- `clean_view=true`: returns the accepted/final text with no markup — use this first to understand context
- `mode="outline"`: returns a heading map only — start here on large documents before reading in full
- `mode="appendix"`: returns defined terms and cross-reference anchors — consult before editing legal docs
- `page=N`: navigate paginated full-text output

### `process_document_batch`
Applies a list of edits to a DOCX. Edits apply **sequentially** — each one evaluates against the document state produced by the edits before it, so dependent edits may be chained in one batch (a later edit must target the text as it reads after the earlier edits). If any edit fails validation, the whole batch is rejected transactionally.

**Change types:**
- `modify`: search-and-replace. `target_text` must uniquely identify the passage. `new_text` supports Markdown headings, bold, italic, and `\n\n` for paragraph breaks. Empty `new_text` deletes the passage.
- `accept` / `reject`: finalize or revert a tracked change by `target_id` (e.g. `Chg:12`)
- `reply`: reply to a comment by `target_id` (e.g. `Com:5`)

Always call `read_docx` immediately before any `accept`/`reject`/`reply` — IDs shift between document states.

### `accept_all_changes`
Accepts all tracked changes and removes all comments in one operation. Use only when review is fully complete.

## Recommended Workflow

1. `read_docx(mode="outline")` — understand document structure
2. `read_docx(clean_view=true)` — read final text for context
3. `read_docx()` — read raw markup to see existing tracked changes and comment IDs
4. `process_document_batch(...)` — apply your edits