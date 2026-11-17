# Plan: Editable Text Boxes (`w:txbxContent`) in Python and Node Engines (Issue #194)

## Context

GitHub issue [#194](https://github.com/dealfluence/adeu/issues/194) reports that Word text boxes (`w:txbxContent` inside DrawingML `wps:txbx` and VML `v:textbox` containers) are commonly used on contract cover pages, letterheads, signature blocks, and sidebars, yet cannot be edited in Adeu:

1. Since QA 2026-07-23 C4 (`python/src/adeu/utils/docx.py:121-173,1407-1431`, `node/packages/core/src/utils/docx.ts:91-144,1582-1606`), `w:txbxContent` is disclosed inline as a read-only virtual image marker `![Text box: …](docx-image:<id>)` truncated at 300 characters (`_TEXTBOX_DISCLOSURE_CAP = 300`), while `docs/FIDELITY.md:41` still lists text boxes as completely omitted.
2. `DocumentMapper` emits that disclosure via `_add_virtual_text(..., is_image_marker=True)` (`run = None`), so `drop_virtual_only_matches` discards matches inside the box and `process_batch` fails with `Target text not found in document` (or `the target overlaps a read-only image marker` when spanning adjacent prose).
3. Existing tracked deletions (`w:delText`) and insertions (`w:ins`) inside a counterparty-redlined text box are silently collapsed to plain `w:t` text by `_textbox_text`, hiding pending redlines in `clean_view=False`.
4. Live Microsoft Word COM and `ooxml-integrity` verification confirm:
   - Paragraphs inside `w:txbxContent` are ordinary `<w:p>` elements supporting `<w:ins>`, `<w:del>`, format changes, tables, and content controls (`w:sdt`).
   - Word refuses comments, footnotes, and endnotes inside text boxes (`Comments, endnotes and footnotes can only be added to the main story.`, COM `-2146822353`).
   - Word 2010+ wraps each floating text box in `<mc:AlternateContent>` containing both `<mc:Choice Requires="wps">…<wps:txbx><w:txbxContent>` and `<mc:Fallback><w:pict><v:shape … o:gfxdata="…"><v:textbox><w:txbxContent>` (where `o:gfxdata` holds geometry only, no text). Blanket `.iter()` / `.findall(".//...")` walks currently visit both branches, double-counting revisions and content controls.
   - ECMA-376 `CT_TxbxContent` requires at least one block-level child (`minOccurs="1"`), matching `<w:tc>`.

**Architecture (deep seam at `iter_document_parts_with_kind`):**
Model each active `<w:txbxContent>` as a `TextboxPart` yielded with `kind = "textbox"` by `iter_document_parts_with_kind` immediately after its owning part (`header`, `body`, `footer`), preferring `<mc:Choice>` over `<mc:Fallback>` inside `<mc:AlternateContent>`. Because `ingest`, `DocumentMapper`, `normalize_docx`, `diff`, `outline`, and `fields` all consume `iter_document_parts_with_kind`, every `<w:txbxContent>` automatically projects full Markdown + CriticMarkup, maps run-backed spans, enforces hard container walls via `part_ranges`, and supports `ModifyText`, `SetField`, table row ops, `AcceptChange`/`RejectChange`, `diff`, and `sanitize`, while surgically mirroring mutated `<mc:Choice>` `<w:txbxContent>` trees into paired `<mc:Fallback>` `<w:txbxContent>` nodes with fresh unique revision `w:id`s and signed-int32 `w14:paraId`/`w14:textId` values.

## Assumptions

- Each active `<w:txbxContent>` is an isolated structural container (`kind = "textbox"`): a single edit cannot span across `body → textbox`, `header → textbox`, or `textbox → textbox` boundaries.
- Comments cannot be anchored inside `w:txbxContent`: comment-only edits inside a text box fail validation clearly (matching headers/footers/notes), and modify+comment edits inside a text box apply the tracked text change while recording a non-fatal warning that Word does not support comments in a textbox part.
- Within `<mc:AlternateContent>`, `<mc:Choice>` is authoritative whenever it carries a graphic element (`w:drawing`, `w:object`, `w:pict`); `<mc:Fallback>` is used only when no `<mc:Choice>` carries a graphic payload, and bare `<w:drawing>` / `<w:pict>` text boxes (without `<mc:AlternateContent>`) are projected and edited directly.
- Pure text-box shapes (shapes carrying `<w:txbxContent>` and no embedded image `<a:blip>` / `<v:imagedata>` / `<w:object>`) no longer emit a duplicate inline `![Text box: …](docx-image:…)` marker, because their full content is projected in their `TextboxPart`. Actual images (`![alt](docx-image:N)`) and watermark `v:textpath` shapes retain their existing behavior.
- Untouched `<mc:AlternateContent>` elements are never rewritten (Surgical Mode); only `<mc:AlternateContent>` blocks whose `<mc:Choice>` `<w:txbxContent>` structurally diverges from `<mc:Fallback>`'s `<w:txbxContent>` are synchronized.
- Python (`adeu`) and Node (`@adeu/core`) remain in strict behavioral parity. All work is committed one task per commit on `main` without pushing.

## Tasks

### Task 1: Python Projection, Mapping & Content-Control Ordinals for `w:txbxContent`
Status: DONE

- **Goal:** Project and map full text-box contents (`w:txbxContent`) across headers, body, and footers in Python without 300-char truncation, preserving CriticMarkup, formatting, tables, and content-control ordinals while keeping `ingest` and `DocumentMapper` in exact Virtual Text sync.
- **Files:**
  - `python/src/adeu/utils/docx.py`
  - `python/src/adeu/utils/content_controls.py`
  - `python/src/adeu/ingest.py`
  - `python/src/adeu/redline/mapper.py`
  - `python/tests/test_textbox_editing.py`
- **Change:**
  1. Add `TextboxPart` (`_element`, `part`, `in_deleted_anchor`, `in_inserted_anchor`) and `iter_textbox_contents(root_elm)` in `python/src/adeu/utils/docx.py`, descending `<mc:Choice>` (when graphic-bearing) instead of `<mc:Fallback>`.
  2. Update `iter_document_parts_with_kind` to yield `(TextboxPart(...), "textbox")` in document order immediately after each owning header, body, and footer part, and teach `iter_block_items` to unwrap `TextboxPart._element`.
  3. Update `_graphic_marker_events` so shapes carrying `w:txbxContent` without an actual image (`a:blip` / `v:imagedata` / `w:object`) do not emit a duplicate `![Text box: …](docx-image:…)` marker.
  4. Honour `TextboxPart.in_deleted_anchor` (`clean_view`) and `TextboxPart.in_inserted_anchor` (`original_view`) in `ingest.py` and `mapper.py`.
  5. Update `iter_sdt_elements_in_order` in `python/src/adeu/utils/content_controls.py` to skip inactive `<mc:Fallback>` branches and defer `<w:txbxContent>` subtrees until their own `TextboxPart` is scanned, so SDT ordinals run `1..N` once in exact projection order.
- **Verify:** `uv run pytest tests/test_textbox_editing.py tests/test_repro_qa_customer_assessment_2026_07_23.py tests/test_repro_qa_2026_07_18.py tests/test_twin_projection_parity.py && uv run ruff check . && uv run ruff format --check . && uv run mypy src` (in `python/`).
- **Done when:** Reading a DOCX with modern `mc:AlternateContent` or legacy VML text boxes (including >300-character prose, multiple paragraphs, tables, existing `<w:ins>`/`<w:del>` redlines, headers/footers, and content controls) returns the complete text with CriticMarkup in `clean_view=False`, clean text in `clean_view=True`, zero duplicate text or false image markers, and `DocumentMapper.full_text` matches `_extract_text_from_doc(..., include_appendix=False)` character-for-character.

### Task 2: Python RedlineEngine Editing, Container Guards, `mc:Fallback` Sync & Sanitize Parity
Status: DONE

- **Goal:** Enable tracked editing, review actions, field writes, diffing, and sanitization inside `w:txbxContent` in Python while enforcing container boundaries, Word's comment restriction, `CT_TxbxContent` minimum-paragraph schema validity, and surgical `<mc:Fallback>` synchronization.
- **Files:**
  - `python/src/adeu/utils/docx.py`
  - `python/src/adeu/redline/engine.py`
  - `python/src/adeu/sanitize/transforms.py`
  - `python/tests/test_textbox_editing.py`
- **Change:**
  1. Add `is_in_inactive_mc_fallback(el)` and `sync_alternate_content_fallbacks(roots, next_rev_id_fn)` in `python/src/adeu/utils/docx.py` to detect inactive `<mc:Fallback>` descendants and surgically clone diverged `<mc:Choice>` `<w:txbxContent>` trees into paired `<mc:Fallback>` `<w:txbxContent>` elements with fresh revision `w:id`s and fresh signed-int32 `w14:paraId`/`w14:textId` attributes.
  2. In `python/src/adeu/redline/engine.py`:
     - Extend `_comment_anchor_in_main_story` / `_skip_comment_outside_main_story` to treat `w:txbxContent` ancestors as `"textbox"` (non-main story), warning and dropping comment attachments on modify+comment while validation refuses comment-only edits in `"textbox"` parts.
     - Extend `_is_last_paragraph_in_cell` to also guard the last `<w:p>` inside `<w:txbxContent>` so accepting/rejecting a full deletion leaves an empty valid `<w:p/>` instead of `<w:txbxContent/>`.
     - Filter inactive `<mc:Fallback>` elements out of `_story_findall`, `_parts_holding_id`, `accept_all_revisions`, and `reject_all_revisions` counts/queries (keeping `_scan_existing_ids` scanning all elements so new `w:id`s never collide), and run `sync_alternate_content_fallbacks` across mutated story roots after edits/review actions/bulk accept-or-reject.
  3. In `python/src/adeu/sanitize/transforms.py`, filter inactive `<mc:Fallback>` elements when counting/reporting in `count_tracked_changes`, `get_track_change_authors`, and `accept_all_tracked_changes`, and sync fallbacks after accepting tracked changes.
- **Verify:** `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` (in `python/`).
- **Done when:** Users can modify text, split/merge/delete paragraphs, edit tables, set fields, diff/revise, sanitize, and accept/reject tracked changes inside body and header/footer text boxes; cross-boundary (`body → textbox`, `textbox → textbox`) and comment-only edits fail with clear diagnostics; both `mc:Choice` and `mc:Fallback` carry matching redlines/accepted text with zero duplicate `w:id` or `w14:paraId` values; untouched shapes stay byte-identical; and `ooxml_integrity.check()` passes with zero findings.

### Task 3: Node (`@adeu/core`) Projection, Mapping & Content-Control Ordinals for `w:txbxContent`
Status: DONE

- **Goal:** Bring `@adeu/core` projection, `DocumentMapper`, and content-control ordinal assignment to full parity with Task 1 for `w:txbxContent`.
- **Files:**
  - `node/packages/core/src/docx/primitives.ts`
  - `node/packages/core/src/utils/docx.ts`
  - `node/packages/core/src/utils/content-controls.ts`
  - `node/packages/core/src/ingest.ts`
  - `node/packages/core/src/mapper.ts`
  - `node/packages/core/src/textbox_editing.test.ts`
- **Change:**
  1. Add `TextboxPart` in `node/packages/core/src/docx/primitives.ts` and `iter_textbox_contents` in `node/packages/core/src/utils/docx.ts`.
  2. Update `iter_document_parts_with_kind`, `iter_block_items`, and `_graphic_marker_events` in `node/packages/core/src/utils/docx.ts` to yield `[TextboxPart, "textbox"]` after owning headers, body, and footers and suppress duplicate inline `![Text box: …](docx-image:…)` markers on pure text-box shapes.
  3. Honour `TextboxPart.in_deleted_anchor` (`cleanView`) and `TextboxPart.in_inserted_anchor` (`original_view`) in `ingest.ts` and `mapper.ts`.
  4. Update `iterSdtElementsInOrder` in `node/packages/core/src/utils/content-controls.ts` to skip inactive `mc:Fallback` branches and defer `w:txbxContent` subtrees to their own `TextboxPart`.
- **Verify:** `npm run build && npm --workspace=@adeu/core run test -- src/textbox_editing.test.ts src/repro_qa_customer_assessment_2026_07_23.test.ts src/repro_qa_report_2026_07_18.test.ts && npm run lint` (in `node/`).
- **Done when:** `@adeu/core` extracts and maps `w:txbxContent` across modern `mc:AlternateContent`, bare `w:drawing`, legacy VML `w:pict`, headers/footers, redlines, tables, and content controls identically to Python, with `mapper.full_text === _extractTextFromDoc(doc, false, false)`.

### Task 4: Node (`@adeu/core`) RedlineEngine Editing, Container Guards, `mc:Fallback` Sync & Sanitize Parity
Status: PENDING

- **Goal:** Bring `@adeu/core` `RedlineEngine`, `diff`, and `sanitize` to full parity with Task 2 for `w:txbxContent`.
- **Files:**
  - `node/packages/core/src/utils/docx.ts`
  - `node/packages/core/src/engine.ts`
  - `node/packages/core/src/sanitize/transforms.ts`
  - `node/packages/core/src/textbox_editing.test.ts`
- **Change:**
  1. Port `is_in_inactive_mc_fallback` and `sync_alternate_content_fallbacks` to `node/packages/core/src/utils/docx.ts` (generating fresh unique revision `w:id`s and signed-int32 `w14:paraId`/`w14:textId` attributes on mirrored fallback subtrees).
  2. Update `node/packages/core/src/engine.ts`:
     - Teach `_comment_anchor_in_main_story` / `_skip_comment_outside_main_story` to treat `w:txbxContent` as `"textbox"` outside the main story.
     - Extend `_is_last_paragraph_in_cell` to protect the last `<w:p>` inside `w:txbxContent`.
     - Filter inactive `mc:Fallback` elements in `_story_findall`, `_parts_holding_id`, `accept_all_revisions`, and `reject_all_revisions` while keeping `_scan_existing_ids` scanning all elements, and invoke `sync_alternate_content_fallbacks` after mutations.
  3. Update `node/packages/core/src/sanitize/transforms.ts` (`count_tracked_changes`, `get_track_change_authors`, `accept_all_tracked_changes`) to ignore inactive `mc:Fallback` duplicates when counting/reporting and keep `mc:Fallback` synchronized.
- **Verify:** `npm run build && npm run test && npm run lint` (in `node/`).
- **Done when:** All text-box editing, review, field, diff, and sanitize operations in `@adeu/core` match Python's output, diagnostics, revision counts, and `findDuplicateParaIds` / schema invariants with zero regressions across the full Node workspace test and lint suites.

### Task 5: Documentation & Full Monorepo Verification
Status: PENDING

- **Goal:** Align `docs/FIDELITY.md` and `AI_CONTEXT.md` with editable `w:txbxContent` support, finalize plan status, and verify Python, Node, and LangChain suites end-to-end.
- **Files:**
  - `docs/FIDELITY.md`
  - `AI_CONTEXT.md`
  - `docs/plans/2026-11-17-issue-194-textbox-editing.md`
- **Change:**
  1. Update `docs/FIDELITY.md` to split editable text boxes (`w:txbxContent` — full read/write tracked changes, isolated container walls, no comments per Word story rules, synced `mc:Choice` + `mc:Fallback`) from still-omitted SmartArt / non-textbox drawing graphics.
  2. Document the `TextboxPart` container wall and surgical `mc:Fallback` mirror invariant in `AI_CONTEXT.md`.
  3. Mark all tasks `DONE` in `docs/plans/2026-11-17-issue-194-textbox-editing.md` and run full verification across `python/`, `node/`, and `langchain/`.
- **Verify:** Python (`uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`), Node (`npm run build && npm run test && npm run lint`), and LangChain (`uv run ruff check . && uv run ruff format --check . && uv run mypy langchain_adeu && uv run pytest`).
- **Done when:** Documentation accurately states user-facing text-box capabilities and limits, and all Python, Node, and LangChain test, lint, and typecheck suites pass with zero errors.

## Risks

- **Grouped shapes (`wpg:wgp` / `v:group`) with multiple text boxes inside one `<mc:AlternateContent>`:** A group can hold several `wps:txbx` elements in `mc:Choice` and several `v:textbox` elements in `mc:Fallback`. Mitigation: pair `mc:Choice` and `mc:Fallback` text boxes by shape name (`wp:docPr/@name` ↔ `v:shape/@id`) first, then 1-to-1 positionally when counts match, and drop an unpairable `<mc:Fallback>` rather than leaving stale contradictory text.
- **Duplicate `w14:paraId` or `w:id` across `mc:Choice` and `mc:Fallback`:** Cloning `<w:txbxContent>` verbatim would duplicate `w14:paraId` (violating `[MS-DOCX] 2.6.2.4` and `find_duplicate_para_ids`) and `w:id` across revision tags. Mitigation: rewrite `w14:paraId`/`w14:textId` on cloned fallback paragraphs/rows to fresh signed-int32 hex IDs (`0x00000001..0x7FFFFFFF`, collision-checked against the part) and mint fresh `w:id`s for cloned revision elements.
- **Live Word COM (`live_word.py`) scope:** Issue #194 targets the OOXML engine in Python and Node (`w:txbxContent`). Windows COM `doc.Content` / `doc.Revisions` only covers the main story unless `Shape.TextFrame` ranges are separately traversed; Live Word COM shape-story editing remains out of scope for this change and will be noted in the final report.

PLAN COMPLETE
