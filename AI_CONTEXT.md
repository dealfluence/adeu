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
    *   *Deletion Survival*: When a text run containing Special Content is marked for deletion (`w:del`), the engine uses `deepcopy` to clone the run. This ensures images and structural elements physically survive inside the deletion block instead of being silently erased.

### 3. The "Virtual Text" Contract
*   `ingest.py` and `mapper.py` must be strictly synchronized.
*   If `ingest.py` produces virtual characters (e.g., `{==` or `**`), `mapper.py` must explicitly account for them as `virtual` spans so the `RedlineEngine` knows they do not exist in the DOM.

### 4. Agentic Distribution Strategy & Monorepo
*   **Dual Engine Architecture**: Adeu maintains parallel backends in Python (FastMCP, rich CLI) and TypeScript/Node.js (`@adeu/core`, `@adeu/mcp-server`).
*   **"Make Both Perfect" Principle**: When discovering behavior divergence between the Python and Node engines, we do not blindly enforce bug-for-bug parity. Instead, we identify the most natural, intuitive Microsoft Word behavior and backport it to *both* engines to elevate the overall baseline.
*   **Native Desktop Extension (MCPB)**: We ship a fully self-contained Node.js backend bundled as a zero-dependency `index.js` for Claude Desktop extensions. This eliminates Python/`uvx` environment constraints for end-users. The 1.2MB bundle is ignored in `.gitignore`, built entirely via CI/CD, and distributed via NPM and GitHub Releases to avoid repository bloat.
*   **Auto-Configuration**: The Python CLI `adeu init` command still manages local dev injections into `claude_desktop_config.json`.
    *   *Safety*: It must always create a timestamped backup (`.bak`) before modifying the user's config.
    *   *OS Agnostic*: It handles path resolution for Windows (`%APPDATA%`) and macOS (`~/Library`) automatically.
*   **Smithery Marketplace Publishing**: To bypass the "Schema Deadlock" (Anthropic's `mcpb pack` rejects tool schemas, but Smithery's registry requires them), we use a dynamic patch strategy. `scripts/patch_smithery_mcpb.py` boots the compiled Node server, extracts live schemas via JSON-RPC (`tools/list`), and injects them into the packaged `.mcpb` manifest before publishing.
*   **Unified Monorepo & LangChain Integration**: We maintain a dedicated `langchain-adeu` package inside `langchain/` that wraps our offline-capable core Python engine as native LangChain tools.
    *   *Version Alignment*: To maintain a cohesive release footprint, all sub-projects (Python, Node, and LangChain) are synchronized to the exact same semantic version number via `scripts/bump.py`, which automatically triggers lockfile updates (`uv.lock` and `package-lock.json`) across workspaces.
    *   *n8n Codex Exception (do NOT version-sync)*: The n8n codex file `node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.json` is deliberately **excluded** from `bump.py` because its fields do not track the npm package version. `nodeVersion` mirrors the `version` property of the node class in `Adeu.node.ts` (currently `version: 1` → `"1.0"`), and `codexVersion` is the codex *schema* version (fixed at `"1.0"`). Syncing either to the package version (e.g. `1.17`) breaks n8n Cloud verification — this happened once and was flagged by n8n Community Engineering. Only bump `nodeVersion` when the node class's `version` actually changes.
    *   *Hatchling Relative Workspace Sandbox*: Hatchling prohibits scanning parent relative directories outside the workspace root during isolated builds. We bypass this constraint by programmatically localizing files (e.g. copying the repository root `LICENSE` file locally into packaging subdirectories) and referencing local paths in `pyproject.toml`.
    *   *OIDC Trusted Publishing*: In our release pipeline, package uploads to PyPI are handled securely via GitHub Actions OIDC Trusted Publishers (configured for both `adeu` and `langchain-adeu` environments), completely eliminating the storage of static credentials in GitHub Secrets.

