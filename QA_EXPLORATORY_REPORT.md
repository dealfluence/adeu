# QA Exploratory Testing Report
*Date: 2026-04-30*
*Focus: Live Word Parity, Diffing, and Sanitization*

## Discovered Issues

### 1. Disk Engine Drops Comments on Multi-Paragraph Insertions
* **Description:** When applying a `ModifyText` operation via the Disk Engine (`RedlineEngine`) where the `target_text` is replaced by `new_text` containing a paragraph break (`\n\n`), the engine correctly processes the text change but silently drops the associated `comment` argument. The comment is never written to `word/comments.xml`.
* **Parity Gap:** The Live Word COM engine handles this correctly (it successfully anchors the comment to the first paragraph of the insertion, as required by COM limitations), but the on-disk engine loses it completely.
* **Reproduction:** Added `tests/test_repro_disk_comment_drop_multipara.py` which demonstrates the `AssertionError: word/comments.xml was not created`.

### 2. Sanitize Report Heuristic Routing Bug
* **Description:** The `SanitizeReport` generator uses naive string matching to route output lines into their respective report sections. Output from `transforms.remove_all_comments` yields detail lines like `  [Open] "Updated term." (Author)`. Because this line does not contain the literal word `"comment"`, it falls through the heuristic and gets mis-routed into the `STRUCTURAL` section of the final audit report, causing confusion.
* **Reproduction:** Added `tests/test_repro_sanitize_report_routing.py` which asserts that the detail line incorrectly lands in `report.structural_lines` instead of `report.removed_comment_lines`.

### 3. Sanitization Violates Architectural Constraint on Empty Comment Parts
* **Description:** In `src/adeu/sanitize/transforms.py`, the `_eject_comment_parts` function physically deletes empty comment XML parts from `pkg._parts` and `pkg.rels`. This is a direct violation of Architectural Decision #8 ("Empty Comment Part Lifecycle") defined in `AI_CONTEXT.md`, which states: *"Empty comment XML parts are explicitly left intact rather than purged when all comments are removed, as dynamically mutating the pkg.rels matrix across different python-docx versions is volatile and can cause unrecoverable package corruption."*
* **Reproduction:** Added `tests/test_repro_sanitize_comment_ejection.py` which proves the parts are deleted (resulting in a missing `word/comments.xml`).

### 4. Diff Engine Fails to Detect Formatting-Only Changes (Silent Change Blindspot)
* **Description:** The `mcp_adeu_diff_docx_files` tool (and underlying `diff_docx`) completely misses formatting modifications if the underlying text string has not changed. For example, changing "Silent Change" to "**Silent Change**" (bolding) produces a diff report without any `@@` hunks when `compare_clean=True`, effectively blinding the AI to stylistic manipulations by the counterparty.
* **Reproduction:** Added `tests/test_repro_diff_formatting_silent.py::test_diff_engine_ignores_formatting_changes`.

### 5. Batch Engine Fails to Enforce Markdown Heading Limits
* **Description:** According to AI_CONTEXT.md, the engine must strictly clamp Markdown headings to a max depth of 6 and raise a `BatchValidationError`. Currently, passing `#` * 7 (or more) through `ModifyText` succeeds silently, corrupting the OOXML rendering state instead of properly rejecting the edit.
* **Reproduction:** Added `tests/test_repro_batch_engine_edge_cases.py::test_batch_engine_heading_depth_enforcement`.

### 6. Batch Engine Raises AttributeError instead of ValidationError on Fake Comments
* **Description:** If an agent attempts to `ReplyComment` and targets a non-existent ID (e.g., `Com:999`), the engine crashes with a bare Python `AttributeError` instead of gracefully trapping it and emitting a `BatchValidationError` or `NotFound` error back to the MCP protocol.
* **Reproduction:** Added `tests/test_repro_batch_engine_edge_cases.py::test_batch_engine_reply_to_fake_comment`.

### 7. Batch Engine Deletes Special Content (w:drawing) on Nearby Edits
* **Description:** Architectural Decision #2 defines "Special Content" (`w:br`, `w:tab`, `w:commentReference`, `w:drawing`) as immutable boundaries that must never be destroyed during run coalescing or text substitution. However, modifying text in the same paragraph as a `w:drawing` element currently destroys the drawing node, silently deleting images from the document without tracking the deletion.
* **Reproduction:** Added `tests/test_repro_batch_engine_deletes_image.py`.

### 8. Diff Engine Misaligns Context on Markdown Tokens
* **Description:** The `mcp_adeu_diff_docx_files` tool chunking logic breaks diffs mid-token for Markdown text. When diffing `veryBigDoc.docx`, replacing table text next to `**(In millions)**` caused the diff hunk to split the token, producing `- *(In millions)**` in the deletion block and leaving the leading `*` in the context block. This generates invalid Markdown patches.
* **Reproduction:** Added `tests/test_repro_diff_markdown_split.py`.

### 9. Batch Engine Swallows Deletions When Splitting Paragraphs
* **Description:** When using `ModifyText` to split a paragraph or list item (e.g., replacing `...(**"Party A"**); and` with `...(**"Party A"**);\n* **[Subsidiary]**...`), the engine successfully inserts the new paragraph block but silently ignores the trailing deletion (the removal of ` and` from the first paragraph). The old text is retained natively without `<w:del>` tags.
* **Reproduction:** Added `tests/test_repro_batch_engine_swallows_trailing_deletions.py`.

### 10. Extractor Silently Ignores Tracked Formatting Changes
* **Description:** The `mcp_adeu_read_docx` tool completely ignores `<w:rPrChange>` (tracked formatting) elements in the underlying XML. If a user turns on Track Changes and only modifies formatting (e.g., bolds a word), the extractor parses it as natively bold (`**Text**`) without generating `{++` or `{==` CriticMarkup. This completely erases redline metadata for styling changes.
* **Reproduction:** Added `tests/test_repro_extractor_ignores_tracked_formatting.py`.

