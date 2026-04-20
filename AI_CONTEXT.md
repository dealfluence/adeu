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

### 5. Block-Level Parsing & Tables
*   **Sequential Iteration**: We iterate over document elements (`w:p` and `w:tbl`) in strict XML order using `iter_block_items`. We do *not* iterate `part.paragraphs` and `part.tables` separately, as this destroys document flow (e.g., tables appearing after all text).
*   **Recursion**: Ingestion and Mapping are recursive. `Document` -> `Table` -> `Cell` -> `Block Items` -> ...
*   **Synchronization Invariants**:
    *   **Empty Rows**: `ingest.py` must *never* skip empty table rows. `mapper.py` iterates all rows in the DOM; skipping one in text extraction causes index misalignment.
    *   **Separators**: Row separators (`\n`) are injected *between* rows. Virtual pipes (` | `) separate cells.

### 6. The Unified `DocumentChange` API
*   **Flat API Structure**: The LLM interacts with a flat list of `DocumentChange` objects (Discriminated Union of `ModifyText`, `AcceptChange`, `RejectChange`, `ReplyComment`).
*   **Search & Replace First**: Pure insertions and deletions are intentionally hidden from the LLM. All text modifications must be executed as search-and-replace (`ModifyText`) to guarantee sufficient anchoring context for the fuzzy matcher.
*   **Universal Tooling**: Disk-based and Live Word tools share the same endpoints (`read_docx`, `process_document_batch`). On Windows, omitting file paths dynamically routes the command to the active Live Word COM object, preventing LLM tool selection paralysis.

### 7. MCP Apps & UI Rendering
*   **Custom HTML Apps**: We use FastMCP's `AppConfig(resource_uri="ui://...")` to serve custom HTML/CSS interfaces for complex tools (e.g., `validate_documents`). We maintain full control over the markup.
*   **Vanilla JS**: We avoid external untested JS libraries to bypass CSP restrictions and ensure offline reliability. The iframe client uses a minimal `window.postMessage` JSON-RPC implementation to complete the Host handshake (`ui/initialize` -> `ui/notifications/initialized`) and receive payloads (`ui/notifications/tool-result`).
*   **Dynamic Resizing**: HTML resources must include a `ResizeObserver` that emits `ui/notifications/size-changed` messages to the Host, allowing the iframe to expand seamlessly as content is injected.
*   **Dual Payloads**: Tools utilizing UIs return `ToolResult(content=..., structured_content={"html": ...})`. This ensures the LLM receives pure Markdown to reason about, while the human user sees the styled HTML.

### 8. Document Sanitization & Part Ejection
*   **Deep Part Ejection**: When completely removing XML parts (e.g., Custom XML, Comments), deleting the elements is insufficient because `python-docx` will repackage empty XML files. We must explicitly sever relationships from `pkg.rels` and `part.rels`, and physically remove the part from `pkg._parts`.
*   **Mathematical Scrub Verification**: For metadata sanitization, we rely on `lxml` + XPath directly on the unzipped DOCX as the absolute source of truth. This strictly bypasses `python-docx` caching layers to mathematically guarantee artifacts are removed.
*   **Modern Comments Architecture**: Word's modern comments span four XML parts (`comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`). The resolved status (`w15:done="1"`) is stored inside `commentsExtended.xml` and must be parsed and scrubbed from there.

### 9. Live MS Word Interop (Windows COM)
*   **Platform Safety**: All live Word tools (`live_word.py`) depend on `pywin32` and are conditionally registered via `sys.platform == 'win32'`.
*   **COM Apartment Lifecycle**: Microsoft Office COM objects are strictly Single-Threaded Apartment (STA). Because FastMCP and `pytest` hold proxy frames unpredictably, we **intentionally omit** `pythoncom.CoUninitialize()` and `app.Quit()` during test teardown. We let the OS/Python GC handle teardown naturally to prevent fatal RPC/Access Violations (`0x800706be`).
*   **Index Drift Mitigation**:
    *   **Extraction Parity**: Active COM extraction uses an *event-based string builder* (sorting events by length and type) to inject CriticMarkup tags safely. This handles infinitely nested/overlapping annotations (e.g., comments wrapping redlines) without string offset drift.
    *   **Pre-Resolution**: Modifying text natively adds Revisions, shifting `doc.Revisions` indices. We pre-resolve and cache all target COM objects *before* applying a batch of `DocumentChange` operations so Accept/Reject actions target the correct revisions.
*   **Comment Bounds**: We strictly use `Comment.Scope` (the highlighted text), not `Comment.Reference` (the 0-length anchor), to accurately extract target strings for Comment annotations.
*   **Identity Spoofing & Deadlocks**: Tools temporarily hijack `Word.Application.UserName` and toggle `doc.TrackRevisions` to apply tracked changes cleanly as the Agent. *Constraint*: Modern M365 enforces logged-in MS Account identities on Comments. Attempting to spoof comment authors via `app.Options.UseLocalUserInfo` causes fatal STA thread deadlocks. Live comments will natively show the local user's real name.

### 10. COM vs XML Impedance Mismatches
Achieving 100% CriticMarkup extraction parity between Live COM and Disk XML requires bridging deep structural differences:
*   **State Machine Parity**: Both engines MUST feed into the exact same event-driven state machine (`DocxEvent` accumulation -> `_get_wrappers` -> `_build_merged_meta_block`) to ensure identical tag ordering and bubble grouping.
*   **Formatting (Explicit vs Inherited)**: Disk XML evaluates explicit `<w:b/>` tags. Word COM's `rng.Find.Font.Bold` evaluates WYSIWYG bold (including inherited styles like Headings). Live COM must explicitly cross-check `rng.Style.Font.Bold` to avoid double-styling markdown markers (`**`) on inherited runs.
*   **Table Rendering & COM Offset Drift**: Word COM injects hidden structural characters (`\r\x07`) at cell boundaries, breaking Python string indices. Solution: Decouple structural markdown extraction (`|` for cells) from native COM execution, using exact index mapping arrays paired with `rng.Find` to securely bypass COM index drift.
*   **Ephemeral Session IDs**: Word natively assigns `w:id="0"` to all unsaved revisions/comments in live memory, randomly assigning persistent IDs during a Save. **IDs are session-bound.** Agents must treat Save/Reload boundaries as a state wipe and re-index the document IDs afterward.
*   **Destructive Native Edits (Comment Rescue)**: Assigning `Range.Text` in Live Word natively destroys any comments anchored to that text. Batch processors must explicitly cache, rescue, and re-anchor comments during string replacements.
*   **Empty Runs & Timestamps**: Both engines must explicitly skip empty runs to synchronize lookahead bubble grouping. Both must emit full ISO-8601 timestamps without truncation to preserve chronological signals.

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
- **v1.1.0**: Live Word Interop & Agentic Workflows.
    - **Live MS Word Engine**: Fully integrated Windows COM engine allowing agents to execute live edits on an active MS Word canvas (`sys.platform == "win32"`).
    - **Flat API**: Unified `DocumentChange` discriminated union deployed for the MCP interface.
    - **Testing**: End-to-end LLM verification complete and backwards compatibility preserved.
    - **UI Layer**: Zero-dependency, Vanilla JS custom HTML MCP Apps implementation for tools like `validate_documents`.