### 5. Block-Level Parsing & Tables
*   **Sequential Iteration**: We iterate over document elements (`w:p` and `w:tbl`) in strict XML order using `iter_block_items`. We do *not* iterate `part.paragraphs` and `part.tables` separately, as this destroys document flow (e.g., tables appearing after all text).
*   **Recursion**: Ingestion and Mapping are recursive. `Document` -> `Table` -> `Cell` -> `Block Items` -> ...
*   **Synchronization Invariants**:
    *   **Empty Rows**: `ingest.py` must *never* skip empty table rows. `mapper.py` iterates all rows in the DOM; skipping one in text extraction causes index misalignment.
    *   **Deleted Rows (Clean View)**: To simulate the "Accepted" document state (`clean_view=True`), the extraction pipelines explicitly skip table rows that contain a `<w:del>` tag inside their `<w:trPr>` properties.
    *   **Separators**: Row separators (`\n`) are injected *between* rows. Virtual pipes (` | `) separate cells.
    *   **Cell Isolation**: The virtual `|` boundary represents a hard `<w:tc>` cell wall. Modifying text across `|` boundaries is dynamically segmented into per-cell edits by the engine. Structural table changes (adding or removing rows, or adding/removing `|` columns) via text replacement are explicitly intercepted and strictly rejected to prevent gridspan corruption and misaligned `<w:tc>` elements.
    *   **Heuristic Cell Matching**: When modifying table rows via text substitution, we explicitly `.strip()` individual cell contents to bypass whitespace drift and accurately anchor comments to the semantically modified cell.
    *   **Structural Table Safety**: Table row manipulation is strictly enforced via intent-based API models (`InsertTableRow`, `DeleteTableRow`) to safely manipulate the DOM without risking gridspan corruption. *(Note: Currently supported for disk-based DOCX editing only; gracefully intercepted and rejected in Live Word COM)*.

### 6. The Unified `DocumentChange` API
*   **Flat API Structure**: The LLM interacts with a flat list of `DocumentChange` objects (Discriminated Union of `ModifyText`, `AcceptChange`, `RejectChange`, `ReplyComment`, `InsertTableRow`, `DeleteTableRow`). We intentionally reject nested parameter objects in favor of flat top-level arguments (`search_query`, `search_regex`, `match_mode`, `regex`) to ensure seamless CLI argument parsing and high LLM execution accuracy.
*   **Search & Replace First**: Pure insertions and deletions are intentionally hidden from the LLM. All text modifications must be executed as search-and-replace (`ModifyText`) to guarantee sufficient anchoring context for the fuzzy matcher.
*   **Representation-Based Linear Traversal**: Match order for tools leveraging multiple occurrences (e.g., `match_mode="first"` or `"all"`) is defined strictly by their linear sequence in the flattened, projected CriticMarkup representation (`full_text`), abstracting away the fragmented physical XML storage sequence.
*   **Symmetric Channel Contract**: Responses preserve identical Markdown layouts across both the `content` (LLM-facing) and `structured_content` (UI-facing) channels, packing the structured UI payload neatly inside standard JSON keys (`markdown`, `file_path`, `title`).
*   **Structured Report Alignment**: `process_document_batch` edit execution reports strictly mirror the layout of `read_docx` search matches (capturing `pages`, `heading_path`, and `occurrences_modified`) to provide the LLM with a highly correlatable audit trail.
*   **Universal Tooling**: Disk-based and Live Word tools share the same endpoints (`read_docx`, `process_document_batch`). On Windows, omitting file paths dynamically routes the command to the active Live Word COM object, preventing LLM tool selection paralysis.
*   **Heading Depth Validation**: Markdown heading depths (`#`) are strictly clamped to a maximum of 6. Exceeding this raises a `BatchValidationError` to prevent the silent generation of broken/unstyled XML blocks.
*   **Strict Action Validation**: The batch engine strictly enforces referential integrity. Attempting to execute review actions (like `ReplyComment`) on non-existent or fake IDs immediately raises a `BatchValidationError` rather than failing silently.

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
    *   **Snapshot Engine Delegation**: To safely process complex `regex` and `match_mode` workflows, the Live Word adapter leverages an in-memory `RedlineEngine` snapshot built via Flat OPC extraction to pre-resolve exact physical offsets, bypassing the need for complex Bounded Levenshtein or regex recalculations natively in COM.
