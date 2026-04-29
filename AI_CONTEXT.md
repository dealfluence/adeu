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
*   **Multi-Level Lists**: OOXML `<w:ilvl>` maps natively to standard Markdown indentation (4 spaces per level) on read. On write, the engine parses leading spaces divided by 4 to explicitly inject `<w:ilvl>` into `<w:numPr>`.

### 2. XML Normalization & Surgical Mode
*   **Surgical Mode**: The `RedlineEngine` operates in "Surgical Mode" — it never performs global document normalization (`normalize_docx`) on initialization or save. It strictly preserves untouched paragraphs, preventing the silent destruction of unrelated metadata (like `<w:proofErr>`) and preserving exact XML whitespace lines to guarantee minimal, readable diffs.
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
    *   **Cell Isolation**: The virtual `|` boundary represents a hard `<w:tc>` cell wall. Modifying text across `|` boundaries is dynamically segmented into per-cell edits by the engine. Structural table changes (adding/removing `|` columns) via text replacement are explicitly intercepted and rejected to prevent cross-cell data corruption.
    *   **Heuristic Cell Matching**: When modifying table rows via text substitution, we explicitly `.strip()` individual cell contents to bypass whitespace drift and accurately anchor comments to the semantically modified cell.

### 6. The Unified `DocumentChange` API
*   **Flat API Structure**: The LLM interacts with a flat list of `DocumentChange` objects (Discriminated Union of `ModifyText`, `AcceptChange`, `RejectChange`, `ReplyComment`).
*   **Search & Replace First**: Pure insertions and deletions are intentionally hidden from the LLM. All text modifications must be executed as search-and-replace (`ModifyText`) to guarantee sufficient anchoring context for the fuzzy matcher.
*   **Universal Tooling**: Disk-based and Live Word tools share the same endpoints (`read_docx`, `process_document_batch`). On Windows, omitting file paths dynamically routes the command to the active Live Word COM object, preventing LLM tool selection paralysis.
*   **Heading Depth Validation**: Markdown heading depths (`#`) are strictly clamped to a maximum of 6. Exceeding this raises a `BatchValidationError` to prevent the silent generation of broken/unstyled XML blocks.

### 7. MCP Apps & UI Rendering
*   **Custom HTML Apps**: We use FastMCP's `AppConfig(resource_uri="ui://...")` to serve custom HTML/CSS interfaces for complex tools (e.g., `validate_documents`). We maintain full control over the markup.
*   **Vanilla JS**: We avoid external untested JS libraries to bypass CSP restrictions and ensure offline reliability. The iframe client uses a minimal `window.postMessage` JSON-RPC implementation to complete the Host handshake (`ui/initialize` -> `ui/notifications/initialized`) and receive payloads (`ui/notifications/tool-result`).
*   **Dynamic Resizing**: HTML resources must include a `ResizeObserver` that emits `ui/notifications/size-changed` messages to the Host, allowing the iframe to expand seamlessly as content is injected.
*   **Dual Payloads**: Tools utilizing UIs return `ToolResult(content=..., structured_content={"html": ...})`. This ensures the LLM receives pure Markdown to reason about, while the human user sees the styled HTML.

### 8. Document Sanitization & Part Ejection
*   **Deep Part Ejection**: When completely removing XML parts (e.g., Custom XML, Comments), deleting the elements is insufficient because `python-docx` will repackage empty XML files. We must explicitly sever relationships from `pkg.rels` and `part.rels`, and physically remove the part from `pkg._parts`.
*   **Mathematical Scrub Verification**: For metadata sanitization, we rely on `lxml` + XPath directly on the unzipped DOCX as the absolute source of truth. This strictly bypasses `python-docx` caching layers to mathematically guarantee artifacts are removed.
*   **Modern Comments Architecture**: Word's modern comments span four XML parts (`comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`). The resolved status (`w15:done="1"`) is stored inside `commentsExtended.xml` and must be parsed and scrubbed from there.
*   **Empty Comment Part Lifecycle**: Empty comment XML parts are explicitly left intact rather than purged when all comments are removed, as dynamically mutating the `pkg.rels` matrix across different `python-docx` versions is volatile and can cause unrecoverable package corruption.
*   **Multi-Author Sanitization Awareness**: When executing a full document sanitization with auto-acceptance (`accept_all=True`), if multiple distinct authors are detected in pending track changes, a high-visibility warning is injected into the report to alert the user of potential 'silent smuggles'.
*   **Proxy Class OPC Binding**: When modifying `python-docx` XML parts that have native proxy classes (like `XmlPart` for Headers/Footers), we must re-bind `part._element = part._adeu_element` to ensure successful serialization on save.

