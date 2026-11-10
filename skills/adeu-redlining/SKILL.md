---
name: adeu-redlining
description: Use this skill when reviewing, editing, redlining, or negotiating an existing Microsoft Word document (.docx) — including proposing edits as tracked changes, accepting or rejecting existing tracked changes, replying to comments, comparing two versions, sanitizing author metadata, or finalizing a contract for distribution. Use whenever the user mentions redlines, track changes, contract review, .docx editing, or a Word document they want changed, even if they don't name a specific tool. Do NOT use for creating a Word document from scratch with no source file — use the docx skill for that.
license: MIT (see LICENSE.txt)
compatibility: Requires either the Adeu MCP server (preferred — Node `@adeu/mcp-server` or Python `adeu`) OR `uvx` for the CLI fallback. Live MS Word integration requires Windows + the Python `adeu` server.
metadata:
  homepage: https://adeu.ai
  repository: https://github.com/dealfluence/adeu
  version: "3.2.1"
---

# Adeu — Tracked-Changes Redlining for .docx

Adeu is a Virtual DOM for Word documents. It translates `.docx` into LLM-friendly Markdown with CriticMarkup for tracked changes, lets you propose edits as native Word `w:ins`/`w:del` revisions, and writes the result back without destroying formatting, comments, or document structure.

## When to use this skill

The user is working with an existing `.docx` and wants you to:

- Propose edits that show up as Word Track Changes
- Accept, reject, or reply to existing tracked changes and comments
- Compare two versions of a document
- Strip author metadata and lock a document for distribution
- Read a contract intelligently, including its defined terms and cross-references

If the user wants a fresh Word document built from nothing, defer to the `docx` skill instead.

## Execution path — pick once, then forget

Adeu runs in two modes. Pick the first that applies and stop:

1. **MCP tools available.** If you see Adeu MCP tools in the session (`read_docx`, `process_document_batch`, `accept_all_changes`, `diff_docx_files`, `finalize_document`), use them directly. This file contains everything needed for standard MCP operations; only load `references/mcp-tools.md` when you need edge-case parameter details.
2. **Bash available, no MCP tools.** Shell out to the Python CLI: `uvx adeu <subcommand>`. This is the only CLI Adeu ships — there is no Node CLI. Load `references/cli-fallback.md` for the command surface and the JSON shape `adeu apply` expects.
3. **Neither available.** Tell the user. Suggested install lines, in this order:
   - Claude Code plugin (covers everything): `/plugin marketplace add dealfluence/adeu` then `/plugin install adeu-redlining@adeu-skills`
   - Node MCP server (recommended for most users, zero Python required): `npx -y @adeu/mcp-server`
   - Python MCP server (required for Live MS Word integration on Windows, and for `protection_mode="encrypt"` on `finalize_document`): `uvx --from adeu adeu-server`
   - Python CLI only (for scripted/headless pipelines): `uv tool install adeu`

Do not present these as options to the user mid-task. Pick the available path and proceed.

## Core workflow

Every redlining task follows the same tight loop:

1. **Read targeted slices first.** Choose the fastest `read_docx` mode for the task instead of `page="all"`:
   - **Triage / review existing redlines & comments:** call `read_docx(file_path=..., mode="changes")` to get the compact ledger of every `Chg:N` and `Com:N` with author, page, and snippet in one call.
   - **Locate specific clauses or terms:** call `read_docx(file_path=..., search_query="...", search_case_sensitive=false)` (or `search_regex=true` with alternation like `"indemnif|liabilit|terminat"`).
   - **Navigate structure or read sections:** call `read_docx(file_path=..., mode="outline")`, then read bounded page windows (`page=1` or `page="1-8"`, continuing with `page="9-16"` when the footer indicates more pages).
   - **Compare two `.docx` files into a redline:** the whole task is one call, `diff_docx_files(original_path=..., modified_path=..., output_path=..., author_name=...)`. It writes every difference as native tracked changes, all or nothing, and refuses an `output_path` that names either input. A compare redline always comes from this call (or `adeu diff v1 v2 -o out.docx` on the CLI); a hand-built `process_document_batch` loses changes the diff engine catches.
