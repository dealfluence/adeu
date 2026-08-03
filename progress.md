# Migration Progress — FastMCP 4

## Phase 0 — Baseline Capture

- **pytest**: 1060 passed, 5 skipped in 32.73s (post-migration adds 8 new tests in `test_fastmcp4_compat.py` for total 1068 passed, 5 skipped)
- **mypy**: Success: no issues found in 31 source files
- **ruff**: All checks passed! 160 files already formatted
- **Pre-migration Versions**:
  - fastmcp: 3.4.4
  - fastmcp-slim: 3.4.4
  - mcp: 1.28.1
  - pydantic: 2.13.4
  - httpx: 0.28.1
  - starlette: 1.3.1

## Phase 1 — Dependency Bump and Lock

- Updated `python/pyproject.toml`: `fastmcp[apps]==4.0.0b1`, `pydantic>=2.12`, `[tool.uv] constraint-dependencies = ["fastmcp-slim==4.0.0b1"]`.
- `uv lock` & `uv sync --all-extras --dev` in `python/`.
- **Post-migration Versions**:
  - fastmcp: 4.0.0b1
  - fastmcp-slim: 4.0.0b1
  - mcp: 2.0.0
  - mcp-types: 2.0.0
  - httpx2: 2.9.1
  - httpx: (removed)
  - pydantic: 2.13.4
  - prefab-ui: 0.20.2
- `uv run fastmcp version`: confirmed FastMCP version 4.0.0b1, MCP version 2.0.0.

## Phase 2 — Fix Removed Imports

- Added `python/tests/test_fastmcp4_compat.py` with compatibility guards.
- Updated `from fastmcp.tools.tool import ToolResult` -> `from fastmcp.tools import ToolResult` in 5 files (`document.py`, `live_word.py`, `utils.py`, `test_live_word.py`, `test_live_word_dispatch.py`).
- `uv run pytest --collect-only`: 1068 tests collected with 0 errors.

## Phase 3 — Probe Answers

- **Probe A (`Icon`)**: `mcp.types.Icon` fields are `['mime_type', 'sizes', 'src', 'theme']`. `Icon(src='...', mime_type='image/png')` is canonical.
- **Probe B (`Image.to_data_uri`)**: `hasattr(Image, 'to_data_uri')` returned `True`.
- **Probe C**:
  - `FastMCP` has `list_tools`, `call_tool`, `enable`, `disable`, `local_provider` (all `True`).
  - `from fastmcp.server.transforms import Transform, GetToolNext` imported successfully.
  - `FileSystemProvider` has `list_tools` (`True`).
  - `fastmcp.server.context.Context` has `info`, `debug`, `warning`, `error` (`True` — all patch targets intact).
  - `mcp.types.Tool` model fields include `meta`, `output_schema`, `input_schema`, `description`, `name`, `annotations`.

## Phase 4 — Modernize `python/src/adeu/server.py`

- Replaced monkeypatching of `provider.list_tools` and `mcp.list_tools` with server-level `AdeuBuildTag(Transform)` registered via `transforms=[AdeuBuildTag()]`.
- Updated `Icon(src=..., mime_type="image/png")` to use snake_case `mime_type`.
- Added client-facing behavior tests in `test_fastmcp4_compat.py`:
  - `test_every_listed_tool_carries_the_build_tag`
  - `test_build_tag_is_not_duplicated`
  - `test_read_docx_declares_its_output_schema_and_ui_meta`
  - `test_scope_docx_lists_only_docx_tagged_tools` (verifies `read_docx` in names and `sanitize_docx` not in names)

## Phase 5 — camelCase Field Reads -> snake_case

- Updated `python/tests/test_repro_qa_customer_assessment_2026_07_23.py` line 90 to read `input_schema` first.
- Updated `python/scripts/verify_reasoning_order.py` to probe `input_schema` before `inputSchema`.
- Confirmed `mcp.types.ToolAnnotations.model_validate({'readOnlyHint': True})` succeeds.

## Phase 6 — Full Verification (`python/`)

- `uv run ruff check .` && `uv run ruff format --check .`: Passed cleanly (161 files).
- `uv run mypy src`: Success: no issues found in 31 source files.
- `uv run pytest` (parallel): 1068 passed, 5 skipped, 1 warning (SEP-2577) in 29.80s.
- `uv run pytest -n 0 -q` (serial): 1068 passed, 5 skipped, 1 warning in 102.51s.
- `FASTMCP_MCP_CAMELCASE_COMPAT=false uv run pytest -q`: 1068 passed, 5 skipped.
- `uv run adeu-server --version`: printed `adeu-server 1.30.0+d8b3240` and exited 0.
- `uv run adeu-server --help`: printed usage and exited 0.
- `uv run pytest -q -k extract_never_imports_fastmcp`: 1 passed.

## Phase 7 — Downstream Lock Consistency

- Updated `langchain/pyproject.toml` with `[tool.uv] constraint-dependencies = ["fastmcp-slim==4.0.0b1"]`.
- `uv lock` & `uv sync --group dev --group test` in `langchain/` resolved cleanly.
- `uv run ruff check . && uv run ruff format --check . && uv run mypy langchain_adeu && uv run pytest` in `langchain/` passed cleanly (171 passed).
- `node scripts/check_release_consistency.mjs` passed cleanly from repo root.

## Deviations from Plan

- `python/src/adeu/mcp_components/tools/document.py::_ProgressRelay._has_progress_token`
  rewritten. Reason: In SDK v2 `ctx.request_context.meta` is typed `dict[str, Any] | None` (the raw `_meta` dict lifted by `fastmcp/server/dependencies._lift_meta`). The old attribute read `rc.meta.progressToken` raises `AttributeError` on a `dict`. The new implementation checks `isinstance(rc.meta, dict)` and reads `rc.meta.get("progressToken")` / `rc.meta.get("progress_token")`. Unit tested in `test_progress_token_detected_from_lifted_meta_dict`.