### 9. Live MS Word Interop (Windows COM)
*   **Platform Safety**: All live Word tools (`live_word.py`) depend on `pywin32` and are conditionally registered via `sys.platform == 'win32'`.
*   **COM Apartment Lifecycle**: Microsoft Office COM objects are strictly Single-Threaded Apartment (STA). Because FastMCP and `pytest` hold proxy frames unpredictably, we **intentionally omit** `pythoncom.CoUninitialize()` and `app.Quit()` during test teardown. We let the OS/Python GC handle teardown naturally to prevent fatal RPC/Access Violations (`0x800706be`).
*   **Index Drift Mitigation**:
    *   **Extraction Parity**: Active COM extraction uses an *event-based string builder* (sorting events by length and type) to inject CriticMarkup tags safely. This handles infinitely nested/overlapping annotations (e.g., comments wrapping redlines) without string offset drift.
    *   **Pre-Resolution**: Modifying text natively adds Revisions, shifting `doc.Revisions` indices. We pre-resolve and cache all target COM objects *before* applying a batch of `DocumentChange` operations so Accept/Reject actions target the correct revisions.
    *   **Minimal-Diff Replacements**: Live Word COM replacements must mathematically trim common context (`trim_common_context`) from the target string's prefix and suffix before executing the COM replacement. Replacing the entire target string wholesale creates bloat and destroys adjacent comment anchors.
*   **Comment Bounds**: We strictly use `Comment.Scope` (the highlighted text), not `Comment.Reference` (the 0-length anchor), to accurately extract target strings for Comment annotations.
*   **Identity Spoofing & Deadlocks**: Tools temporarily hijack `Word.Application.UserName` and toggle `doc.TrackRevisions` to apply tracked changes cleanly as the Agent. *Constraint*: Modern M365 enforces logged-in MS Account identities on Comments. Attempting to spoof comment authors via `app.Options.UseLocalUserInfo` causes fatal STA thread deadlocks. Live comments will natively show the local user's real name. Live COM batch executions will natively surface a warning when the `author_name` is overridden by the host OS M365 identity to maintain predictable audit trails.

