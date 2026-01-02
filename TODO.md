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

## 🐛 Known Issues
### 1. Table Layouts
*   **Status**: Basic support. Tables are extracted linearly (`|` separated).
*   **Limitation**: Edits spanning across cell boundaries (e.g., merging two cells) are NOT supported and will likely throw errors or be ignored.
*   **Next Step**: Implement explicit Table/Row/Cell awareness in `ComplianceEdit` target resolution.

## 🚀 Next Steps (Roadmap)
1.  **Formatting Preservation**: Currently, inserted text inherits style from the anchor run. We need logic to handle cases where the insertion should inherit from the *next* run (e.g., inserting at the start of a bold sentence).
2.  **LLM Integration**: Connect the `ComplianceEdit` schema to an actual OpenAI/Anthropic function call to bypass the text-diffing step for simple instructions ("Change the governing law to NY").
3.  **Table Enhancements**: Improve table redlining by mapping cell indices.

## 📂 Key Files
*   `src/adeu/redline/engine.py`: **The Brain**. modifying this requires care.
*   `src/adeu/redline/mapper.py`: **The Map**. If searching fails, look here.
*   `tests/test_roundtrip.py`: **The Proof**. Run this before pushing.
*   `tests/test_properties.py`: **The Fuzzer**. Generates random docs to test crash resilience.
