export function identifyEngine() {
  return 'adeu-core-node';
}

export { DocumentObject } from './docx/bridge.js';
export { DocumentMapper, TextSpan } from './mapper.js';
export { RedlineEngine, BatchValidationError, extract_failed_indices, validate_edit_strings, describe_illegal_control_chars } from './engine.js';
export { generate_edits_from_text, generate_structured_edits, generate_edits_via_paragraph_alignment, trim_common_context, create_unified_diff, create_word_patch_diff, collect_media_difference_warnings, DiffEdit } from './diff.js';
export { TextRevisionError, TextRevisionVerificationError, apply_text_revision_core, check_criticmarkup, check_major_deletions, strip_page_chrome, verify_clean_text } from './text-revision.js';
export { apply_edits_to_markdown, MarkupEditReport } from './markup.js';
export { paginate, split_structural_appendix, parse_page_arg, PAGE_RANGE_MAX_PAGES, PaginationResult, PageInfo, PageArgKind } from './pagination.js';
export { extract_outline, offset_to_page, OutlineNode } from './outline.js';
export { extract_comments_data } from './comments.js';
export { extractTextFromBuffer, _extractTextFromDoc, ExtractStructure, TableGeometry, RowGeometry } from './ingest.js';
export { finalize_document, FinalizeOptions, FinalizeResult } from './sanitize/core.js';
export { RegexTimeoutError, userFindAllMatches, userSearch, USER_PATTERN_TIMEOUT_MS } from './utils/safe-regex.js';
export { clamp_text, truncate_middle, REPORT_ECHO_CAP, PREVIEW_TEXT_CAP } from './utils/text.js';
export { failure_envelope, has_fused_json_marker, response_budget_limit, whole_doc_guard_message, shrink_batch_stats, BATCH_RECOVERY_PROTOCOL, BATCH_ERROR_CODES, FUSED_JSON_HINT, MINIMAL_EDIT_TOKEN_BUDGET, FAILED_TARGET_STUB_CAP, GUARD_EMITTED_MAX_CHARS, FailureEnvelope } from './payloads.js';