*   **Comment Bounds**: We strictly use `Comment.Scope` (the highlighted text), not `Comment.Reference` (the 0-length anchor), to accurately extract target strings for Comment annotations.
*   **Identity Spoofing & Deadlocks**: Tools temporarily hijack `Word.Application.UserName` and toggle `doc.TrackRevisions` to apply tracked changes cleanly as the Agent. *Constraint*: Modern M365 enforces logged-in MS Account identities on Comments. Attempting to spoof comment authors via `app.Options.UseLocalUserInfo` causes fatal STA thread deadlocks. Live comments will natively show the local user's real name. Live COM batch executions will natively surface a warning when the `author_name` is overridden by the host OS M365 identity to maintain predictable audit trails.

### 10. COM vs XML Impedance Mismatches
Achieving 100% CriticMarkup extraction parity between Live COM and Disk XML requires bridging deep structural differences:
*   **State Machine Parity**: Both engines MUST feed into the exact same event-driven state machine (`DocxEvent` accumulation -> `_get_wrappers` -> `_build_merged_meta_block`) to ensure identical tag ordering and bubble grouping.
*   **Formatting (Explicit vs Inherited)**: Disk XML evaluates explicit `<w:b/>` tags. Word COM's `rng.Find.Font.Bold` evaluates WYSIWYG bold (including inherited styles like Headings). Live COM must explicitly cross-check `rng.Style.Font.Bold` to avoid double-styling markdown markers (`**`) on inherited runs.
*   **Table Rendering & COM Offset Drift**: Word COM injects hidden structural characters (`\r\x07`) at cell boundaries, breaking Python string indices. Solution: Decouple structural markdown extraction (`|` for cells) from native COM execution, using exact index mapping arrays paired with `rng.Find` to securely bypass COM index drift.
*   **Ephemeral Session IDs**: Word natively assigns `w:id="0"` to all unsaved revisions/comments in live memory, randomly assigning persistent IDs during a Save. **IDs are session-bound.** Agents must treat Save/Reload boundaries as a state wipe and re-index the document IDs afterward.
*   **Destructive Native Edits (Comment Rescue)**: Assigning `Range.Text` in Live Word natively destroys any comments anchored to that text. Batch processors must explicitly cache, rescue, and re-anchor comments during string replacements.
*   **SmartSelection & Anchor Bleed**: Microsoft Word's `SmartSelection` natively snaps comment anchors leftward across spaces into preceding un-tracked sentences. To mathematically isolate anchors during structured multi-paragraph replacements, we temporarily disable `app.Options.SmartSelection`, inject an un-tracked "Sacrificial 'X'" character, attach the comment, and un-track-delete the 'X' to perfectly collapse the anchor.
*   **Pure Comment Redline Prevention**: If an agent requests a pure comment (`target_text == new_text`), executing a `.Text` replacement natively forces Word to generate spurious `<w:del>` and `<w:ins>` pairs. We must explicitly short-circuit and attach the comment directly without modifying the text to prevent document timeline pollution.
*   **Structured Insertions (Reverse Sandwich Algorithm)**: Word natively corrupts document structure if a tracked deletion precedes a new tracked paragraph break (`\r`), physically pushing the deletion into the next paragraph. To safely insert multi-paragraph replacements, we use a "Reverse Sandwich": insert Line 1 *before* the target, insert remaining lines *after*, and execute `.Delete()` on the target *last*.
*   **COM Comment Truncation Limit**: Microsoft Word's COM API (`doc.Comments.Add`) actively refuses to span a range containing both tracked deletions and tracked paragraph breaks. When executing multi-paragraph replacements, comments must be strictly anchored to the first inserted line (`Line 1`) to guarantee survival and visibility.
*   **Empty Runs & Timestamps**: Both engines must explicitly skip empty runs to synchronize lookahead bubble grouping. Both must emit full ISO-8601 timestamps without truncation to preserve chronological signals.
*   **Tracked Formatting Fragmentation**: Word natively splits a single tracked revision (`<w:ins>`) into multiple contiguous elements if partial formatting (like bold) is applied mid-revision. The ingestion state machine explicitly ignores pure state transition boundaries (like `ins_end`) when building CriticMarkup to seamlessly coalesce these fragments back into a single unified tag.

