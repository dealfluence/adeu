QA Findings Summary
I'll group by severity. All findings include the expectation I had before each call; full repros are in the chat above.
#SeverityAreaTitle1🔴 Majorread_docx outlineOutline detector misses long custom-derived heading styles (StyleHeading2NotItalicBefore0ptAfter0ptLinespa not recognized; the shorter StyleHeading2NotItalic is). example03's "Sub Headings:" H2 is missing from outline.6🔴 Major (perf)process_document_batch live modeSingle live edit took 50.034s vs 0.055s on disk (~900× slower). Other live calls finished in <1s — implies an anomalous wait/retry path on the success branch.10🔴 Majoraccept_all_changesDoesn't remove comments despite docs saying so. Confidentiality risk: lawyers using "Accept All" to finalize a clean doc may unknowingly leak comments. The proper finalize path is sanitize_docx accept_all=True.8🟠 Major→Minorprocess_document_batch heading-level changeChanging ## X → # Y doesn't change style in place; deletes old text + inserts new para. After accept_all_changes, an empty Normal paragraph remains (P#82 in OOXML). After sanitize, an empty ##  paragraph remains visible.9🟠 Majorprocess_document_batch rejectRejecting a multi-paragraph insertion via Chg ID removes the inserted text but leaves behind the paragraph break. Document doesn't fully revert despite docs saying multi-paragraph inserts are "one logical revision".11🟠 Majoraccept_all_changes vs sanitize_docxTwo finalize paths produce structurally-different output for the same input (orphan paragraph kept as Normal vs as Heading2).12🟡 Majorsanitize_docx baseline modeReports "84% differ" warning for two near-identical files (<5% real text difference). False positives undermine the safety net.2🟡 Minorread_docx outline"(has table)" annotation aggregates upward to ancestor headings; misleading for navigation.4🟡 Minorread_docx fullSome Heading 2s render as ## \n<text> (heading marker on one line, text on next). Breaks target_text="## Foo" matches in subsequent edits.14🟡 Minorlive WordLive-mode latency varies wildly (0.4s — 50s) — same tool, similar inputs.15🟡 Minorprocess_document_batchWhen auto-routed to live Word, user-supplied output_path is silently ignored.17🟡 Minorinsert_rowSilent truncation when cells array exceeds table column count; silent padding when too few. No warning either way.3🟢 Cosmeticread_docx tablesMarkdown tables emitted without separator row (`5🟢 Cosmeticread_docx raw viewTracked-change author/IDs surfaced via CriticMarkup comment syntax {>>...<<}, conflating comments with change metadata.7🟢 Cosmeticprocess_document_batchChg IDs are assigned in document-position order, not in the order they appear in the changes array. Surprising for callers.13🟢 Cosmeticprocess_document_batch"Batch complete. Applied: 0, Failed: 1" wording when nothing was applied — misleading vs the cleaner "Batch rejected" phrasing for ambiguous-match.16🟢 Cosmeticinsert_row / delete_rowCriticMarkup format for table-row edits differs from text edits: `{++ row
Things that worked well

validate_documents async polling pattern: clean.
sanitize_docx (all three modes): comprehensive audit reports, blocks unresolved-changes path correctly, scrubs metadata thoroughly.
Adversarial CriticMarkup in new_text: rejected with a clear, helpful error.
Ambiguous-match detection: shows BOTH match contexts, gives actionable guidance.
Atomic batch validation: failed sequential-dependency edit didn't partially apply.
accept_all_changes is fast (0.038s).
Live Word auto-routing detects the open file without requiring an explicit live flag.
read_docx pagination input validation (out-of-range page, page=0, page=-1) returns clear errors.
Multi-paragraph insert with \n\n produces a minimal-diff tracked change rather than re-tracking unchanged text.
Comment workflow (comment param on a no-op modify, reply action) chains nicely.

Areas not yet tested (next round if you want)

Pagination at real boundaries (need a synthetic ≥19K-char doc).
Tracked changes that span page boundaries.
debug_xml_diff exhaustively.
diff_docx_files with compare_clean=False.
Footnote / endnote editing.
save_active_word_document round-trip and save-as.
Adversarial: extremely long target_text, Unicode edge cases (RTL, ZWJ, smart quotes), overlapping Chg IDs across multiple authors.
Real contract-shaped doc with numbered clauses, definitions, cross-references.