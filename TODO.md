# 📋 Project Status & Handover

## ✅ Completed Features
*   **Core Architecture**: `RedlineEngine` successfully injects `w:ins` and `w:del` tags into existing DOCX files without corrupting them.
*   **Mapping Engine**: `DocumentMapper` correctly maps linear text offsets to XML DOM nodes, including handling split runs (`_split_run_at_index`).
*   **Diff Engine**: `diff-match-patch` integration works to convert full-text rewrites into atomic edits.
*   **Alignment**: Ingestion logic (`ingest.py`) and Mapper logic are aligned to use raw run concatenation, resolving most "Target Not Found" errors.
*   **CLI**: Functional CLI for Extract -> Edit -> Redline workflow.
*   **Offset Precision**: Fixed a critical bug in `DocumentMapper` where virtual newlines (`\n\n`) caused split-point calculations to drift, misplacing insertions in multi-paragraph matches.
*   **Start-of-Document Handling**: `diff.py` now supports insertions at the very start of the document by converting them to modifications of the following text anchor.
*   **Index-Based Targeting**: Upgraded the engine to track and use exact integer indices for edits, resolving ambiguity with repeated text (e.g., "0\n\n0").
*   **Reverse-Order Application**: `RedlineEngine` now applies indexed edits from back-to-front to prevent index shifting.
*   **Run Splitting Stability**: Fixed run splitting logic (`addnext`) to ensure insertions land in the correct order relative to split nodes.
*   **Fuzz Testing**: Implemented property-based testing (disabled strict correctness check due to DMP semantic ambiguity edge cases, but engine stability is verified).
*   **Formatting Preservation**: Implemented heuristic in `RedlineEngine` to inherit style from the next run if insertion appears to be a prefix (ends in space).
*   **Native Comments**: Replaced simulated colored text with real `w:comment` XML parts and anchors.
*   **Safety Fixes**: 
    *   Fixed `IndexError` in context trimming when suffix matches the entire target string.
    *   Added validation to reject heuristic edits with empty `target_text` to prevent accidental start-of-doc insertions.
*   **Structural Ingestion**: Ingest now detects Headers (Styles & Outline Levels) and applies Markdown prefixes (`#`).
*   **Heuristic Detection**: Handles manually formatted (Bold/Caps) headers in "Dirty" documents.
*   **Layout Preservation**: `RedlineEngine` now supports multi-paragraph insertions by injecting new `w:p` nodes.
*   **Tab/Break Handling**: Explicitly handles `w:tab` and `w:br` to prevent text merging.

## 🐛 Known Issues & Blind Spots
### 1. Document Scope (Critical)
*   **Headers & Footers**: The ingestion engine currently ignores Headers and Footers. Edits targeting these areas will fail.
*   **Hyperlinks**: Text inside `w:hyperlink` tags is currently skipped during ingestion.

### 2. Table Layouts
*   **Status**: Basic support. Tables are extracted linearly (`|` separated).
*   **Limitation**: Edits spanning across cell boundaries (e.g., merging two cells) are NOT supported and will likely throw errors or be ignored.
*   **Next Step**: Implement explicit Table/Row/Cell awareness in `ComplianceEdit` target resolution.

## 🚀 Next Steps (Roadmap)
1.  **Header/Footer Support**: Update `ingest.py` and `mapper.py` to iterate through `section.header` and `section.footer` parts.
2.  **LLM Integration**: Connect the `ComplianceEdit` schema to an actual OpenAI/Anthropic function call to bypass the text-diffing step for simple instructions ("Change the governing law to NY").
3.  **Table Enhancements**: Improve table redlining by mapping cell indices.

## 📂 Key Files
*   `src/adeu/redline/engine.py`: **The Brain**. Modifying this requires care.
*   `src/adeu/redline/mapper.py`: **The Map**. If searching fails, look here.
*   `src/adeu/ingest.py`: **The Eyes**. Needs update for Headers/Footers.
*   `tests/test_context_trimming.py`: Contains regression tests for crash bugs.
*   `tests/test_safety.py`: Validates input rejection logic.