### 11. Redline Engine Execution Model (Performance & Safety)
*   **Sequential Batch Contract (Chaining + Transactional Rejection)**: Batches apply SEQUENTIALLY in both engines and both modes (dry-run and write): each edit is validated and applied against the document state produced by the edits before it, so a later edit may legitimately target text an earlier edit introduced (chaining). Dry-run runs the identical loop, so its report always mirrors the real outcome. If any edit fails validation, the whole batch is rejected transactionally — the engine rolls back to a pre-batch snapshot and raises `BatchValidationError` (dry-run reports the same rejection per edit, marking would-be-applied edits "Not applied: the batch is transactional…"). Validation errors raised after earlier edits already applied carry a sequential-contract hint (the failing target may be stale — re-target the updated text). Text projections (raw/clean/original mappers) are rebuilt between edits so each edit resolves against fully current state.
*   **Pre-Resolution & Backwards-Sweep (within one edit)**: Inside a single edit's application, all of its resolved sub-edits are mapped against the current document state to cache their physical offsets before any DOM mutations occur, then sorted in reverse order and applied bottom-up in a single O(N) sweep — eliminating index drift without rebuilding the Virtual DOM map mid-edit. Caller-pinned index edits (e.g. `generate_edits_from_text` output, whose coordinates refer to the initial state) are applied together this way, first in the batch, before text-anchored edits re-resolve sequentially.
*   **Namespace Injection & Serialization Safety**: Custom namespaces (e.g., `xmlns:w16du`) are injected directly into the raw XML byte stream at the document root upon load to prevent `lxml` from generating `ns0` alias artifacts that corrupt downstream processors. Crucially, we bypass `python-docx`'s `serialize_for_reading()` which forces destructive pretty-printing. We use raw `lxml.etree` with `pretty_print=False` and `remove_blank_text=False` to strictly preserve Microsoft Word's original whitespace structure and prevent massive, noisy diffs.
*   **Formatting Inheritance & Optimal Coalescing**: When text is inserted or replaced inside a styled span (e.g., bold), the new text natively inherits the context's styling (`suppress_inherited=False`) to prevent visual data loss. The engine's run coalescer then merges the matching runs to produce optimal, single-run output for whole-span replacements.
*   **Modification Comment Anchoring**: When a single edit causes a deletion and an insertion (`w:del` followed by `w:ins`), comments spanning the modification are explicitly anchored from the start of the `del` element to the end of the `ins` element to successfully encapsulate the full atomic revision.
*   **Nested Redline Refusal (Foreign `<w:ins>` vs Comment Ranges)**: Edits overlapping an active `<w:ins>` authored by a *different* user are rejected (`BatchValidationError`) when they straddle the insertion boundary or fan out via `match_mode="all"`; a deliberate strict/first edit fully contained inside the foreign insertion is allowed and nested (the enclosing `<w:ins>` is split). Foreign **comment ranges** do *not* block deliberate strict/first edits — amending body text under a colleague's comment is a normal review workflow and the comment anchor survives the tracked change — but `match_mode="all"` fan-outs are refused (via `TextSpan.comment_ids` tracking) with a comment-specific error, so bulk replacements cannot silently sweep through another author's annotations.
*   **Double-Sided Paragraph Merges Strict Refusal**: Regex matches spanning a structural paragraph boundary (`\n\n`) with body text on both sides are explicitly rejected to safely prevent structural DOM corruption.
*   **Paragraph Break Tracking**: When multi-paragraph text is inserted (`\n\n`), the engine explicitly injects an `<w:ins>` marker inside the `<w:pPr><w:rPr>` of the newly created paragraph, ensuring MS Word natively tracks the paragraph break itself.
*   **Multi-Paragraph Comment Anchoring (Disk)**: When a text replacement spans multiple paragraphs, the engine explicitly anchors the start of the comment to the first paragraph and the end of the comment to the last inserted paragraph to ensure survival and proper rendering.
*   **Diff Hunk Coalescing**: To prevent redline fragmentation, adjacent textual diff hunks separated by short runs of stable tokens (≤ 4 words) within the same paragraph are mathematically coalesced into a single unified edit hunk.
*   **Contiguous Orphan Comment Sweep**: When accepting/rejecting changes, comment anchor cleanup (`w:commentRangeStart/End`) must sweep across the entire contiguous block of adjacent redline tags (`w:ins`/`w:del`) to prevent orphaned anchors from leaking into the document body.
*   **Phase 2 OOXML Paragraph Merges (Bottom-Up)**: When text deletions span paragraph boundaries (`\n\n`), the engine explicitly coalesces adjacent paragraphs. To prevent orphaned containers, these structural merges are executed in *reverse order* (bottom-up) over `virtual_spans`, actively jumping over invisible structural nodes (like `w:bookmarkStart`) via DOM sibling traversal to securely maintain DOM sibling pointers.
*   **Safe Paragraph Acceptance**: When executing `accept_all_revisions`, the engine explicitly checks for surviving visible content (`w:t`, `w:tab`, `w:br`) that is *not* enclosed in a `<w:del>` block. If content survives, it safely strips the tracked paragraph-break deletion marker but preserves the paragraph container, preventing catastrophic data loss of partially deleted paragraphs.