2. **Apply all changes in one batch.** Send every edit and review action in a single `process_document_batch` call (or one `adeu apply` invocation).
   - **Sub-clause surgical edits:** keep `target_text` to the shortest unique phrase (5–20 words around the changed words) rather than replacing entire multi-sentence paragraphs.
   - **Sequential evaluation:** each edit in `changes` evaluates against the document state produced by preceding edits in the same batch.
   - **Output path:** pass `output_path` explicitly when requested. When `output_path` is omitted, files ending in `_processed`, `_redlined`, `.redline`, `-redline`, `_markup`, or `.marked` update in place; other files save to `<stem>_processed.docx`.
   - **Manual / inline formatting mode:** when the user asks for strikethrough deletions and underlined insertions instead of Word Track Changes, pass `markup_mode="manual"` on `process_document_batch` (or `--manual` on `adeu apply`). This mode produces the markup and its margin comments natively; it replaces scripting python-docx, which breaks the package.
3. **Finish from the batch receipt.** `process_document_batch` returns per-edit `*Preview (CriticMarkup):*`, minted `**IDs:**` (`Chg:N`, `Com:N`), `**Part:**` when an edit landed in a header, footer or notes, `Applied Actions`, and `Document State` (remaining tracked changes and comments). Treat a clean batch receipt as final verification — skip any extra post-batch `read_docx` call unless you need to inspect surrounding prose not shown in the receipt.

The MCP tool salvages by default (`partial=true`): valid edits apply, and the response LEADS with `PARTIAL: applied K of N` listing each failed edit with its diagnostics and nearest-match candidates. Fix only those and send them as a follow-up batch. Pass `partial=false` when a half-applied batch would be worse than none (dependent edits, a negotiated package). The CLI `adeu apply` is the reverse: all-or-nothing unless you pass `--partial`.

## Critical gotchas

- **IDs are session-bound.** Change IDs (`Chg:12`) and comment IDs (`Com:5`) come from `read_docx` (e.g. `mode="changes"`) or from the `**IDs:**` line of the immediately preceding `process_document_batch` receipt. Never guess IDs.
- **Batches apply sequentially.** If edit 1 renames `X → Y`, a later edit in the same batch touching that span must target `Y`, not the original `X`.
- **`target_text` must be unique by default.** `match_mode: "strict"` (default) requires a single match. Add 3–8 words of surrounding context to disambiguate, or set `match_mode: "first"` or `"all"`. Set `regex: true` for regex matching; capture groups expand via `$1`, `$2` (or `\1`, `\2`) in `new_text`.
- **Plain replacement text in `new_text`.** Never write `{++`, `{--`, `{>>`, or `{==` into `new_text`. Use the `comment` field on the edit to attach a margin comment (a string, or a list `[{"text": "...", "author": "..."}]` to attach multiple comments with optional per-comment authors).
- **`new_text` Markdown dialect.** Supports headings (`#` through `######`), `**bold**`, `_italic_` (underscores only), `\n\n` for paragraph breaks, and Markdown pipe tables (`| Col 1 | Col 2 |\n|---|---|\n| A | B |`) to insert native Word tables. Empty string `""` deletes `target_text`; omitting `new_text` with `comment` annotates without changing text. Empty table cells emit `{#cell:<id>}` — pass that token as `target_text` to fill one.
- **Restructure whole paragraphs.** To split, merge, or reorder paragraphs, target the complete paragraph(s) and give the complete new paragraphs in `new_text`. A `\n\n` inserted into the middle of an existing paragraph becomes a tracked paragraph mark, which many reviewers and redline readers display as an untracked edit.
- **The top of page 1 may be the page header.** Headers and footers repeat on every page, so headings and tables cannot be added there (the batch refuses and names the first body paragraph to use instead). Anchor document-top insertions such as an executive summary on the first body paragraph shown by `mode="outline"`, with `match_mode="first"` if that text recurs.
- **Read-only appendix & structural markers.** Anything inside `<!-- READONLY_BOUNDARY_START -->` (defined terms, cross-reference targets, diagnostic summaries) is read-only projection metadata. Plain-text `target_text` in the body resolves automatically across hyperlinks (`[text](url)`), cross-references (`[~text~](#_Ref...)`), and bookmarks (`{#_BookmarkName}`), preserving them when kept intact.
- **`clean_view` toggles the projected state.** `clean_view=false` (default) projects raw text with pending tracked changes as CriticMarkup; `clean_view=true` projects the accepted state. Note that `process_document_batch` matches `target_text` against the clean/accepted state so you can edit cleanly across existing changes.
- **Page indexing.** `page="1-8"` reads up to 8 pages at a time; check the `Continues on page X of Y` footer to see if more pages follow. `page` also accepts a page or range (`page=4` or `page="4-5"`) alongside `search_query`.

