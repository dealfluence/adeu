# Project Context: Adeu

## System Overview
Adeu acts as a "Virtual DOM" for DOCX files, enabling LLMs to edit documents via a text proxy while preserving complex XML structure.
- **Ingestion**: `ingest.py` creates a Markdown/CriticMarkup representation of the document.
- **Mapping**: `mapper.py` builds a linear index of text spans linking back to `python-docx` objects.
- **Reconciliation**: `engine.py` calculates and applies atomic XML patches (`w:ins`/`w:del`).
- **Agent Interface**: `server.py` exposes these capabilities as an MCP (Model Context Protocol) server, while `cli.py` handles automated environment configuration.

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

### 4. Agentic Distribution Strategy
*   **Zero-Install**: We prioritize `uvx` (ephemeral execution) over global installation for end-users. The MCP server runs via `uvx adeu adeu-server`.
*   **Auto-Configuration**: The `adeu init` command manages the injection of tools into `claude_desktop_config.json`.
    *   *Safety*: It must always create a timestamped backup (`.bak`) before modifying the user's config.
    *   *OS Agnostic*: It handles path resolution for Windows (`%APPDATA%`) and macOS (`~/Library`) automatically.

## Developer Workflows

### Testing
*   **Regression Pattern**: Create `tests/test_repro_[issue].py` to isolate bugs before fixing.
*   **Golden Files**: `tests/fixtures/golden.docx` is the source of truth for Modern Comments (Word 2021+) XML structure.

### Deployment
*   **Versioning**: Semantic versioning in `pyproject.toml`. `src/adeu/__init__.py` dynamically loads this via `importlib.metadata`.
*   **Dependencies**: Uses `uv` (PEP 621 standard) with `hatchling` as the build backend. `python-docx` is patched at runtime in `comments.py` to support Modern Comments namespaces (`w16cid`, `w15`).

### Agent Integration Testing
*   To test changes to the MCP server without publishing to PyPI, use `uv run adeu init --local`.
*   This configures Claude Desktop to execute the server from the current local source (`sys.executable` + `cwd`), bypassing `uvx`.

## Current Status
- **v0.6.5**: Infrastructure Migration.
    - **Build System**: Migrated from Poetry to `uv` + `hatchling` for faster, standard-compliant dependency management.
    - **One-Shot Setup**: `adeu init` auto-configures Claude Desktop.
    - **Ephemeral Execution**: Full support for `uvx` based workflows.