### 12. FastMCP Concurrency & Tooling
*   **Event Loop Blocking**: Any heavy, synchronous CPU or disk-bound tasks (e.g., `sanitize_docx`, or heavy batch processing) called from an async FastMCP tool endpoint MUST be wrapped in `asyncio.to_thread()`. This prevents the `asyncio` event loop from freezing and dropping MCP client heartbeats.
*   **Kwargs in to_thread**: When dispatching functions via `asyncio.to_thread` that expect keyword-only arguments, arguments must be explicitly passed as keyword arguments to prevent `TypeError: takes X positional arguments` errors.
*   **Testing Tools**: FastMCP's `@tool` decorator heavily modifies function metadata. When asserting against an MCP tool's prompt or docstring in tests, prefer `inspect.getsource(func)` or `getattr(func, "description", "")` rather than `func.__doc__`.
*   **Testing Redlines**: `python-docx`'s native `Paragraph.text` property silently fails to read text inside `<w:ins>` elements. Test assertions must strictly use the internal `extract_text_from_stream(clean_view=True)` function to accurately evaluate accepted document states.

### 13. Domain Gaps & Projection Syntax (Semantic Markdown)
To solve domain visibility gaps without adding new MCP tools, `read_docx` projects a strictly defined semantic dialect of Markdown:
*   **Italics Strictness**: Adeu strictly uses `_italic_`. The `*italic*` syntax is explicitly parsed as literal text.
*   **Footnotes/Endnotes**: Projected inline as `[^fn-{w:id}]` (using stable OOXML IDs, not display numbers) and appended at the bottom. Fully bi-directional. Editing them natively updates `footnotes.xml`. *Constraint*: Generic XML parts lack `get_style()`, so `_get_paragraph_style_safe` gracefully handles missing formatting attributes.
*   **Bi-directional Links**: `[text](url)`. Editing the text applies tracked changes. Editing the URL executes a silent `URL_RETARGET` operation in `_rels` (no redlines emitted).
*   **Cross-References**: Projected as `[~text~](#_Ref)`. The `[~...~]` wrapper indicates computed/read-only text. Attempting to modify the display text or hash via `ModifyText` is strictly rejected (`BatchValidationError`) to prevent dependency corruption.
*   **Internal Anchors**: Structural bookmarks project inline as `{#_BookmarkName}`. Modifying or fabricating this syntax throws a `BatchValidationError` (Strict Refusal) as text-replacement cannot safely resolve these structural nodes.
*   **Determined Page Ranges on Flat Outline Nodes**: Section start and end pages are structurally calculated post-pagination over the flat `OutlineNode` array rather than descending recursively into the DOM tree.
*   **Structural Appendix & Boundary Validation**: Structural XML (Bookmarks, TOC boundaries) is appended to the bottom behind a `<!-- READONLY_BOUNDARY_START -->` marker. The boundary validator strictly uses *resolved physical indices* (`find_all_match_indices`) rather than blind string-matching, safely allowing body edits that coincidentally share text with the appendix.
*   **Defined Terms & Semantic Diagnostics**: We project a "Symbol Table" of terms and "Semantic Diagnostics" (Unresolved, Unused, Duplicate, Typo warnings) in the Appendix.
    *   *Language-Agnostic Extraction*: Terms are extracted structurally via typography (leading/inline quotes), not brittle English regexes. Terms must be used ≥1 time to be included.
    *   *High-Signal Diagnostics*: Typo candidates are grouped by target term. False positives are pruned via stop-word filtering, singular/plural exclusion, and a strict rule for short acronyms (≤5 chars: max edit distance of 1 and identical first letter).

