# Project Context: Adeu

## System Overview
Adeu acts as a "Virtual DOM" for DOCX files, enabling LLMs to edit documents via a text proxy while preserving complex XML structure.
- **Ingestion**: `ingest.py` creates a Markdown/CriticMarkup representation of the document.
- **Mapping**: `mapper.py` builds a linear index of text spans linking back to `python-docx` objects.
- **Reconciliation**: `engine.py` calculates and applies atomic XML patches (`w:ins`/`w:del`).

## Architectural Decisions & Invariants

### 1. Ingestion & Formatting
*   **Newline Isolation**: Markdown formatting markers (`**`, `_`, etc.) **must never** enclose newline characters (`\n`).
    *   *Reasoning*: Wrapping newlines breaks many Markdown parsers and complicates line-based text segmentation.
    *   *Implementation*: `utils.docx.apply_formatting_to_segments` splits text by newlines *before* wrapping segments in markers.
    *   *Pattern*: `**Line 1**\n**Line 2**`, NOT `**Line 1\nLine 2**`.

### 2. XML Normalization (`normalize_docx`)
*   **Run Coalescing**: We merge adjacent runs with identical styling to reduce token count and simplify mapping ("Con" + "tract" -> "Contract").
*   **Safety Constraint**: Runs containing "Special Content" (`w:br`, `w:tab`, `w:commentReference`, `w:drawing`) are **immutable boundaries**.
    *   *Rule*: Never merge a run containing special tags into a text run, or the special tag will be destroyed.

### 3. The "Virtual Text" Contract
*   `ingest.py` and `mapper.py` must be strictly synchronized.
*   If `ingest.py` produces virtual characters (e.g., `{==` or `**`), `mapper.py` must explicitly account for them as `virtual` spans so the `RedlineEngine` knows they do not exist in the DOM.

## Developer Workflows

### Testing
*   **Regression Pattern**: Create `tests/test_repro_[issue].py` to isolate bugs before fixing.
*   **Golden Files**: `tests/fixtures/golden.docx` is the source of truth for Modern Comments (Word 2021+) XML structure.

### Deployment
*   **Versioning**: Semantic versioning in `pyproject.toml`.
*   **Dependencies**: Uses `poetry`. `python-docx` is patched at runtime in `comments.py` to support Modern Comments namespaces (`w16cid`, `w15`).

## Current Status
- **v0.5.3**: Full support for Threaded Comments, Negotiation Actions, and Safe Formatting.