## Edit types — complete schema reference

Pass `changes` as a list of objects to `process_document_batch(original_docx_path, author_name, changes, output_path?, partial?, markup_mode?)`:

| `type`        | Purpose                                   | Fields                                                                                                                                      |
| ------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `modify`      | Search-and-replace as a tracked change    | `target_text` (req), `new_text` (req unless `comment` set; `""` deletes), `comment?` (str \| `[{text, author?}]`), `match_mode?` (`"strict"` \| `"first"` \| `"all"`), `regex?` (bool) |
| `accept`      | Finalize an existing tracked change by ID | `target_id` (req, e.g. `"Chg:12"`), `comment?`, `part?` (`"body"` \| `"header"` \| `"footer"` \| `"footnotes"` \| `"endnotes"` or `"word/header1.xml"`) |
| `reject`      | Revert an existing tracked change by ID   | `target_id` (req, e.g. `"Chg:12"`), `comment?`, `part?`                                                                                     |
| `reply`       | Reply to an existing comment thread       | `target_id` (req, e.g. `"Com:5"`), `text` (req)                                                                                             |
| `set_field`   | Fill or toggle a content control (`CC:N`) | `field` (req, e.g. `"CC:3"` or tag/alias), `value` (req), `match_mode?`, `comment?`                                                         |
| `insert_row`  | Insert a table row above/below an anchor  | `target_text` (req, cell text in anchor row), `position` (`"above"` \| `"below"`), `cells` (`string[]` left-to-right), `comment?`           |
| `delete_row`  | Delete a table row containing anchor text | `target_text` (req, cell text in row to delete), `comment?`                                                                                 |

Other MCP tools:
- `read_docx(file_path, mode="full"|"outline"|"changes"|"appendix"|"fields", page?, force?, search_query?, search_regex?, search_case_sensitive?, max_matches?, match_offset?, full_paragraph?, changes_author?, changes_offset?, fields_offset?, clean_view?)`
- `diff_docx_files(original_path, modified_path, compare_clean=true, output_path?, author_name?)` — returns the text diff; with `output_path` it also writes the redlined `.docx` (all or nothing, never over an input).
- `apply_text_revision(file_path, revised_text, output_path?, author?, allow_major_deletions?)` — rewrites a whole document from revised clean text into native tracked changes behind a clean-text verification gate.
- `accept_all_changes(docx_path, output_path?, remove_comments=true)` — accepts all tracked changes (`remove_comments=false` keeps comments).
- `finalize_document(file_path, output_path?, sanitize_mode="full"|"keep-markup", accept_all?, protection_mode?, author?)`

## Reference loading (on demand only)

Do **not** load these during normal MCP tasks; consult them only when their specific trigger applies:

- **`references/mcp-tools.md`** — consult only for content-control lock overrides (`ignore_control_locks`, `ignore_document_protection`, `allow_untracked_writes`) or `finalize_document` encryption details.
- **`references/cli-fallback.md`** — load only when MCP tools are absent and you must use `uvx adeu` via Bash.
- **`references/criticmarkup.md`** — load only when interpreting unusual projection markers (`[^fn-3]`, `{#_BookmarkName}`, `[~text~](#_Ref...)`).
