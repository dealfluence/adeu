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
1. **Install:** `npm ci` (not `npm install`: the split TypeScript pin below only holds if the tree matches the lockfile)
2. **Build All Packages:** `npm run build` (MUST build before testing; compiles tsup outputs for all packages and verifies MCP bundle)
3. **Run Tests:** `npm run test`
   - Single test: `cd packages/core && npm run test -- -t "test_name"`
4. **Lint:** `npm run lint` (all workspaces), or `cd packages/n8n-nodes-adeu && npm run lint`

### LangChain (`langchain/`)
1. **Lint & Format Check:** `uv run ruff check . && uv run ruff format --check .`
2. **Type Check:** `uv run mypy langchain_adeu`
3. **Run Tests:** `uv run pytest` (single test: `uv run pytest tests/unit_tests/test_apply_changes.py`)

## Operational Quirks & Invariants

- **Build Dependency:** Node workspace requires `npm run build` before `npm test` so compiled dist files exist for `@adeu/mcp-server` and `n8n-nodes-adeu`.
- **Dual-Engine Parity:** Python (`adeu`) and TypeScript (`@adeu/core`) share identical Virtual Text, CriticMarkup, and redlining algorithms. Changes to parsing, diffing, or redlining logic MUST be mirrored in both engines.
- **Monorepo Version Bumping:** Run `python scripts/bump.py [minor|major|patch|X.Y.Z]` from repo root to sync version numbers and lockfiles across subprojects, then verify with `node scripts/check_release_consistency.mjs`.
  - **n8n Codex Exception:** Do NOT bump `nodeVersion` or `codexVersion` in `node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.json` during release bumps. `nodeVersion` mirrors `Adeu.node.ts` runtime `version` (e.g. `"1.0"`), and `codexVersion` is the fixed schema version (`"1.0"`). Syncing them to package versions breaks n8n Cloud verification.
- **Split TypeScript Pin (node/):** The workspace root pins `typescript` to `~6.0.3` and `packages/core` + `packages/mcp-server` pin `^7.0.2` nested. This is deliberate, not drift. TypeScript 7 is the native port: `require("typescript")` exports only `{ version, versionMajorMinor }`, so `ts-api-utils` throws at module load and eslint dies before parsing a file — and npm hoists `ts-api-utils` to the workspace root, which is why the JS-API TypeScript has to live there. `tsc --emitDeclarationOnly` runs the compiler *binary* and tsup compiles with esbuild, so builds keep TS 7. Do NOT collapse this to a single root `^7.0.2` until `typescript-eslint` supports TypeScript 7 (`@typescript-eslint/parser@8.67.0` still declares peer `typescript >=4.8.4 <6.1.0`). See `BUG_cli_test_encoding_and_n8n_lint_toolchain.md`.
- **Test Subprocesses Decode Explicitly (python/):** `subprocess.run(..., text=True)` with no `encoding=` decodes with the host ANSI code page (cp1252 on Windows) while the CLI writes UTF-8 — and the resulting `UnicodeDecodeError` dies in a reader *thread*, silently leaving `.stdout`/`.stderr` as `None`. Use `tests.utils.run_cli()`; `tests/test_cli_encoding.py` fails the build if any test call site skips it.
- **Tool Schemas Publish No `$schema` (node/):** `ProtocolAdapter.transformOutgoingMessage` strips `$schema` from every `tools[].inputSchema`/`outputSchema` on the way out. This is load-bearing, not cosmetic: the MCP SDK converts Zod schemas with a hardcoded draft-07 target (`mapMiniTarget` in `sdk/server/zod-json-schema-compat.js` defaults to `'draft-7'` when no target is passed, and `mcp.js` never passes one), so Zod v4 stamps `$schema: "http://json-schema.org/draft-07/schema#"` on each schema — and Claude Desktop validates with an Ajv built for 2020-12 only, rejecting the tool with *"declares an unsupported dialect"*. Do NOT "fix" this upstream by handing the SDK a plain JSON Schema dict instead of a Zod schema: `normalizeObjectSchema` returns `undefined` for plain dicts, so `mcp.js`'s `if (obj)` guard silently drops `outputSchema` entirely and MCP Apps stops forwarding `structuredContent`. Stripping is lossless only while the emitted schemas avoid keywords whose meaning changed between drafts (tuple-form `items`, `additionalItems`, `definitions`); `spec_2026_07_28.test.ts` fails the build if any appear. This also restores parity with the Python engine, which publishes plain dicts with no `$schema`.
- **Sequential Edit Evaluation:** `process_document_batch` applies edits sequentially. Dependent edits in one batch evaluate against the document state produced by preceding edits.
- **OPC Part Boundaries:** Header, body, footer, footnote, and endnote parts are separated by hard walls. Edits (including context widening) cannot cross OPC part boundaries.
- **Windows Live Word COM (win32):** Windows-only (`pywin32`). STA COM tests intentionally omit `pythoncom.CoUninitialize()` and `app.Quit()` during teardown to avoid Windows RPC Access Violations (`0x800706be`). Live Word COM tests are serialized via `pytest.mark.xdist_group("live_word")` + `--dist loadgroup` so they run sequentially on a single xdist worker.
- **Prerequisites:** System tests and XML checks require `xmllint` (`libxml2-utils`).

## Key Reference Documents

- `AI_CONTEXT.md`: Architectural invariants (Virtual Text contract, boundary whitespace rules, block-level table parsing, OPC part boundaries, XML surgical mode).
- `specs/content-controls/`: **active initiative** — content-control (`w:sdt`) support. Frozen v1 specs, ATDD acceptance examples, and the task board (`TASKS.md`) live there; read its `README.md` before touching any `w:sdt`-related behavior, and claim tasks on the board before starting them.
- `GEMINI.md`: Tool specification and parameters (`read_docx`, `process_document_batch`, `accept_all_changes`).
- `CONTRIBUTING.md`: Dev environment setup, git hooks (`.githooks`), and PR guidelines.
