# Redline anchoring, comment and report fixes

## Context

Seven gaps reproduced on `main` (2026-11-05), plus one corruption bug: an
edit that keeps a cross-reference or a bold hyperlink writes the projection
markup (`[~2.1~](#_Ref123)`, `[**x**](mailto:x)`) into the document as literal
text and deletes the field result / link text. Every behaviour change ships in
BOTH engines (Python `python/src/adeu`, Node `node/packages/core/src`) per
AGENTS.md "Dual-Engine Parity", except where one engine is already correct.

## Assumptions

- Seams under test are the public ones the existing suites already use:
  `RedlineEngine.process_batch` / `apply_edits` + the saved DOCX XML (both
  engines), `DocumentMapper.find_match_index` (Python mapper tests),
  `trim_common_context` (unit, both engines), and the `ModifyText` model
  (Python) / Zod tool schema (Node MCP).
- Commented edits stay atomic per segment (decision `4e3655f`). Where an edit
  must be split around a structural marker, the comment goes on the first
  changed segment — the precedent the table-cell and paragraph splits already
  set (`engine.py` `_resolve_single_match`).
- T7 keeps `comment: str` fully backward compatible and adds the list form
  `[{text, author?}]`. A per-comment `author` falls back to the engine author.
- One commit per task on `main`, pushed after the pre-push hook (full Python /
  Node suites) passes.

## Tasks

### T1 — Comments root keeps its namespace declarations — DONE
- Goal: adding a comment to a document whose existing `comments.xml` declares
  extra namespaces neither crashes nor drops `mc:Ignorable` prefixes.
- Files: `python/src/adeu/redline/comments.py` (`_ensure_namespaces`),
  `node/packages/core/src/comments.ts` (`_ensureNamespaces`), tests
  `python/tests/test_comments.py`, `node/packages/core/src/comments.test.ts`.
- Change: Python rebuilds the root with lxml from `{**nsmap, **missing}` and
  merges `mc:Ignorable`; Node rebinds w14/w15/w16cid/w16cex/mc when absent or
  bound elsewhere and merges `mc:Ignorable`.
- Verify: new tests (hyperlinked existing comment with `r:id`; existing
  `mc:Ignorable="w16se"` survives with w14 w15 w16cid w16cex appended; w14
  bound to another URI is rebound; already-correct root left untouched).
- Done when: both suites green.

### T2 — Structural markers survive markup-path edits — DONE
- Goal: an edit that keeps a cross-reference or hyperlink unchanged edits only
  the text around it; the field / link and its result stay intact, no literal
  `[~…~](#…)` / `[…](…)` text is written.
- Files: `engine.py` `_resolve_single_match`, `engine.ts` twin; new tests
  `python/tests/test_repro_marker_preserving_edits.py`,
  `node/packages/core/src/repro.marker-preserving-edits.test.ts`.
- Change: when target and replacement carry the same sequence of structural
  markers (links, cross-references, images), split the edit at the markers and
  resolve each changed gap on its own (recursively through the normal
  resolver).
- Verify: cross-ref doc, `Section [~2.1~](#_Ref123), nothing else…` →
  `…, no other licence…`: no literal markup, REF field result `2.1` not
  deleted, accept-all text correct. Same for a bold hyperlink.
- Done when: both suites green.

### T3 — Plain anchors resolve across hyperlinks, cross-refs, bookmarks — DONE
- Goal: `"Email the team at legal@adeu.com before Friday."` and
  `"…granted in Section 2.1, nothing else…"` resolve, and the link / field is
  preserved in the redline.
- Files: `mapper.py` `_get_plain_projection` / `_find_plain_projection_matches`,
  `mapper.ts` twins, `engine.py` + `engine.ts` (`_pre_resolve_heuristic_edit`:
  restore markers into the replacement), tests in the T2 repro files.
- Change: the plain projection also drops link syntax (`[`, `](url)`), xref
  syntax (`[~`, `~](#…)`) and bookmark anchors (`{#…}`); matches widen to
  whole markers. The resolver re-injects each marker into the caller's
  replacement where its visible text survives (diff-aligned); a marker whose
  visible text the caller changed/removed fails with a clear error.
- Verify: plain-anchor tests in the repro files; field/link intact.
- Done when: both suites green.

### T4 — Punctuation-only changes do not restate the word — DONE
- Goal: `Cause.` → `Cause;` deletes only `.`; money, decimals, URLs, domains
  and `text.`→`document.` still trim as before.
- Files: `python/src/adeu/diff.py` + `node/packages/core/src/diff.ts`
  (`trim_common_context`), tests `python/tests/test_context_trimming.py`, Node
  diff tests.
- Change: a word↔terminal-punctuation (`.,;!?`) boundary is a clean prefix
  cut unless the punctuation sits between two word characters. `:` is left
  out: it would split `https://` URLs.
- Verify: unit + guard cases; commented end-to-end case.
- Done when: both suites green (incl. cross-platform goldens).

### T5 — Comment spans every paragraph added after the target — DONE
- Goal (Python only; Node already correct): the comment range runs from the
  first added paragraph to the last.
- Files: `engine.py` `_apply_insertion_op`; test `python/tests/test_comments.py`.
- Verify: appended paragraphs with both `\n` and `\n\n` separators.
- Done when: Python suite green.

### T6 — Per-edit reports carry the caller's index — DONE
- Goal: each `edits[]` report has `index` = position in the caller's change
  list (mixed action/edit batches). Minimal reports keep it on failed edits
  only: the applied-edit 40-token budget covers the whole serialized edit and
  the index would shave the redline preview.
- Files: `engine.py` `_build_edit_report` + failed-report literals, `engine.ts`
  twin; tests in both engines.
- Done when: both suites green.

### T7 — Several comments per edit, each with its own author — DONE
- Goal: `comment` accepts a string or a list of `{text, author?}`; each entry
  becomes its own comment on the same anchor; Node rejects a malformed
  `comment` with a validation error instead of a TypeError.
- Node MCP publishes `comment` as a plain string (clients strip property
  unions to `{}`, see `repro.qa_2026_07_23.client-compat.test.ts`); the list
  form is runtime tolerance parked under a marker key past the schema. Live
  Word joins a list into one comment under the session author (COM takes one
  string per range).
- Files: `python/src/adeu/models.py`, engine comment attach paths, markup
  preview, MCP tool schemas (Python + Node), `node/packages/core` types +
  engine, n8n/langchain only if they re-declare the schema, `GEMINI.md`.
- Done when: both suites green; existing string comments unchanged.

## Risks

- T2/T3 touch the resolver hot path; conformance goldens
  (`consistency.test.ts`, `test_cross_platform_consistency.py`) guard parity.
- T4 changes `trim_common_context`, which feeds `markup.py`, MCP diff output and
  live Word; goldens may need regeneration only if a change is a genuine
  improvement (reported, never silently rebaselined).
- T7 is a public schema change across five surfaces.

PLAN COMPLETE
