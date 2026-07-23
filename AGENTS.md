# Adeu Agent Instructions

Adeu is a DOCX ↔ LLM translation engine and redlining Virtual DOM. It maintains parallel Python and Node.js implementations plus a LangChain integration.

## Repository Structure & Packages

- `python/`: Core Python engine (`adeu`), FastMCP server (`adeu-server`), and CLI (`adeu`). Managed via `uv` (requires Python ≥ 3.12).
- `node/`: Node.js workspace (requires Node ≥ 22.0.0). Contains:
  - `node/packages/core`: `@adeu/core` (TypeScript SDK)
  - `node/packages/mcp-server`: `@adeu/mcp-server` (MCP server binary)
  - `node/packages/n8n-nodes-adeu`: `n8n-nodes-adeu` (n8n community node)
- `langchain/`: `langchain-adeu` integration package. Editable-links to `python/` via `tool.uv.sources`.
- `desktop-extension/`: Claude Desktop extension packaging (`.mcpb`).
- `scripts/`: Monorepo automation (`bump.py`, `check_release_consistency.mjs`).

## Commands & Verification Workflow

Run commands from the respective package directory:

### Python (`python/`)
1. **Lint & Format Check:** `uv run ruff check . && uv run ruff format --check .`
2. **Type Check:** `uv run mypy src`
3. **Run Tests:** `uv run pytest` (single test: `uv run pytest tests/test_engine.py -k "test_name"`)

### Node.js (`node/`)
1. **Build All Packages:** `npm run build` (must build before testing dependent packages)
2. **Run Tests:** `npm run test` (single test: `cd packages/core && npm run test -- -t "test_name"`)
3. **Lint (n8n package):** `cd packages/n8n-nodes-adeu && npm run lint`

### LangChain (`langchain/`)
1. **Lint & Format Check:** `uv run ruff check . && uv run ruff format --check .`
2. **Type Check:** `uv run mypy langchain_adeu`
3. **Run Tests:** `uv run pytest`

## Operational Quirks & Invariants

- **Command Order:** Node workspace requires `npm run build` before `npm test` so built dist files exist for `@adeu/mcp-server` and `n8n-nodes-adeu`.
- **Dual-Engine Parity:** Python and TypeScript backends share identical Virtual Text and CriticMarkup behavior. Changes to redlining algorithms must be mirrored in both engines.
- **Monorepo Version Bumping:** Run `python scripts/bump.py [minor|major|patch|X.Y.Z]` from repo root to update all subprojects simultaneously, then verify with `node scripts/check_release_consistency.mjs`.
  - **Exception:** Do NOT bump `nodeVersion` or `codexVersion` in `node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.json` during version updates (breaks n8n Cloud verification).
- **Windows Live Word COM:** Windows-only (`pywin32`). STA COM tests intentionally omit `pythoncom.CoUninitialize()` and `app.Quit()` during teardown to avoid RPC crashes (`0x800706be`).
- **Prerequisites:** System tests and XML checks require `xmllint` (`libxml2-utils`).

## Key Reference Documents

- `AI_CONTEXT.md`: Architectural invariants (Virtual Text contract, block-level table parsing, OPC part boundaries, XML surgical mode).
- `GEMINI.md`: Tool specification and parameters (`read_docx`, `process_document_batch`, `accept_all_changes`).
- `CONTRIBUTING.md`: Dev environment setup, git hooks (`.githooks`), and PR guidelines.
