# Bug Report: Paragraph Merge Failure on Text Deletion

## Issue Description

When using the `process_document_batch` engine to perform a text modification (`ModifyText`), the engine fails to merge paragraphs if the `target_text` spans across a paragraph boundary (`\n\n`) but the `new_text` does not.

For example, given the document text:
```
Paragraph 1 end.

Paragraph 2 start.
```

An edit targeting `"1 end.\n\nParagraph 2"` and replacing it with `"1 end. Paragraph 2"` is intended to merge the two sentences into a single paragraph. However, the engine currently applies the text changes within their respective existing paragraph blocks but leaves the structural paragraph break intact.

## Architectural Root Cause

The Adeu engine operates as a non-destructive OOXML *Track Changes* engine. It resolves text strings against the virtual Markdown projection back down to specific `<w:r>` (Run) elements.

1. `target_text: "1 end.\n\nParagraph 2"`
2. The `DocumentMapper` resolves this to two distinct runs:
   - Run A: `"1 end."` (resides inside `<w:p>` Paragraph 1)
   - Run B: `"Paragraph 2"` (resides inside `<w:p>` Paragraph 2)
3. The engine replaces the text within these runs with `<w:del>` and `<w:ins>` tags.

**The Limitation:** The engine never touches the actual `<w:p>` nodes. To correctly "merge" two paragraphs in OOXML while preserving Track Changes, the engine would need to structurally manipulate the DOM by:
- Creating a tracking tag for the deletion of the paragraph mark (`<w:pPr>`).
- Coalescing the remaining children of Paragraph 2 into Paragraph 1.
- Handling the resolution of conflicting paragraph styles (`<w:pStyle>`).

Because the current engine strictly loops over modified *runs* (`_apply_single_edit_indexed`), structural paragraph merges are silently ignored.

## Relevant Files

- **`src/adeu/redline/engine.py`**: The `_apply_single_edit_indexed` method (specifically under `elif op == EditOperationType.MODIFICATION:`) applies text replacements at the run level but lacks logic to detect and structurally merge parent `<w:p>` tags when `\n\n` is deleted.
- **`src/adeu/redline/mapper.py`**: The `DocumentMapper` correctly maps `\n\n` as virtual text, but `find_target_runs_by_index` only returns the text `<w:r>` elements, entirely skipping the paragraph boundary metadata.
- **`tests/test_repro_paragraph_merge_failure.py`**: The problem-replicating test case that asserts two paragraphs should merge into one. Currently failing.

## Next Steps

To properly support paragraph merging, the `RedlineEngine` requires a specialized structural operation (similar to how tables or footnotes are handled) rather than relying on generic text run replacement. The engine must explicitly detect when virtual `\n\n` tokens are included in the deletion span and execute an OOXML paragraph merge protocol.
