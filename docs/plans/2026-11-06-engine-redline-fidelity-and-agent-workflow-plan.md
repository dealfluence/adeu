# Adeu Engine Redline Fidelity, OOXML Compliance, and Agent Workflow Plan

## Context

An end-to-end audit of the Python (`adeu`) and Node.js (`@adeu/core`, `@adeu/mcp-server`) engines across multi-paragraph redlining, document-to-document comparison, OOXML schema validation, manual-markup workflows, and MCP agent tool interactions identified 11 concrete gaps:

1. **Tracked paragraph split pilcrow (`<w:pPr><w:rPr><w:ins/>`) placement (`engine.py`, `engine.ts`):**
   When an inline insertion contains paragraph breaks (`\n\n`) and splits an existing paragraph (`current_p`) so that trailing runs (`suffix_nodes`) are relocated to `last_p`, both engines stamp `<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>` onto `new_p[0..k-1]` (including `last_p`) and leave `current_p`'s `<w:pPr><w:rPr>` unmarked. In OOXML (ECMA-376 §17.3.1.29), `<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>` marks the paragraph break at the *end* of that `<w:p>`. Because `suffix_nodes` move to `last_p`, `last_p` ends with `current_p`'s *original* paragraph break, while `current_p` and `new_p[0..k-2]` end with the *newly inserted* breaks. Putting `<w:ins/>` on `last_p` instead of `current_p` causes Reject All (and Original-view readers) to leave `current_p` and `last_p` permanently split into two paragraphs and to merge `last_p` into the following untouched paragraph. In addition, `python/src/adeu/redline/engine.py:6091-6120` checks `_paragraph_has_visible_content(src_p)` *before* inserting `ins_elem` into `src_p`, erroneously stamping `<w:del/>` onto `src_p/w:pPr/w:rPr` when `ins_elem` is non-null.

2. **Destructive Phase-2 coalescing on whole-paragraph deletions (`engine.py`, `engine.ts`):**
   When an entire paragraph is deleted (`target_text = "Paragraph\n\n"`, `new_text = ""`), Phase 2 (`engine.py:6203-6249`, `engine.ts:8751-8820`) stamps `<w:del/>` onto `p1/w:pPr/w:rPr` *and* moves `p2`'s children into `p1` while removing `p2` from the DOM. When consecutive paragraphs are deleted, all of their `<w:del>` runs are collapsed into a single `<w:p>` element, destroying paragraph boundaries when revisions are rejected and leaving a dangling `<w:del/>` on the surviving paragraph's `<w:pPr><w:rPr>`. Both `ingest` and `DocumentMapper` (`clean_view=True`) and `accept_all_revisions` already handle standalone deleted paragraphs (`p1` kept as its own `<w:p>` with `<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>`).

3. **Document diff (`diff.py`, `diff.ts`) alignment, uniqueness, and Markdown delimiter balance:**
   - `make_edits_self_contained` (`diff.py:647-656`, `diff.ts:624-634`) checks `sim_text.count(candidate) == 1` without checking `original_text.count(candidate) == 1`. When a repeated token is replaced twice, the first edit mutates `sim_text` so the second edit appears unique in `sim_text` without widening; when `RedlineEngine.apply_edits` resolves the batch against `self.mapper` (`original_text`), the second edit resolves to the first occurrence's offset and is skipped as an overlapping edit.
   - When `make_edits_self_contained` widens or coalesces hunks whose boundaries land inside `**...**`, `__...__`, or `_..._`, the resulting `target_text`/`new_text` carries an odd number of emphasis delimiters; `_parse_inline_markdown` then emits literal `**` characters and drops bold/italic inheritance.
   - `generate_edits_via_paragraph_alignment` (`diff.py:1441-1467`, `diff.ts:1380-1415`) joins unequal-length `replace` blocks with `"\n\n"` without sub-aligning paragraphs by word similarity, producing cross-paragraph partial merges or wholesale multi-paragraph rewrites instead of pairing corresponding paragraphs.
   - `generate_edits_from_text` and `generate_edits_via_paragraph_alignment` populate `comment="Diff: ..."` on returned `ModifyText` objects, which programmatic callers apply as Word margin comments unless stripped.
   - `python/src/adeu/domain.py:11` (`_get_paragraph_text`) iterates `p.runs` (direct `<w:r>` children of `<w:p>` only, missing `<w:r>` inside `<w:ins>`), whereas `node/packages/core/src/domain.ts:41` traverses all `<w:r>` descendants.

4. **No direct redline output option in `diff_docx_files` (MCP) / `adeu diff` (CLI):**
   Comparing two `.docx` files via MCP currently returns only a text diff, forcing agents to either shell out to a two-step CLI pipeline or manually construct dozens of `ModifyText` edits in `process_document_batch`.

5. **OOXML schema validation and Word repair-prompt triggers (`engine.py`, `engine.ts`, `comments.py`, `comments.ts`):**
   - **Undeclared `mc:Ignorable` prefixes:** Saving a document whose `<w:document>` (or header/footer/comments root) lists prefixes in `mc:Ignorable` (such as `w14`, `wp14`, `w15`, `w16cid`, `w16cex`, `w16sdtdh`, `w16du`) without matching `xmlns:<prefix>` declarations triggers Microsoft Word's repair prompt on open.
   - **`CT_RPr` child order in Node (`engine.ts:3317-3360`):** `_apply_run_props` appends `<w:b/>` and `<w:i/>` at the end of `<w:rPr>` (after `<w:color/>` and `<w:sz/>`), violating ECMA-376 `CT_RPr` sequence order.
   - **`CT_ParaRPr` child order in Python and Node:** `<w:ins/>` and `<w:del/>` inside `<w:pPr><w:rPr>` are appended at the end of `<w:rPr>` (after `<w:rFonts/>`/`<w:sz/>` or `<w:ins/>` after `<w:del/>`), violating `CT_ParaRPr` where `w:ins` then `w:del` must come first before `EG_RPrBase`.
   - **`<w:pPr>` placement and empty property containers:** `comments.py:867` calls `p.append(pPr)` instead of `p.insert(0, pPr)`, and stripping formatting or revision marks can leave empty `<w:rPr/>` or `<w:pPr/>` elements.

