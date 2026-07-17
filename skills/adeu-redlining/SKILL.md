---
name: adeu-redlining
description: Use this skill when reviewing, editing, redlining, or negotiating an existing Microsoft Word document (.docx) — including proposing edits as tracked changes, accepting or rejecting existing tracked changes, replying to comments, comparing two versions, sanitizing author metadata, or finalizing a contract for distribution. Use whenever the user mentions redlines, track changes, contract review, .docx editing, or a Word document they want changed, even if they don't name a specific tool. Do NOT use for creating a Word document from scratch with no source file — use the docx skill for that.
license: MIT (see LICENSE.txt)
compatibility: Requires either the Adeu MCP server (preferred — Node `@adeu/mcp-server` or Python `adeu`) OR `uvx` for the CLI fallback. Live MS Word integration requires Windows + the Python `adeu` server.
metadata:
  homepage: https://adeu.ai
  repository: https://github.com/dealfluence/adeu
  version: "1.18.2"
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

1. **MCP tools available.** If you see Adeu MCP tools in the session (`read_docx`, `process_document_batch`, `accept_all_changes`, `diff_docx_files`, `finalize_document`), use them. This is the preferred path on every platform. Load `references/mcp-tools.md` before planning any non-trivial batch.
2. **Bash available, no MCP tools.** Shell out to the Python CLI: `uvx adeu <subcommand>`. This is the only CLI Adeu ships — there is no Node CLI. Load `references/cli-fallback.md` for the command surface and the JSON shape `adeu apply` expects.
3. **Neither available.** Tell the user. Suggested install lines, in this order:
   - Claude Code plugin (covers everything): `/plugin marketplace add dealfluence/adeu` then `/plugin install adeu-redlining@adeu-skills`
   - Node MCP server (recommended for most users, zero Python required): `npx -y @adeu/mcp-server`
   - Python MCP server (required for Live MS Word integration on Windows, and for `protection_mode="encrypt"` on `finalize_document`): `uvx --from adeu adeu-server`
   - Python CLI only (for scripted/headless pipelines): `uv tool install adeu`

Do not present these as options to the user mid-task. Pick the available path and proceed.

## Core workflow

Every redlining task follows the same shape. Follow it in order:

1. **Read first.** Always read the document before editing. Use `read_docx` (MCP) or `uvx adeu extract` (CLI). For long contracts, start with `mode="outline"` to see the heading structure, then read specific pages.
2. **Plan the edits.** Each edit is either a search-and-replace (most common), an accept/reject of an existing tracked change by ID, a reply to a comment by ID, or a structural table edit. Write the plan down explicitly before applying.
3. **Apply as one batch.** Send all edits in a single `process_document_batch` call (MCP) or one `adeu apply` invocation (CLI). Edits apply _sequentially_: each edit evaluates against the document state produced by the edits before it, so dependent edits may be chained — a later edit must target the text as it reads _after_ the earlier edits.
4. **Verify.** If the user asked for a specific outcome, re-read the modified file with `clean_view=true` (MCP) or `--clean` (CLI) and confirm.

For destructive or finalization operations, run a dry-run first when the tool supports it (`dry_run: true` on `process_document_batch`).

## Critical gotchas

These are environment-specific facts that will trip you up if you assume Word/`.docx` behaves like plain text. Read this section every time.

- **IDs are session-bound.** Change IDs (`Chg:12`) and comment IDs (`Com:5`) shift every time the document state changes. Before any `accept`, `reject`, or `reply` action, call `read_docx` _immediately_ before the batch. Never reuse IDs from earlier in the conversation. Never reuse IDs across a save/reload boundary.

- **Batches apply sequentially and reject transactionally.** You can rename `X → Y` and then in the same batch modify `Y` — but the second edit must target `Y` (the text as it reads after the rename), not the original `X` wording. Stale targets fail validation, and any validation failure rejects the whole batch (nothing is applied) with per-edit errors explaining what to fix.

- **`target_text` must be unique by default.** `match_mode: "strict"` (the default) requires a single match. Either add surrounding context to disambiguate, or explicitly set `match_mode: "first"` or `"all"`. Set `regex: true` to use a regular expression; capture groups are available as `$1`, `$2` in `new_text`.

- **Do not write CriticMarkup tags manually.** Never put `{++`, `{--`, `{>>`, or `{==` into `new_text`. Use the `comment` field on the edit to attach a margin comment. Adeu generates the tracked-change XML from your plain replacement text.

- **`new_text` supports Markdown.** Headings (`#` through `######`), `**bold**`, `_italic_` (not `*italic*` — strictly underscores), and `\n\n` to split into paragraphs. Empty string deletes.

- **Read the appendix before editing structural references.** `read_docx` projects a semantic appendix at the bottom of the document containing defined terms, cross-references (`[~text~](#_Ref)`), internal anchors (`{#_BookmarkName}`), and footnotes (`[^fn-id]`). Anything inside the `<!-- READONLY_BOUNDARY_START -->` marker is read-only — attempting to modify it via search-and-replace will be rejected. Use `mode="appendix"` to see it explicitly.

- **`clean_view` toggles the document state you see.** `clean_view=false` (default) shows the _raw_ document with all pending tracked changes inline as CriticMarkup. `clean_view=true` shows what the document would look like if every pending change were accepted. Choose deliberately — comparing the wrong view to user intent is the most common source of confusion.

- **Page indexing.** `page=N` paginates the body. `page='all'` or omitting `page` with a `search_query` searches the whole document. Don't assume page numbers from the user's PDF viewer match Adeu's pagination — they often don't.

- **Live Word (Windows COM) is Python-only.** If the user is editing the active document in Word, only the Python `adeu` server supports it. The Node server does not. Table row inserts/deletes are also not supported in Live Word mode — fall back to disk editing.

- **Multi-author redlines.** If the document has tracked changes from multiple authors, edits that overlap another author's pending insertion are rejected to prevent silent destruction of their work. Tell the user; don't try to force it.

## Edit types — quick reference

Use `references/mcp-tools.md` for the full schema. The five operations:

| Type                        | Purpose                                | Required fields                                                                |
| --------------------------- | -------------------------------------- | ------------------------------------------------------------------------------ |
| `modify`                    | Search-and-replace as a tracked change | `target_text`, `new_text`                                                      |
| `accept`                    | Finalize an existing tracked change    | `target_id` (e.g. `Chg:12`)                                                    |
| `reject`                    | Revert an existing tracked change      | `target_id`                                                                    |
| `reply`                     | Reply to an existing comment           | `target_id` (e.g. `Com:5`), `text`                                             |
| `insert_row` / `delete_row` | Add or remove a table row              | `target_text` (a cell value to anchor on), plus `position`/`cells` for inserts |

`comment` is optional on `modify`/`accept`/`reject` to attach a margin comment.

## Reference loading

Load these only when relevant — they're not part of the base context:

- **`references/mcp-tools.md`** — full MCP tool schemas, parameter details, `process_document_batch` discriminated union. Load before planning any non-trivial edit batch on the MCP path.
- **`references/cli-fallback.md`** — `uvx adeu` subcommands and the `edits.json` shape. Load on the CLI path.
- **`references/criticmarkup.md`** — CriticMarkup syntax and Adeu's semantic projections (footnotes, cross-refs, anchors, defined-terms appendix). Load when interpreting raw `read_docx` output or when the user asks about a marker like `[^fn-3]` or `{#_BookmarkName}`.