### 14. TypeScript / Node.js Engine Constraints
*   **DOM Simulation**: `python-docx` is emulated via `jszip` and `@xmldom/xmldom`. Strict shim functions (`findChild`, `findAllDescendants`) simulate `lxml`'s direct-child vs recursive search to prevent catastrophic DOM traversal mismatches.
*   **Regex Engine Limits**: JS regex does not support Python's atomic grouping `(?>...)`. Catastrophic backtracking in fuzzy matchers is prevented using mathematically equivalent character classes (e.g., `[*_]*`).
*   **Regex Compatibility (ES2022)**: We strictly target ES2022 to guarantee support for regex lookbehinds (`(?<!...)`), which are heavily utilized to cleanly port Python's semantic domain extraction logic.
*   **Whitespace Evaluation**: Python's `isspace()` checks the whole string, whereas JS requires explicit regex anchors (`/^\s+$/`) to prevent aggressive context-trimming bugs.
*   **diff-match-patch Volatility & Format Parity**: Inside the core Redline Engine, `diff_charsToLines_` is bypassed using manual array-mapping loops to prevent minifier inconsistencies. For the external Diff Tool (`diff.ts`), both engines strictly emit a custom `@@ Word Patch @@` sub-word level diff format. Standard line-based Unified Diffs are intentionally avoided to guarantee identical LLM interpretation and token-level granularity across both environments.
*   **Package Mutation Parity**: Emulating `python-docx`'s save flow requires manually serializing mutated `@xmldom/xmldom` elements via `XMLSerializer` and dynamically updating `[Content_Types].xml` and `.rels` files inside `JSZip` to prevent OPC package corruption.
*   **Explicit Relationship Mapping & Strict Namespaces**: In pure JS (`@xmldom` + `jszip`), we cannot rely on implicit `python-docx` relationship abstractions. External targets (like Hyperlinks) require explicitly fetching/creating the target `_rels/document.xml.rels` file and manually injecting the `<Relationship>` node. Crucially, these nodes MUST be created with explicit namespace awareness (`createElementNS`), or `XMLSerializer` will silently drop them during package serialization.
*   **Pure-TS Bounded Levenshtein**: Node.js MCP environments cannot use C-bindings (like `rapidfuzz`). We implement a highly optimized, pure-TS Bounded Levenshtein algorithm (`max_dist = 2`) directly in the structural appendix generator to evaluate typographical errors without blocking the V8 event loop on large documents.
*   **Test Utilities**: Because JS lacks `python-docx`'s in-memory factory methods, `test-utils.ts` shims document building by dynamically wiping and mutating an empty `initial.docx` fixture with raw OOXML node injection.
*   **Zero-Dependency Finalization**: The Node.js `.mcpb` bundle strictly omits PDF export (`docx2pdf`/LibreOffice) and AES encryption (`msoffcrypto-tool`) capabilities from the `finalize_document` tool to strictly maintain its zero-dependency architecture.
*   **Native OOXML Locking**: Document protection is achieved natively by injecting `<w:documentProtection w:edit="readOnly" w:enforcement="1"/>` directly into `word/settings.xml` using `@xmldom/xmldom`.
*   **XML Serialization Quirks**: Emptying elements in `@xmldom/xmldom` (`el.textContent = ""`) serializes them as self-closing tags (e.g., `<Template/>` instead of `<Template></Template>`), requiring mathematically equivalent but structurally loose string assertions in unit tests.

## Developer Workflows

### Testing
*   **Regression Pattern**: Create `tests/test_repro_[issue].py` to isolate bugs before fixing.
*   **Golden Files**: `tests/fixtures/golden.docx` is the source of truth for Modern Comments (Word 2021+) XML structure.

