@@ ... @@
 export { DocumentObject } from './docx/bridge.js';
 export { DocumentMapper, TextSpan } from './mapper.js';
 export { RedlineEngine, BatchValidationError } from './engine.js';
-export { generate_edits_from_text, trim_common_context } from './diff.js';
+export { generate_edits_from_text, trim_common_context, create_unified_diff } from './diff.js';
 export { apply_edits_to_markdown } from './markup.js';
 export { paginate, split_structural_appendix, PaginationResult, PageInfo } from './pagination.js';
 export { extract_outline, OutlineNode } from './outline.js';