6. **No manual / inline-formatting markup mode (`markup_mode="manual"`):**
   Workflows that prohibit Word Track Changes (`<w:ins>`/`<w:del>`) and require inline formatting markup (strikethrough deletions + bold/underlined/colored insertions with native Word comments) cannot use `process_document_batch` or `adeu apply`.

7. **No table creation or new-document creation from Markdown:**
   `process_document_batch` only supports `insert_row` / `delete_row` on existing tables. Inserting a new summary table at the top of an agreement or creating a companion `.docx` memo requires raw `python-docx` scripting outside Adeu's validated XML pipeline.

8. **Fuzzy whitespace newline mismatch in `trim_common_context`:**
   When `edit.target_text` and `edit.new_text` both contain `\n` or `\n\n` at the same prefix/suffix position that matched an inline space `" "` in `actual_target_text` via fuzzy whitespace matching, `trim_common_context` treats `" "` vs `"\n\n"` as an intentional paragraph split.

9. **`read_docx` ergonomics and parity gaps:**
   - Node's regex compiler (`response-builders.ts`, `mapper.ts`, `engine.ts`) rejects PCRE inline flags `(?i)`, `(?m)`, `(?s)` at the start of patterns (`Invalid regular expression: /(?i).../: Invalid group`), whereas Python's `re` accepts them.
   - `read_docx` in search mode rejects page ranges (`page="4-5"`) and quoted page strings (`page="'all'"`).
   - `build_page_range_response` (`_response_builders.py:457`, `response-builders.ts:710`) omits the `"Continues on page X of Y"` footer when `last == end` and `last < total_pages` (e.g., `page="1-8"` on a 14-page document).
   - `extract_outline` (`outline.py`, `outline.ts`) misses standalone Title-Case bold structural section headings (`**Paragraph 1 — Interpretation**`, `**Section 2.01. Form and Dating.**`, `**1. Transaction Terms**`), returning `0 of 0 headings` on contracts that do not use Word `Heading` styles or ALL-CAPS headings.

10. **Derivative filename in-place resolution & batch verification receipts:**
    - Omitting `output_path` only updates in place when the input stem ends with `_processed` or `_redlined`, spawning `*.redline_processed.docx` when given `*.redline.docx` or `*-redline.docx`.
    - `formatBatchResult` omits minted `Chg:N` / `Com:N` IDs on applied edits, omits previews/receipts on applied `accept`/`reject`/`reply` actions, and omits remaining tracked-change/comment counts.

11. **Skill prompt (`skills/adeu-redlining/`) turn overhead:**
    - `SKILL.md` instructs agents to read `references/mcp-tools.md` before every batch, re-read the document with `clean_view=true` after every batch despite inline previews, and does not warn against blind `page="all"` calls on multi-page documents.

---

## Assumptions

- All parsing, diffing, redlining, validation, and MCP response changes must maintain strict dual-engine parity between Python (`python/src/adeu`) and Node.js (`node/packages/core`, `node/packages/mcp-server`), plus `langchain/` where tool parameters are wrapped.
- Existing golden projection fixtures (`python/tests/test_projection_goldens.py`, `shared/conformance/`) and existing regression suites (`test_repro_chg_id_coalescing.py`, `test_response_budget_guard.py`, `budget-guard.test.ts`, `spec_2026_07_28.test.ts`) must remain 100% green without weakening any test.
- Each task will be implemented test-first (reproducing the failure in both Python and Node before applying the fix) and committed atomically.

---

## Tasks

### Task 1: Fix Tracked Paragraph Split Pilcrow (`<w:pPr><w:rPr><w:ins/>`) Placement and Rejection Merging

- **Status:** `[x] Completed`
- **Goal:** Ensure that splitting an existing paragraph via `\n\n` in `new_text` places `<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>` on the paragraphs whose trailing breaks were newly inserted (`current_p` and intermediate `new_p`s) rather than on `last_p` when `suffix_nodes` are relocated, never stamps a spurious `<w:del/>` on `src_p` when `ins_elem` is present, and cleanly reconstructs the original paragraph when revisions are rejected.
- **Files:**
  - `python/src/adeu/redline/engine.py`
  - `node/packages/core/src/engine.ts`
  - `python/tests/test_paragraph_split_reject_parity.py` (new or existing test module)
  - `node/packages/core/src/paragraph_split_reject_parity.test.ts`
- **Change:**
  1. In `python/src/adeu/redline/engine.py:6091-6122`:
     - Move the `if ins_elem is not None:` insertion into `parent` *before* checking `if last_p is not None and ins_elem is None:` (or guard the `src_p` `<w:del/>` pilcrow mark with `if last_p is not None and ins_elem is None:`, matching `node/packages/core/src/engine.ts:8710`). This prevents stamping `<w:del/>` onto `src_p/w:pPr/w:rPr` when `ins_elem` (the first line of an inline multiline replacement) is inserted into `src_p`.
  2. In `python/src/adeu/redline/engine.py:1671-1740` (`track_insert`) and `node/packages/core/src/engine.ts:2920-2995` (`_track_insert_multiline`):
     - Determine `has_relocated_suffix = bool(suffix_nodes)` (before `suffix_nodes` are moved from `current_p` to `last_p`).
     - When `has_relocated_suffix` is `True`:
       - `last_p` (`new_p` for `i == len(remaining_lines) - 1`) receives `suffix_nodes` and therefore ends with `current_p`'s *original* paragraph break. Do **not** add a new `<w:ins/>` mark to `last_p`'s `w:pPr/w:rPr`; instead, if `current_p`'s `w:pPr/w:rPr` already carried a pre-existing pilcrow revision mark (`w:ins` or `w:del`), transfer it to `last_p`'s `w:pPr/w:rPr`.
       - Stamp `<w:ins/>` onto `current_p`'s `w:pPr/w:rPr` and onto each intermediate `new_p` (`0 <= i < len(remaining_lines) - 1`).
     - When `has_relocated_suffix` is `False` (pure append at the end of `current_p`):
       - Keep `current_p`'s `w:pPr/w:rPr` unchanged and stamp `<w:ins/>` onto each newly created `new_p` (`0 <= i < len(remaining_lines)`).
  3. In `python/src/adeu/redline/engine.py` (`_reject_change` lines 7204-7223 and `reject_all_revisions` lines 7626-7640) and `node/packages/core/src/engine.ts` (`_reject_change` lines 7074-7105 and `reject_all_revisions` lines 7500-7535):
     - After removing run-level `<w:ins>` elements, handle `<w:ins/>` inside `<w:pPr>/<w:rPr>` consistently in both `_reject_change` and `reject_all_revisions`:
       - Remove the `<w:ins/>` mark from `<w:rPr>`.
       - If `p_el` has no remaining visible/untracked runs or `<w:del>` runs (and is not the sole paragraph in a table cell/body), remove `p_el` from its parent.
       - If `p_el` still has content (because `p_el` was split and its `suffix_nodes` live in the next sibling paragraph `p_next`), merge `p_next`'s non-`pPr` children to the end of `p_el`, transfer `p_next`'s `w:pPr/w:rPr` pilcrow state to `p_el`, and remove `p_next`.
