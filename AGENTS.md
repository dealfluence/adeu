# Adeu Agent Instructions

Adeu is a DOCX ↔ LLM translation engine and redlining Virtual DOM. It maintains parallel Python and Node.js implementations plus a LangChain integration.

## Repository Structure & Packages

- `python/`: Core Python engine (`adeu`), FastMCP server (`adeu-server`), and CLI (`adeu`). Managed via `uv` (requires Python ≥ 3.12).
- `node/`: Node.js workspace (requires Node ≥ 22.0.0).
  - `node/packages/core`: `@adeu/core` (TypeScript SDK & engine)
  - `node/packages/mcp-server`: `@adeu/mcp-server` (MCP server binary; builds extension bundle to `desktop-extension/index.js`)
  - `node/packages/n8n-nodes-adeu`: `n8n-nodes-adeu` (n8n community node)
- `langchain/`: `langchain-adeu` integration package. Editable-links to `python/` via `tool.uv.sources`.
- `desktop-extension/`: Claude Desktop extension packaging (`manifest.json` + `index.js`).
- `scripts/`: Monorepo automation (`bump.py`, `check_release_consistency.mjs`).

## Commands & Verification Workflow

Run commands from the respective package directory:

### Python (`python/`)
1. **Lint & Format Check:** `uv run ruff check . && uv run ruff format --check .`
2. **Type Check:** `uv run mypy src`
3. **Run Tests:** `uv run pytest`
   - Single test: `uv run pytest tests/test_engine.py -k "test_name"`
   - Property tests (extended): `uv run pytest --hypothesis-profile=hunt`
   - Debug serial run: `uv run pytest -n 0`

### Node.js (`node/`)
1. **Build All Packages:** `npm run build` (MUST build before testing; compiles tsup outputs for all packages and verifies MCP bundle)
2. **Run Tests:** `npm run test`
   - Single test: `cd packages/core && npm run test -- -t "test_name"`
3. **Lint (n8n package):** `cd packages/n8n-nodes-adeu && npm run lint`

### LangChain (`langchain/`)
1. **Lint & Format Check:** `uv run ruff check . && uv run ruff format --check .`
2. **Type Check:** `uv run mypy langchain_adeu`
3. **Run Tests:** `uv run pytest` (single test: `uv run pytest tests/unit_tests/test_apply_changes.py`)

## Operational Quirks & Invariants

- **Build Dependency:** Node workspace requires `npm run build` before `npm test` so compiled dist files exist for `@adeu/mcp-server` and `n8n-nodes-adeu`.
- **Dual-Engine Parity:** Python (`adeu`) and TypeScript (`@adeu/core`) share identical Virtual Text, CriticMarkup, and redlining algorithms. Changes to parsing, diffing, or redlining logic MUST be mirrored in both engines.
- **Monorepo Version Bumping:** Run `python scripts/bump.py [minor|major|patch|X.Y.Z]` from repo root to sync version numbers and lockfiles across subprojects, then verify with `node scripts/check_release_consistency.mjs`.
  - **n8n Codex Exception:** Do NOT bump `nodeVersion` or `codexVersion` in `node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.json` during release bumps. `nodeVersion` mirrors `Adeu.node.ts` runtime `version` (e.g. `"1.0"`), and `codexVersion` is the fixed schema version (`"1.0"`). Syncing them to package versions breaks n8n Cloud verification.
- **Sequential Edit Evaluation:** `process_document_batch` applies edits sequentially. Dependent edits in one batch evaluate against the document state produced by preceding edits.
- **OPC Part Boundaries:** Header, body, footer, footnote, and endnote parts are separated by hard walls. Edits (including context widening) cannot cross OPC part boundaries.
- **Windows Live Word COM (win32):** Windows-only (`pywin32`). STA COM tests intentionally omit `pythoncom.CoUninitialize()` and `app.Quit()` during teardown to avoid Windows RPC Access Violations (`0x800706be`). Live Word COM tests are serialized via `pytest.mark.xdist_group("live_word")` + `--dist loadgroup` so they run sequentially on a single xdist worker.
- **Prerequisites:** System tests and XML checks require `xmllint` (`libxml2-utils`).

## Key Reference Documents

- `AI_CONTEXT.md`: Architectural invariants (Virtual Text contract, boundary whitespace rules, block-level table parsing, OPC part boundaries, XML surgical mode).
- `GEMINI.md`: Tool specification and parameters (`read_docx`, `process_document_batch`, `accept_all_changes`).
- `CONTRIBUTING.md`: Dev environment setup, git hooks (`.githooks`), and PR guidelines.