### 10. COM vs XML Impedance Mismatches
Achieving 100% CriticMarkup extraction parity between Live COM and Disk XML requires bridging deep structural differences:
*   **State Machine Parity**: Both engines MUST feed into the exact same event-driven state machine (`DocxEvent` accumulation -> `_get_wrappers` -> `_build_merged_meta_block`) to ensure identical tag ordering and bubble grouping.
*   **Formatting (Explicit vs Inherited)**: Disk XML evaluates explicit `<w:b/>` tags. Word COM's `rng.Find.Font.Bold` evaluates WYSIWYG bold (including inherited styles like Headings). Live COM must explicitly cross-check `rng.Style.Font.Bold` to avoid double-styling markdown markers (`**`) on inherited runs.
*   **Table Rendering & COM Offset Drift**: Word COM injects hidden structural characters (`\r\x07`) at cell boundaries, breaking Python string indices. Solution: Decouple structural markdown extraction (`|` for cells) from native COM execution, using exact index mapping arrays paired with `rng.Find` to securely bypass COM index drift.
*   **Ephemeral Session IDs**: Word natively assigns `w:id="0"` to all unsaved revisions/comments in live memory, randomly assigning persistent IDs during a Save. **IDs are session-bound.** Agents must treat Save/Reload boundaries as a state wipe and re-index the document IDs afterward.
*   **Destructive Native Edits (Comment Rescue)**: Assigning `Range.Text` in Live Word natively destroys any comments anchored to that text. Batch processors must explicitly cache, rescue, and re-anchor comments during string replacements.
*   **Pure Comment Redline Prevention**: If an agent requests a pure comment (`target_text == new_text`), executing a `.Text` replacement natively forces Word to generate spurious `<w:del>` and `<w:ins>` pairs. We must explicitly short-circuit and attach the comment directly without modifying the text to prevent document timeline pollution.
*   **Structured Insertions (Reverse Sandwich Algorithm)**: Word natively corrupts document structure if a tracked deletion precedes a new tracked paragraph break (`\r`), physically pushing the deletion into the next paragraph. To safely insert multi-paragraph replacements, we use a "Reverse Sandwich": insert Line 1 *before* the target, insert remaining lines *after*, and execute `.Delete()` on the target *last*.
*   **COM Comment Truncation Limit**: Microsoft Word's COM API (`doc.Comments.Add`) actively refuses to span a range containing both tracked deletions and tracked paragraph breaks. When executing multi-paragraph replacements, comments must be strictly anchored to the first inserted line (`Line 1`) to guarantee survival and visibility.
*   **Empty Runs & Timestamps**: Both engines must explicitly skip empty runs to synchronize lookahead bubble grouping. Both must emit full ISO-8601 timestamps without truncation to preserve chronological signals.

### 11. Redline Engine Execution Model (Performance & Safety)
*   **Pre-Resolution & Backwards-Sweep**: To avoid O(N²) scaling on large documents, all text edits are mapped against the *initial* document state to cache their physical offsets before any DOM mutations occur. Edits are then sorted in reverse order and applied bottom-up in a single O(N) sweep. This completely eliminates index drift and bypasses rebuilding the Virtual DOM map mid-batch. (Note: Because of this reverse execution, bottom-most edits receive lower sequential IDs like `Chg:1`).
*   **Namespace Injection & Serialization Safety**: Custom namespaces (e.g., `xmlns:w16du`) are injected directly into the raw XML byte stream at the document root upon load to prevent `lxml` from generating `ns0` alias artifacts that corrupt downstream processors. Crucially, we bypass `python-docx`'s `serialize_for_reading()` which forces destructive pretty-printing. We use raw `lxml.etree` with `pretty_print=False` and `remove_blank_text=False` to strictly preserve Microsoft Word's original whitespace structure and prevent massive, noisy diffs.
*   **Formatting Inheritance & Optimal Coalescing**: When text is inserted or replaced inside a styled span (e.g., bold), the new text natively inherits the context's styling (`suppress_inherited=False`) to prevent visual data loss. The engine's run coalescer then merges the matching runs to produce optimal, single-run output for whole-span replacements.
*   **Modification Comment Anchoring**: When a single edit causes a deletion and an insertion (`w:del` followed by `w:ins`), comments spanning the modification are explicitly anchored from the start of the `del` element to the end of the `ins` element to successfully encapsulate the full atomic revision.
*   **Nested Redline Strict Refusal**: Edits that target text strictly inside an active `<w:ins>` authored by a *different* user are explicitly rejected (`BatchValidationError`) to prevent confusing nested redline fragmentation.
*   **Paragraph Break Tracking**: When multi-paragraph text is inserted (`\n\n`), the engine explicitly injects an `<w:ins>` marker inside the `<w:pPr><w:rPr>` of the newly created paragraph, ensuring MS Word natively tracks the paragraph break itself.
*   **Diff Hunk Coalescing**: To prevent redline fragmentation, adjacent textual diff hunks separated by short runs of stable tokens (≤ 4 words) within the same paragraph are mathematically coalesced into a single unified edit hunk.
*   **Contiguous Orphan Comment Sweep**: When accepting/rejecting changes, comment anchor cleanup (`w:commentRangeStart/End`) must sweep across the entire contiguous block of adjacent redline tags (`w:ins`/`w:del`) to prevent orphaned anchors from leaking into the document body.

