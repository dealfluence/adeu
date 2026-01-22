# FILE: TODO.md
# 📋 Roadmap

## 🚧 Upcoming Priorities (v0.6.0+)

### 1. Document Scope Expansion
*   **Headers & Footers**: Update `ingest.py` and `mapper.py` to iterate through `section.header` and `section.footer` parts. Currently, these are read-only/invisible.
*   **Hyperlinks**: Implement support for editing text inside `w:hyperlink` tags.
*   **Complex Fields**: Investigate safe strategies for handling Fields (Date, TOC) without breaking them.

### 2. Advanced Table Redlining
*   **Cell Awareness**: Improve `DocumentEdit` targeting to be explicit about table structures (e.g., `row_index`, `col_index`).
*   **Structural Edits**: Support adding/removing rows or merging cells via the API (currently only text content modification is supported).

### 3. Performance
*   **Lazy Loading**: Optimize `DocumentMapper` to map paragraphs on-demand rather than scanning the entire document on load (performance boost for 100+ page contracts).

## 🐛 Known Limitations
*   **Tables**: Edits spanning across cell boundaries (e.g., merging two cells via text deletion) are not supported and will be ignored.
*   **Field Codes**: Edits inside complex field codes are skipped to prevent corruption.