- **Verify:**
  - `cd python && uv run pytest tests/ -k "paragraph_split or reject"`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Replacing a prefix or interior span of a paragraph with multi-paragraph `new_text` (`"Line 1\n\nLine 2"`) and then rejecting the change (via `reject_all_revisions()`, `RejectChange`, or reading non-`<w:ins>` runs separated by non-`<w:ins>` pilcrows) yields the exact original single paragraph in both Python and Node.

---

### Task 2: Preserve Paragraph Containers on Whole-Paragraph Tracked Deletions

- **Status:** `[x] Completed`
- **Goal:** Represent whole-paragraph tracked deletions (`target_text = "Paragraph\n\n"`, `new_text = ""`) using standard OOXML (`<w:del>` around the paragraph's runs plus `<w:del/>` inside `<w:pPr><w:rPr>`) without destructively coalescing `p2` into `p1` at edit time.
- **Files:**
  - `python/src/adeu/redline/engine.py`
  - `node/packages/core/src/engine.ts`
  - `python/tests/test_whole_paragraph_delete_reject.py`
  - `node/packages/core/src/whole_paragraph_delete_reject.test.ts`
- **Change:**
  1. In `python/src/adeu/redline/engine.py:6203-6268` and `node/packages/core/src/engine.ts:8751-8840` (`PHASE 2: OOXML Paragraph Merge Protocol`):
     - When `p1_fully_deleted` is `True` and `(op == EditOperationType.DELETION or not edit.new_text)`:
       - Stamp `<w:del/>` onto `p1_element`'s `<w:pPr><w:rPr>` (using canonical `CT_ParaRPr` insertion order).
       - Do **not** overwrite `p1_element`'s `<w:pPr>` with `p2_element`'s `<w:pPr>`, do **not** move `p2_element`'s children into `p1_element`, and do **not** remove `p2_element` from the DOM.
     - When `p1_fully_deleted` is `False` and `p2_fully_deleted` is `True` and `(op == EditOperationType.DELETION or not edit.new_text)` (e.g., deleting the final paragraph of a document via `target_text = "\n\nLast paragraph"`, `new_text = ""`):
       - Stamp `<w:del/>` onto `p2_element`'s `<w:pPr><w:rPr>` (via `affected_ps`).
       - Do **not** stamp `<w:del/>` onto `p1_element`'s `<w:pPr><w:rPr>` and do **not** coalesce `p2_element` into `p1_element`.
     - When neither `p1` nor `p2` is fully deleted (`p1_fully_deleted == False` and `p2_fully_deleted == False`, i.e., a true mid-paragraph join `"tail of p1\n\nhead of p2" -> ""`):
       - Keep the existing merge behavior for `clean_view` / raw-view inline projection, and ensure `reject_all_revisions()` / `_reject_change()` splits at the boundary between `p1`'s `<w:del>` and `p2`'s `<w:del>`.
- **Verify:**
  - `cd python && uv run pytest`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Deleting 1 to 10 consecutive paragraphs as tracked changes and then rejecting all revisions (or inspecting Original view) restores every deleted paragraph as its own separate `<w:p>` with its original paragraph properties, while `clean_view=True` and `accept_all_revisions()` omit the deleted paragraphs completely.

---

### Task 3: Improve `diff.py` / `diff.ts` Paragraph Sub-Alignment, Context Uniqueness, Markdown Delimiter Balancing, and `domain.py` Run Traversal

- **Status:** `[x] Completed`
- **Goal:** Make `generate_structured_edits` / `generate_edits_from_docx` produce self-contained edits that apply with zero skipped/overlapping edits, zero unbalanced `**`/`_` artifacts, and clean paragraph-level alignment in both Python and Node.
- **Files:**
  - `python/src/adeu/diff.py`
  - `node/packages/core/src/diff.ts`
  - `python/src/adeu/domain.py`
  - `python/src/adeu/cli.py`
  - `python/tests/test_diff.py`
  - `node/packages/core/src/diff.test.ts`
- **Change:**
  1. **Uniqueness in both `sim_text` and `original_text` (`diff.py:647-656`, `diff.ts:624-634`):**
     - In `make_edits_self_contained`, require `sim_text.count(candidate) == 1 and original_text.count(candidate) == 1` in `_is_valid_candidate` so that batch resolution against `RedlineEngine.mapper` (`original_text`) never matches an earlier occurrence of the same token.
  2. **Balanced Markdown emphasis delimiters (`diff.py:647-760`, `diff.ts:624-745`):**
     - Add a helper `_has_balanced_markdown(s: str) -> bool` (checking even counts of `**`, `__`, and standalone `_`, matching `trim_common_context` in `diff.py:93-125`).
     - In `make_edits_self_contained`, if a candidate or coalesced hunk `[start, end]` has unbalanced `**`, `__`, or standalone `_` in `candidate` or `new_full`, expand `start` leftward and/or `end` rightward to include the partner delimiter (and update `_match_start_index` accordingly).
  3. **Similarity-based DP sub-alignment for unequal paragraph `replace` blocks (`diff.py:1441-1467`, `diff.ts:1380-1415`):**
     - In `generate_edits_via_paragraph_alignment`, when `tag == "replace"` and `i2 - i1 != j2 - j1`, align `orig_paragraphs[i1:i2]` against `mod_paragraphs[j1:j2]` using dynamic programming over word-level similarity (`SequenceMatcher(None, orig_p.split(), mod_p.split()).ratio()`, threshold `>= 0.35`):
       - Paired paragraphs `(orig_p, mod_p)` are diffed within the paragraph (exactly like the `1 == 1` branch).
       - Unpaired original paragraphs are emitted as whole-paragraph deletions (`orig_p + "\n\n" -> ""`).
       - Unpaired modified paragraphs are emitted as whole-paragraph insertions (`mod_p + "\n\n"` before the next anchor paragraph, or `"\n\n" + mod_p` after the preceding paragraph).
  4. **Do not set synthetic `comment="Diff: ..."` on generated edits (`diff.py`, `diff.ts`):**
     - Leave `comment=None` on `ModifyText` objects produced by `generate_edits_from_text` and `generate_edits_via_paragraph_alignment` so programmatic callers (`generate_structured_edits`, `generate_edits_from_docx`) do not inject `"Diff: Replacement"` Word comments into documents.
  5. **Python `domain.py` `_get_paragraph_text` parity with Node (`python/src/adeu/domain.py:11`):**
     - Update `_get_paragraph_text(p: Paragraph)` in `python/src/adeu/domain.py` to iterate `[Run(r, p) for r in p._element.findall(f".//{qn('w:r')}")]` instead of `p.runs`, matching `node/packages/core/src/domain.ts:41` (`findAllDescendants(p._element, "w:r")`) so defined terms inside `<w:ins>` are recognized in `clean_view=True`.
- **Verify:**
  - `cd python && uv run pytest tests/test_diff.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Running `generate_structured_edits` + `RedlineEngine.apply_edits` on complex multi-section contract revisions applies 100% of generated edits with `skipped == 0`, produces a `clean_view=True` projection (including appendix) identical to the revised document, and produces an Original-view / `reject_all_revisions()` projection identical to the original document.

---

### Task 4: One-Step Redline Comparison Output in `diff_docx_files` (MCP) and `adeu diff` (CLI)

- **Status:** `[x] Completed`
- **Goal:** Allow callers of `diff_docx_files` (Python MCP, Node MCP, LangChain) and `adeu diff` (CLI) to optionally pass `output_path` (`-o` / `--output`) and `author_name` (`--author`) to directly write a tracked-changes `.docx` comparing `original_path` against `modified_path` in a single call.
- **Files:**
  - `python/src/adeu/mcp_components/tools/document.py`
  - `node/packages/mcp-server/src/index.ts`
  - `python/src/adeu/cli.py`
  - `langchain/langchain_adeu/` (if `diff_docx_files` is exposed)
  - `python/tests/test_mcp_diff_output.py`
  - `node/packages/mcp-server/src/diff_output.test.ts`
- **Change:**
  1. In `python/src/adeu/mcp_components/tools/document.py` (`diff_docx_files`) and `node/packages/mcp-server/src/index.ts` (`diff_docx_files`):
     - Add optional parameters:
       - `output_path: Optional[str] = None` — `"Optional path to save a redlined .docx showing all changes from original_path to modified_path as native Word tracked changes."`
       - `author_name: str = "Comparison"` — `"Author name for tracked changes when output_path is provided."`
     - When `output_path` is omitted (`None`), preserve the exact existing unified text diff return behavior.
     - When `output_path` is provided:
       - Extract structure and text from `original_path` and `modified_path` (`clean_view=compare_clean, include_appendix=False`).
       - Generate structured edits via `generate_structured_edits`.
       - Apply them to `original_path` using `RedlineEngine(original_stream, author=author_name)` (`apply_edits`).
       - Write the resulting `.docx` to `output_path`.
       - Return a concise summary header (`Redline saved to: <output_path> (<applied> tracked changes applied, Author: <author_name>)`) followed by the unified diff summary.
  2. In `python/src/adeu/cli.py` (`cmd_diff`):
     - Add `-o` / `--output` and `--author` (default `"Comparison"`) flags to `adeu diff` so `adeu diff orig.docx rev.docx -o redline.docx --author "..."` generates and saves the redlined `.docx` in one command.
- **Verify:**
  - `cd python && uv run pytest tests/test_mcp_diff_output.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - A single `diff_docx_files(original_path, modified_path, output_path=..., author_name=...)` call produces a valid redlined `.docx` whose accepted view matches `modified_path` and whose rejected view matches `original_path`.

---

### Task 5: OOXML Schema Ordering (`CT_RPr`, `CT_ParaRPr`, `CT_P`), Empty Property Pruning, and `mc:Ignorable` Namespace Repair

- **Status:** `[x] Completed`
- **Goal:** Eliminate OpenXML schema validation errors (`Sch_UnexpectedElementContentExpectingComplex` on `w:b`, `w:i`, `w:ins`, `w:pPr`), prune empty `<w:rPr/>`/`<w:pPr/>` elements, and bind undeclared `mc:Ignorable` prefixes on save so Microsoft Word never prompts to repair the file.
- **Files:**
  - `python/src/adeu/redline/engine.py`
  - `python/src/adeu/redline/comments.py`
  - `node/packages/core/src/engine.ts`
  - `node/packages/core/src/comments.ts`
  - `python/tests/test_ooxml_schema_compliance.py`
  - `node/packages/core/src/ooxml_schema_compliance.test.ts`
- **Change:**
  1. **Repair undeclared `mc:Ignorable` prefixes on save (`engine.py:6335-6425`, `engine.ts:8845-8890`):**
     - Define the standard Office namespace map for `mc:Ignorable` prefixes:
       - `w14`: `http://schemas.microsoft.com/office/word/2010/wordml`
       - `wp14`: `http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing`
       - `w15`: `http://schemas.microsoft.com/office/word/2012/wordml`
       - `wp15`: `http://schemas.microsoft.com/office/word/2012/wordprocessingDrawing`
       - `w16cid`: `http://schemas.microsoft.com/office/word/2016/wordml/cid`
       - `w16cex`: `http://schemas.microsoft.com/office/word/2018/wordml/cex`
       - `w16sdtdh`: `http://schemas.microsoft.com/office/word/2020/wordml/sdtdatahash`
       - `w16du`: `http://schemas.microsoft.com/office/word/2023/wordml/word16du`
     - During `save_to_stream()`, inspect every XML part's root element for `mc:Ignorable`. For any prefix token in `mc:Ignorable` that is not bound on the root element:
       - If the prefix is in the known Office map, bind `xmlns:<prefix>="<uri>"` on the root element (`etree.cleanup_namespaces(root, top_nsmap=missing, keep_ns_prefixes=list(missing))` in Python; `root.setAttribute("xmlns:" + prefix, uri)` in Node).
       - If the prefix is unknown and unbound anywhere in the part, remove that token from `mc:Ignorable`.
  2. **Canonical `CT_RPr` child ordering in Node (`node/packages/core/src/engine.ts:3305-3375`):**
     - Implement `insertRPrChild(rPr: Element, child: Element)` in Node using the ECMA-376 `CT_RPr` sequence table (`rStyle`, `rFonts`, `b`, `bCs`, `i`, `iCs`, `caps`, `smallCaps`, `strike`, `dstrike`, `outline`, `shadow`, `emboss`, `imprint`, `noProof`, `snapToGrid`, `vanish`, `webHidden`, `color`, `spacing`, `w`, `kern`, `position`, `sz`, `szCs`, `highlight`, `u`, `effect`, `bdr`, `shd`, `fitText`, `vertAlign`, `rtl`, `cs`, `em`, `lang`, `eastAsianLayout`, `specVanish`, `oMath`, `rPrChange`).
     - Use `insertRPrChild` whenever adding `<w:b>`, `<w:bCs>`, `<w:i>`, `<w:iCs>`, `<w:u>`, `<w:strike>`, `<w:color>`, etc. to `<w:rPr>`, and ensure `<w:rPr>` is always inserted at index 0 of `<w:r>` (`runElement.insertBefore(rPr, runElement.firstChild)`).
  3. **Canonical `CT_ParaRPr` child ordering for `<w:pPr><w:rPr>` in Python (`engine.py`) and Node (`engine.ts`):**
     - Add helper `_insert_para_rpr_track_change(rPr, mark)` in both engines:
       - Per ECMA-376 `CT_ParaRPr`, `<w:ins>` must be inserted at index 0 of `<w:pPr><w:rPr>` (before `<w:del>` and before all `EG_RPrBase` children).
       - `<w:del>` must be inserted immediately after `<w:ins>` (if present) and before all `EG_RPrBase` children (`rStyle`, `rFonts`, `b`, `i`, `color`, `sz`, etc.).
  4. **Index-0 `<w:pPr>` placement and empty `<w:rPr/>`/`<w:pPr/>` cleanup (`comments.py:867`, `comments.ts`, `engine.py`, `engine.ts`):**
     - Replace `p.append(pPr)` with `p.insert(0, pPr)` in `python/src/adeu/redline/comments.py:867` (and `p.insertBefore(pPr, p.firstChild)` in `node/packages/core/src/comments.ts`).
     - When removing child elements from `<w:rPr>` or `<w:pPr>` (e.g., `suppress_inherited` stripping `b`/`i` or `accept`/`reject` removing `ins`/`del` marks), if `<w:rPr>` has 0 element children and 0 attributes (and is not the required child of `<w:rPrChange>`), remove `<w:rPr>` from its parent; if `<w:pPr>` subsequently has 0 element children and 0 attributes (and is not inside `<w:pPrChange>`), remove `<w:pPr>` from `<w:p>`.
- **Verify:**
  - `cd python && uv run pytest tests/test_ooxml_schema_compliance.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Saved `.docx` files have zero undeclared `mc:Ignorable` prefixes, zero out-of-order `CT_RPr` / `CT_ParaRPr` / `CT_P` elements, and zero empty `<w:rPr/>` or `<w:pPr/>` containers in both Python and Node.

---

### Task 6: Add Manual / Inline Formatting Markup Mode (`markup_mode="manual"`)

- **Status:** `[x] Completed`
- **Goal:** Support legal and financial markup conventions that prohibit Word Track Changes (`<w:ins>`/`<w:del>`) by adding a `markup_mode="manual"` option that renders deletions as strikethrough runs (`<w:strike/>`) and insertions as styled runs (default: bold, single underline, `#C00000` red) while still attaching native Word comments.
- **Files:**
  - `python/src/adeu/redline/engine.py`
  - `python/src/adeu/mcp_components/tools/document.py`
  - `python/src/adeu/cli.py`
  - `node/packages/core/src/engine.ts`
  - `node/packages/mcp-server/src/index.ts`
  - `langchain/langchain_adeu/apply_changes.py`
  - `python/tests/test_manual_markup_mode.py`
  - `node/packages/core/src/manual_markup_mode.test.ts`
- **Change:**
  1. In `RedlineEngine.__init__` (`python/src/adeu/redline/engine.py` and `node/packages/core/src/engine.ts`):
     - Accept `markup_mode: Literal["track_changes", "manual"] = "track_changes"`, `manual_del_props: Optional[Dict[str, Any]] = None` (default `{"strike": True}`), and `manual_ins_props: Optional[Dict[str, Any]] = None` (default `{"bold": True, "underline": True, "color": "C00000"}`).
  2. When `self.markup_mode == "manual"`:
     - **Deletions (`track_delete_run` / Phase 1 deletion):** Instead of wrapping deleted runs in `<w:del>` and converting `<w:t>` to `<w:delText>`, keep the runs as regular `<w:r>` elements with `<w:t>` and apply `manual_del_props` (`<w:strike/>` in canonical `CT_RPr` order, preserving the original font color and cleared of `<w:u/>` so deleted text is struck through without underline).
     - **Insertions (`track_insert` / `_track_insert_multiline`):** Instead of wrapping inserted runs in `<w:ins>` or stamping `<w:ins/>` onto `<w:pPr><w:rPr>`, insert regular `<w:r>` elements (or regular `<w:p>` elements on `\n\n`) and apply `manual_ins_props` (`<w:b/>`, `<w:u w:val="single"/>`, `<w:color w:val="C00000"/>` in canonical `CT_RPr` order) on top of inherited font family/size.
     - **Spacing between adjacent manual deletion and insertion:** When a modification deletes non-whitespace text and inserts non-whitespace text at the same boundary without a leading/trailing space between them, preserve readability and round-trip clarity while attaching any `edit.comment` via `CommentsManager` spanning from the first struck-through run to the last inserted run.
  3. Also support inline `~~strikethrough~~` and `++underline++` in `_parse_inline_markdown` when `markup_mode == "manual"`.
  4. Expose `markup_mode: Optional[Literal["track_changes", "manual"]] = "track_changes"` on `process_document_batch` (Python MCP, Node MCP, LangChain) and `--manual` on `adeu apply` (CLI).
- **Verify:**
  - `cd python && uv run pytest tests/test_manual_markup_mode.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - `process_document_batch(..., markup_mode="manual")` and `adeu apply --manual` apply edits as regular `<w:r>` runs with `<w:strike/>` on deleted text and bold+underlined `#C00000` on inserted text, attach native Word comments (`w:commentRangeStart`/`w:commentRangeEnd`), and emit zero `<w:ins>` or `<w:del>` elements.

---

### Task 7: Support Markdown Tables in `new_text`, `insert_table` Change Action, and `create_docx`

- **Status:** `[x] Completed`
- **Goal:** Enable agents to insert new tables into existing documents (e.g., an Executive Summary table at the top of a contract) and create standalone `.docx` documents (e.g., cover notes and memos) from Markdown without dropping down to raw `python-docx` / XML scripts.
- **Files:**
  - `python/src/adeu/models.py`
  - `python/src/adeu/redline/engine.py`
  - `python/src/adeu/mcp_components/tools/document.py`
  - `python/src/adeu/cli.py`
  - `node/packages/core/src/models.ts`
  - `node/packages/core/src/engine.ts`
  - `node/packages/mcp-server/src/index.ts`
  - `python/tests/test_table_creation.py`
  - `node/packages/core/src/table_creation.test.ts`
- **Change:**
  1. **Markdown Pipe Table Block Detection in Multiline `new_text` (`engine.py`, `engine.ts`):**
     - When `new_text` in `ModifyText` contains a Markdown pipe table block (lines starting and ending with `|` with a `|---|---|` separator row), parse the header row and data rows and construct a schema-valid `<w:tbl>` sibling element at the paragraph split point:
       - `<w:tblPr>` with `<w:tblW w:w="5000" w:type="pct"/>` and `<w:tblBorders>` (`single`, `w:sz="4"`, `w:space="0"`, `w:color="BFBFBF"`).
       - `<w:tblGrid>` with `<w:gridCol/>` per column.
       - Header `<w:tr>` with `<w:trPr><w:tblHeader/></w:trPr>` (and `<w:ins/>` in `<w:trPr>` when `markup_mode == "track_changes"`), `<w:tcPr>` with `<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>`, and bold cell runs.
       - Data `<w:tr>` rows with `<w:ins/>` in `<w:trPr>` (when `markup_mode == "track_changes"`) and inline Markdown (`**bold**`, `_italic_`) parsed in every cell.
  2. **Explicit `InsertTable` (`type: "insert_table"`) Change Variant (`models.py`, `models.ts`, `engine.py`, `engine.ts`):**
     - Support `{"type": "insert_table", "target_text": "...", "position": "before" | "after", "headers": [...], "rows": [[...]], "comment": "..."}` in `process_document_batch` / `adeu apply` so callers can also pass structured JSON arrays to insert a new table before or after any anchor paragraph.
  3. **New Document Creation when `original_docx_path` == `output_path` (or `create_if_missing=True` / `adeu create`):**
     - If `process_document_batch` is called with `original_docx_path` equal to `output_path` where the file does not yet exist (or via a single `ModifyText` with `target_text=""` on a newly initialized blank document when `create_if_missing=True`, plus `adeu create input.md -o output.docx` on the CLI), initialize a clean, schema-valid blank `.docx` (with standard styles and namespaces) and populate its body from the provided Markdown blocks (headings `#`–`######`, paragraphs, bullet/numbered lists, and Markdown pipe tables) without tracked-change wrappers.
- **Verify:**
  - `cd python && uv run pytest tests/test_table_creation.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Inserting a Markdown pipe table via `ModifyText` or `insert_table` creates a schema-valid `<w:tbl>` in both Python and Node, and creating a new `.docx` with headings, paragraphs, and tables passes OpenXML validation with zero errors.

---

### Task 8: Normalize Unchanged Fuzzy Whitespace Boundaries in `trim_common_context`

- **Status:** `[x] Completed`
- **Goal:** Prevent accidental paragraph splits when `edit.target_text` and `edit.new_text` contain identical `\n` or `\n\n` breaks in unchanged prefix/suffix context that matched inline spaces `" "` in the document via fuzzy whitespace matching.
- **Files:**
  - `python/src/adeu/redline/engine.py`
  - `node/packages/core/src/engine.ts`
  - `python/tests/test_fuzzy_whitespace_context.py`
  - `node/packages/core/src/fuzzy_whitespace_context.test.ts`
- **Change:**
  1. Before calling `trim_common_context(actual_target_text, effective_new_text)` in `python/src/adeu/redline/engine.py` and `node/packages/core/src/engine.ts`:
     - Compare `edit.target_text` and `edit.new_text` directly to find their literal common prefix length `raw_prefix_len` and literal common suffix length `raw_suffix_len`.
     - If `edit.target_text[:raw_prefix_len]` (or the common suffix) contains `\n` where `actual_target_text` at the corresponding matched span contains a single horizontal space `" "` (because fuzzy regex `\s+` matched `" "` against `\n` or `\n\n`), replace those unchanged prefix/suffix `\n+` runs in `effective_new_text` with the actual document whitespace from `actual_target_text`.
     - This ensures `trim_common_context(actual_target_text, effective_new_text)` trims the entire unchanged prefix and suffix cleanly without turning an unchanged fuzzy-matched space into a paragraph split.
- **Verify:**
  - `cd python && uv run pytest tests/test_fuzzy_whitespace_context.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - Applying `ModifyText(target_text="Header LLC\n\n610 Ave", new_text="Header LLC\n\n600 Ave")` against a single-line paragraph `"Header LLC 610 Ave"` replaces `"610"` with `"600"` inline without splitting the paragraph into two `<w:p>` elements.

---

### Task 9: `read_docx` Ergonomics — Node Inline Regex Flags, Search Page Ranges, Range Continuation Footers, and Outline Bold Section Detection

- **Status:** `[x] Completed`
- **Goal:** Eliminate `read_docx` tool errors and silent truncation traps across Node and Python by supporting PCRE inline regex flags (`(?i)`, `(?m)`, `(?s)`) in Node, allowing page ranges (`page="4-5"`) in search mode, emitting a continuation footer on `build_page_range_response` when `last < total_pages`, and detecting numbered/structural bold section headers in `extract_outline`.
- **Files:**
  - `node/packages/mcp-server/src/response-builders.ts`
  - `node/packages/core/src/mapper.ts`
  - `node/packages/core/src/engine.ts`
  - `python/src/adeu/mcp_components/tools/document.py`
  - `node/packages/mcp-server/src/index.ts`
  - `python/src/adeu/mcp_components/_response_builders.py`
  - `python/src/adeu/outline.py`
  - `node/packages/core/src/outline.ts`
  - `python/tests/test_read_ergonomics.py`
  - `node/packages/mcp-server/src/read_ergonomics.test.ts`
- **Change:**
  1. **Support leading inline regex flags `(?i)`, `(?m)`, `(?s)` in Node (`response-builders.ts`, `mapper.ts`, `engine.ts`):**
     - Add a shared helper `compileRegexWithInlineFlags(pattern: string, baseFlags: string): RegExp` that extracts leading `^\(\?([ims]+)\)` (and mid-pattern `\(\?([ims]+):` if present) and merges `i`, `m`, `s` into `baseFlags` before calling `new RegExp(...)`.
     - Use it in `build_search_response` (`node/packages/mcp-server/src/response-builders.ts`), `DocumentMapper.find_all_match_indices` / `find_match_index` (`node/packages/core/src/mapper.ts`), and `RedlineEngine` regex validation (`node/packages/core/src/engine.ts`).
  2. **Accept page ranges (`page="4-5"`) in search mode and strip surrounding quotes on `page` (`document.py`, `index.ts`, `_response_builders.py`, `response-builders.ts`):**
     - In `_parse_page_param` (Python `document.py:92` and Node `index.ts:165`), strip surrounding single or double quotes (`s = s.strip().strip("'\"")`) so `"'all'"` or `'"1-5"'` parses cleanly.
     - In `read_docx` search mode and `build_search_response`, accept a `(start, end)` page-range tuple in addition to `None` and `int`, filtering matches to `start <= hit.page <= end`.
  3. **Emit continuation footer on `build_page_range_response` when `last < total_pages` (`_response_builders.py:457-469`, `response-builders.ts:710-725`):**
     - When `last == end` and `last < total_pages`, append:
       - MCP: `> **Continues on page {last + 1} of {total_pages}.** Pass \`page="{last + 1}-{min(last + PAGE_RANGE_MAX_PAGES, total_pages)}"\` to read the next pages.`
       - CLI: `> **Continues on page {last + 1} of {total_pages}.** Pass \`--page {last + 1}-{min(last + PAGE_RANGE_MAX_PAGES, total_pages)}\` to read the next pages.`
  4. **Detect standalone structural bold section headings in `extract_outline` (`outline.py`, `outline.ts`):**
     - In `extract_outline` (without changing `get_paragraph_prefix` or the Virtual Text projection), recognize standalone bold paragraphs (`< 120` chars, not ending in a sentence period `.`) whose text matches structural legal/contract section patterns:
       - Level 1 (`#`): `^(?:ARTICLE|PART|SCHEDULE|EXHIBIT|ANNEX|APPENDIX)\s+[0-9IVXLCDMab]+\b` (case-insensitive)
       - Level 2 (`##`): `^(?:SECTION|PARAGRAPH|CLAUSE|ITEM)\s+\d+(?:\.\d+)*(?:\([a-z0-9]+\))*` or `^\d+\.\s+[A-Z]`
       - Level 3 (`###`): `^\d+\.\d+(?:\.\d+)*\.?\s+[A-Z]` or `^(?:Section|Paragraph|Clause)\s+\d+\([a-z0-9]+\)`
     - This populates `mode="outline"` for contracts that format section headers as Title-Case bold `Normal` paragraphs while leaving existing golden projection fixtures untouched.
- **Verify:**
  - `cd python && uv run pytest tests/test_read_ergonomics.py tests/test_projection_goldens.py`
  - `cd node && npm run build && npm test`
- **Done when:**
  - `(?i)` regexes work identically in Node and Python; `read_docx(search_query=..., page="4-5")` filters search hits to pages 4–5; `read_docx(page="1-8")` on a 14-page file prints the `"Continues on page 9 of 14"` footer; and `read_docx(mode="outline")` extracts numbered bold section headings (`Paragraph 1 — ...`, `1. ...`, `Section 2.01 ...`).

---

### Task 10: Expand Derivative Filename In-Place Detection and Enrich `process_document_batch` Receipts

- **Status:** `[x] Completed`
- **Goal:** Prevent unwanted `*_processed.docx` file proliferation when editing `.redline.docx` / `-redline.docx` files in place, and include minted `Chg:N`/`Com:N` IDs, action resolution previews, and remaining revision/comment counts in `process_document_batch` responses.
- **Files:**
  - `python/src/adeu/mcp_components/tools/document.py`
  - `python/src/adeu/cli.py`
  - `python/src/adeu/serve.py`
  - `python/src/adeu/text_revision.py`
  - `python/src/adeu/redline/engine.py`
  - `node/packages/core/src/engine.ts`
  - `node/packages/mcp-server/src/index.ts`
  - `langchain/langchain_adeu/apply_changes.py`
  - `python/tests/test_batch_receipts_and_paths.py`
  - `node/packages/mcp-server/src/batch_receipts_and_paths.test.ts`
- **Change:**
  1. **Derivative redline stem detection (`is_derivative_redline_stem`):**
     - Replace `p.stem.endswith("_processed") or p.stem.endswith("_redlined")` across `document.py:721`, `cli.py:1464`, `serve.py:355`, `text_revision.py:251`, `node/packages/mcp-server/src/index.ts:1166`, and `langchain/langchain_adeu/apply_changes.py:260` with a shared regex check `re.search(r"(?:_|-|\.)(processed|redlined|redline|markup|marked)$", p.stem, re.IGNORECASE)` (`/(?:_|-|\.)(processed|redlined|redline|markup|marked)$/i.test(stem)` in Node).
     - When `output_path` is omitted on a file whose stem already matches (such as `master-supply-agreement.redline.docx` or `contract-redline.docx`), update that file in place rather than creating `master-supply-agreement.redline_processed.docx`.
  2. **Enriched batch receipts (`engine.py`, `engine.ts`, `document.py`, `index.ts`):**
     - Record the minted `change_ids` (e.g., `["Chg:1", "Chg:2"]`) and `comment_ids` (e.g., `["Com:0"]`) on each per-edit entry in `stats["edits"]` during `process_document_batch`, and render `**IDs:** \`Chg:1\`, \`Chg:2\`` in `formatBatchResult` when present.
     - Record per-action receipts in `stats["actions"]` for applied `accept`, `reject`, and `reply` actions (including the resolved `Chg:N` / `Com:N` IDs and a single-line clean preview of the affected paragraph), and render them in `formatBatchResult`.
     - Include a summary line `Document State: X tracked change(s), Y comment(s) remaining.` in `formatBatchResult` so agents have immediate confirmation of the saved document state without needing a follow-up `read_docx` call.
- **Verify:**
  - `cd python && uv run pytest tests/test_batch_receipts_and_paths.py`
  - `cd node && npm run build && npm test`
  - `cd langchain && uv run pytest`
- **Done when:**
  - Calling `process_document_batch` with `output_path` omitted on `foo.redline.docx` writes to `foo.redline.docx` in place, and the returned receipt includes minted `Chg:N`/`Com:N` IDs, action resolution previews, and remaining tracked change/comment counts.

---

### Task 11: Streamline `skills/adeu-redlining/` for Low-Turn, Zero-Error Agent Execution

- **Status:** `[x] Completed`
- **Goal:** Reduce agent turn count and eliminate avoidable tool errors by making `skills/adeu-redlining/SKILL.md` self-contained for standard MCP operations, steering agents toward targeted `search_query` / `mode="outline"` / `mode="changes"` reads instead of blind `page="all"` calls, and documenting `diff_docx_files(..., output_path=...)`, `markup_mode="manual"`, and batch receipts.
- **Files:**
  - `skills/adeu-redlining/SKILL.md`
  - `skills/adeu-redlining/references/mcp-tools.md`
  - `skills/adeu-redlining/references/cli-fallback.md`
- **Change:**
  1. In `skills/adeu-redlining/SKILL.md`:
     - Inline the concise parameter signatures for `read_docx`, `process_document_batch`, `diff_docx_files`, `accept_all_changes`, and `finalize_document` so agents do not need to spend an extra turn calling `Read` on `references/mcp-tools.md` before standard batches.
     - Replace the instruction to use `page='all'` with clear guidance:
       - Use `search_query` (with `search_regex=true`, `full_paragraph=true`) when locating specific clauses, terms, dates, amounts, or parties across the document.
       - Use `mode="changes"` or `mode="comments"` when reviewing or resolving existing redlines/comments.
       - Use `mode="outline"` or `page=1` to inspect document structure and total page count; never pass `page="all"` on an unmeasured document over 3 pages.
     - Clarify that `search_query` snippets highlight regex matches with `**...**`, and that `ModifyText` automatically inherits the replaced run's bold/italic formatting (so callers should not add `**...**` unless adding new bold formatting).
     - Document `diff_docx_files(original_path, modified_path, output_path=..., author_name=...)` (and `adeu diff orig.docx rev.docx -o redline.docx --author ...`) for one-step document comparison to a tracked-changes `.docx`.
     - Document `markup_mode="manual"` (and `adeu apply --manual`) when instructions call for manual strikethrough/underline markup instead of Word Track Changes.
     - Document that `process_document_batch` returns a complete verification receipt (including CriticMarkup previews, minted `Chg:N`/`Com:N` IDs, action resolution previews, and remaining change/comment counts), so agents do not need a redundant post-batch `read_docx` call unless inspecting an unedited section.
  2. Sync `skills/adeu-redlining/references/mcp-tools.md` and `skills/adeu-redlining/references/cli-fallback.md` with the new parameters (`output_path` / `author_name` on `diff_docx_files`, `markup_mode="manual"`, `insert_table` / Markdown tables in `new_text`).
- **Verify:**
  - Inspect `git diff skills/adeu-redlining/` and run `cd python && uv run pytest` and `cd node && npm test` to ensure no skill metadata tests fail.
- **Done when:**
  - `SKILL.md`, `references/mcp-tools.md`, and `references/cli-fallback.md` accurately reflect all updated tool capabilities and low-turn workflow guidance.

---

## Risks & Mitigations

- **Risk: Changing `get_paragraph_prefix` or default projection output could break golden projection tests (`test_projection_goldens.py`, `shared/conformance/`).**
  - *Mitigation:* Task 9d scopes structural bold section-heading detection strictly to `extract_outline` (`outline.py` / `outline.ts`) for numbered legal section patterns (`Paragraph 1`, `Section 2.01`, `1. ...`) that do not appear in the synthetic golden fixtures, leaving `get_paragraph_prefix` and all golden projections byte-identical.
- **Risk: Changing `reuse_id` on multi-paragraph insertions could break `test_repro_chg_id_coalescing.py`.**
  - *Mitigation:* Task 1 and Task 5 preserve `reuse_id` coalescing semantics for `Chg:N` grouping so `test_repro_chg_id_coalescing.py` continues to pass without modification.
- **Risk: Changing the >76,000-character refusal on `read_docx(page="all")` could break `test_response_budget_guard.py` and `budget-guard.test.ts`.**
  - *Mitigation:* The `page="all"` budget guard behavior is kept intact; Task 9 and Task 11 eliminate the error in practice by fixing page-range continuation footers, supporting page ranges in search mode, and steering agents away from blind `page="all"` calls in `SKILL.md`.
- **Risk: Python / Node drift across 10 engine and MCP tasks.**
  - *Mitigation:* Every task specifies the exact Python and Node files side by side and requires paired unit tests in both `python/tests/` and `node/packages/*/src/` before the task is marked complete.

PLAN COMPLETE