### 12. FastMCP Concurrency & Tooling
*   **Event Loop Blocking**: Any heavy, synchronous CPU or disk-bound tasks (e.g., `sanitize_docx`, or heavy batch processing) called from an async FastMCP tool endpoint MUST be wrapped in `asyncio.to_thread()`. This prevents the `asyncio` event loop from freezing and dropping MCP client heartbeats.
*   **Kwargs in to_thread**: When dispatching functions via `asyncio.to_thread` that expect keyword-only arguments, arguments must be explicitly passed as keyword arguments to prevent `TypeError: takes X positional arguments` errors.
*   **Testing Tools**: FastMCP's `@tool` decorator heavily modifies function metadata. When asserting against an MCP tool's prompt or docstring in tests, prefer `inspect.getsource(func)` or `getattr(func, "description", "")` rather than `func.__doc__`.

### 13. Domain Gaps & Projection Syntax (Semantic Markdown)
To solve domain visibility gaps without adding new MCP tools, `read_docx` projects a strictly defined semantic dialect of Markdown:
*   **Italics Strictness**: Adeu strictly uses `_italic_`. The `*italic*` syntax is explicitly parsed as literal text.
*   **Footnotes/Endnotes**: Projected inline as `[^fn-{w:id}]` (using stable OOXML IDs, not display numbers) and appended at the bottom. Fully bi-directional. Editing them natively updates `footnotes.xml`. *Constraint*: Generic XML parts lack `get_style()`, so `_get_paragraph_style_safe` gracefully handles missing formatting attributes.
*   **Bi-directional Links**: `[text](url)`. Editing the text applies tracked changes. Editing the URL executes a silent `URL_RETARGET` operation in `_rels` (no redlines emitted).
*   **Cross-References**: Projected as `[~text~](#_Ref)`. The `[~...~]` wrapper indicates computed/read-only text. Attempting to modify the display text or hash via `ModifyText` is strictly rejected (`BatchValidationError`) to prevent dependency corruption.
*   **Internal Anchors**: Structural bookmarks project inline as `{#_BookmarkName}`. Modifying or fabricating this syntax throws a `BatchValidationError` (Strict Refusal) as text-replacement cannot safely resolve these structural nodes.
*   **Structural Appendix & Boundary Validation**: Structural XML (Bookmarks, TOC boundaries) is appended to the bottom behind a `<!-- READONLY_BOUNDARY_START -->` marker. The boundary validator strictly uses *resolved physical indices* (`find_all_match_indices`) rather than blind string-matching, safely allowing body edits that coincidentally share text with the appendix.
*   **Defined Terms & Semantic Diagnostics**: We project a "Symbol Table" of terms and "Semantic Diagnostics" (Unresolved, Unused, Duplicate, Typo warnings) in the Appendix.
    *   *Language-Agnostic Extraction*: Terms are extracted structurally via typography (leading/inline quotes), not brittle English regexes. Terms must be used ≥1 time to be included.
    *   *High-Signal Diagnostics*: Typo candidates are grouped by target term. False positives are pruned via stop-word filtering, singular/plural exclusion, and a strict rule for short acronyms (≤5 chars: max edit distance of 1 and identical first letter).

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
- **v1.3.0**: UI Integrations
    - **Native Open**: Added `open_local_file` tool to allow the Custom MCP UI to seamlessly launch the native OS default application (Word, PDF readers, etc.) without hitting iframe sandbox restrictions via the `tools/call` RPC method.
- **v1.1.0**: Live Word Interop & Agentic Workflows.
    - **Live MS Word Engine**: Fully integrated Windows COM engine allowing agents to execute live edits on an active MS Word canvas (`sys.platform == "win32"`).
    - **Flat API**: Unified `DocumentChange` discriminated union deployed for the MCP interface.
    - **Testing**: End-to-end LLM verification complete and backwards compatibility preserved.
    - **UI Layer**: Zero-dependency, Vanilla JS custom HTML MCP Apps implementation for tools like `validate_documents`.