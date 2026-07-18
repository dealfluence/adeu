export function identifyEngine() {
  return 'adeu-core-node';
}

export { DocumentObject } from './docx/bridge.js';
export { DocumentMapper, TextSpan } from './mapper.js';
export { RedlineEngine, BatchValidationError, validate_edit_strings, describe_illegal_control_chars } from './engine.js';
export { generate_edits_from_text, trim_common_context, create_unified_diff, create_word_patch_diff } from './diff.js';
export { apply_edits_to_markdown } from './markup.js';
export { paginate, split_structural_appendix, PaginationResult, PageInfo } from './pagination.js';
export { extract_outline, OutlineNode } from './outline.js';
export { extractTextFromBuffer, _extractTextFromDoc } from './ingest.js';
export { finalize_document, FinalizeOptions, FinalizeResult } from './sanitize/core.js';
export { RegexTimeoutError, userFindAllMatches, userSearch, USER_PATTERN_TIMEOUT_MS } from './utils/safe-regex.js';