### Deployment
*   **Versioning**: Semantic versioning in `pyproject.toml`. `src/adeu/__init__.py` dynamically loads this via `importlib.metadata`.
*   **Dependencies**: Uses `uv` (PEP 621 standard) with `hatchling` as the build backend. `python-docx` is patched at runtime in `comments.py` to support Modern Comments namespaces (`w16cid`, `w15`).
*   **Release pipeline** (`.github/workflows/release.yml`): Two-phase. Run `scripts/bump.py X.Y.Z` (syncs manifests + lockfiles), commit, then push a `vX.Y.Z` tag — the tag triggers `draft-release` (build + draft GitHub Release). A human then clicks **Publish**, which fires the `npm` / `pypi` / `langchain-pypi` / `smithery` jobs.
    *   *Consistency gate*: `scripts/check_release_consistency.mjs` is the single source of truth for codex integrity (n8n `nodeVersion`/`codexVersion`/`node`/categories) + monorepo version lockstep. It runs in CI/pre-push (via a vitest test in `n8n-nodes-adeu`), inside `bump.py`, and as the first step of `draft-release` — a bad tag fails before any asset ships.
    *   *Manual gates*: the only human gate is the draft → **Publish** click. The `pypi`/`langchain-pypi` GitHub Environments are name-only OIDC pins (Trusted Publishing), NOT approval gates — keep the `environment:` blocks; add a "Required reviewers" rule in repo Settings → Environments to re-gate. Smithery publishes on the **publish** event (not at tag/draft time) so all registries ship together.

### Agent Integration Testing
*   To test changes to the MCP server without publishing to PyPI, use `uv run adeu init --local`.
*   This configures Claude Desktop to execute the server from the current local source (`sys.executable` + `cwd`), bypassing `uvx`.

## Current Status

> **Note:** The changelog below is not exhaustive — the latest entry is v1.14.0 while the project
> is currently at **v1.18.4**. See [GitHub Releases](https://github.com/dealfluence/adeu/releases)
> for the full release history.

- **v1.14.0**: Search & Targeted Write Engine Upgrade.
    - Introduced flat `search_query` and `regex` filters directly on the `read_docx` endpoint.
    - Added `match_mode` (`"strict"`, `"first"`, `"all"`) and `regex` parameters to `ModifyText` for highly targeted, multi-occurrence writing workflows.
    - Hardened Redline Engine safety rollbacks against overlapping foreign-author comments and destructive cross-paragraph boundary merges.
    - Delegated Live Word COM heuristic resolution to in-memory `RedlineEngine` snapshots via Flat OPC parsing.
- **v1.6.0**: Native Node.js Monorepo Transition.
    - Ported the entire XML Redline Engine to TypeScript (`@adeu/core` and `@adeu/mcp-server`).
    - Replaced the Python `uvx` wrapper in the Claude Desktop extension with a fully standalone Node.js bundle, permanently eliminating Python dependency requirements for end users.
    - Automated NPM package publishing and GitHub Release bundle generation via CI/CD.
    - Ported the unified `finalize_document` tool (metadata sanitization and native OOXML locking) to pure TypeScript, successfully maintaining the zero-dependency constraint.
    - Hardened Phase 2 OOXML Paragraph Merges (bottom-up processing) and established `@@ Word Patch @@` custom diff parity between Node and Python engines.
- **v1.5.2**: Smithery Marketplace & Desktop Extensions.
    - Published `adeu/adeu` to the Smithery.ai registry.
    - Implemented a Node.js bootstrapper for native Claude Desktop `.mcpb` installation, automatically managing `uvx` dependencies.
    - Resolved schema validation deadlocks between Anthropic and Smithery tooling via dynamic JSON-RPC schema injection.
- **v1.4.2**: Structural Table Edits & Live Word Stabilization.
    - Added `InsertTableRow` and `DeleteTableRow` intent-based models for safe OOXML gridspan manipulation.
    - Resolved COM extraction fragmentation by properly coalescing partial formatting revisions.
    - Eliminated Word COM anchor bleed during multi-paragraph replacements via SmartSelection circumvention and Sacrificial 'X' anchors.
    - Cleaned up duplicate MCP tool descriptions across platform environments.
- **v1.3.0**: UI Integrations
    - **Native Open**: Added `open_local_file` tool to allow the Custom MCP UI to seamlessly launch the native OS default application (Word, PDF readers, etc.) without hitting iframe sandbox restrictions via the `tools/call` RPC method.
- **v1.1.0**: Live Word Interop & Agentic Workflows.
    - **Live MS Word Engine**: Fully integrated Windows COM engine allowing agents to execute live edits on an active MS Word canvas (`sys.platform == "win32"`).
    - **Flat API**: Unified `DocumentChange` discriminated union deployed for the MCP interface.
    - **Testing**: End-to-end LLM verification complete and backwards compatibility preserved.
    - **UI Layer**: Zero-dependency, Vanilla JS custom HTML MCP Apps implementation for tools like `validate_documents`.