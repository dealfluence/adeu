# Task Plan — Migrate `python/` (`adeu` + `adeu-server`) to FastMCP 4

Status legend: `pending` / `in-progress` / `done` / `blocked`

---

## 1. Goal

Upgrade the Python package in `python/` — the `adeu` library and the `adeu-server` MCP
server — from FastMCP `3.4.4` to FastMCP `4.0.0b1` (the newest FastMCP 4 prerelease on
PyPI; latest stable is still `3.4.5`). FastMCP 4 is built on MCP Python SDK v2, which
moves protocol types to `mcp_types` (still re-exported as `mcp.types`), renames every
protocol model field from camelCase to snake_case, replaces `httpx` with `httpx2`
throughout FastMCP's HTTP stack, removes a set of 3.x-deprecated imports/methods, and
raises the `pydantic` floor to `>=2.12`. After this migration `uv run pytest`,
`uv run mypy src`, and `uv run ruff check . && uv run ruff format --check .` must all
pass in `python/`, and the server must expose exactly the same tool/resource surface
(same names, schemas, tags, `meta.ui` payloads, build-tag suffix, `--scope` filtering)
as it does on 3.4.4.

**Non-goals** (do NOT do these):

- Do not touch `node/`, `desktop-extension/`, or `.github/workflows/*` behaviour beyond
  what a dependency bump requires. The Node engine is a separate implementation and the
  dual-engine parity rule in `AGENTS.md` does **not** apply here: this change is a
  Python framework upgrade, not a change to parsing/diffing/redlining algorithms.
- Do not bump the `adeu` project version (`python/pyproject.toml` `version = "1.30.0"`)
  and do not run `scripts/bump.py`. Version bumps are a separate release step.
- Do not adopt new FastMCP 4 features that are not required for the migration
  (no `TasksExtension`, no `FastMCPApp` rewrite, no `cache_ttl`, no tool search, no
  `mount()`/`create_proxy`, no OAuth). The repo uses none of these today.
- Do not change any tool's name, parameter names, defaults, JSON Schema, or output
  schema. Client-visible contracts are frozen.
- Do not remove or weaken the `Annotated[..., "description"]` parameter shorthand or the
  `ctx: Context` type-hint injection style used throughout
  `python/src/adeu/mcp_components/tools/`. Both are confirmed still supported in v4
  (spec `## Parameter Metadata` → "Simple String Descriptions"; `### Legacy Type-Hint
  Injection`).

---

## 2. Context & constraints (grounded findings — trust these, they were verified)

### 2.1 Where FastMCP is actually touched

Full audit of `fastmcp` / `mcp` / `httpx` / `McpError` / `ErrorData` references in
`python/` (excluding `.venv/` and `.mypy_cache/`):

**Production code (5 files):**

| File | Line | Reference | Status under v4 |
| --- | --- | --- | --- |
| `python/src/adeu/server.py` | 7 | `from fastmcp import FastMCP` | OK |
| `python/src/adeu/server.py` | 8 | `from fastmcp.server.providers import FileSystemProvider` | OK (spec `# Filesystem Provider`) |
| `python/src/adeu/server.py` | 9 | `from fastmcp.utilities.types import Image` | OK (spec line ~1436) |
| `python/src/adeu/server.py` | 10 | `from mcp.types import Icon` | OK import; **field access changed** |
| `python/src/adeu/server.py` | 61 | `logging.getLogger("fastmcp.server.context.to_client")` | Harmless even if the logger name moved |
| `python/src/adeu/server.py` | 69 | `Icon(src=..., mimeType="image/png")` | **VERIFY** — see §2.3 |
| `python/src/adeu/server.py` | 88–125 | monkeypatches `provider.list_tools` **and** `mcp.list_tools` | **RISK — Phase 4** |
| `.../mcp_components/tools/document.py` | 10–12 | `from fastmcp import Context`, `fastmcp.exceptions.ToolError`, `from fastmcp.tools import tool` | OK |
| `.../mcp_components/tools/document.py` | **13** | `from fastmcp.tools.tool import ToolResult` | **BREAKS — `fastmcp.tools.tool` removed** |
| `.../mcp_components/tools/live_word.py` | 10–11 | `from fastmcp import Context`, `fastmcp.exceptions.ToolError` | OK |
| `.../mcp_components/tools/live_word.py` | **12** | `from fastmcp.tools.tool import ToolResult` | **BREAKS** |
| `.../mcp_components/tools/sanitize.py` | 6–8 | `from fastmcp import Context`, `ToolError`, `from fastmcp.tools import tool` | OK |
| `.../mcp_components/resources/markdown_ui.py` | 5 | `from fastmcp.resources import resource` | OK (spec `### @resource`) |

**Tests / scripts:**

| File | Line | Reference | Status |
| --- | --- | --- | --- |
| `python/tests/utils.py` | **4** | `from fastmcp.tools.tool import ToolResult` | **BREAKS** |
| `python/tests/test_live_word.py` | **356** | `from fastmcp.tools.tool import ToolResult` (inline) | **BREAKS** |
| `python/tests/test_live_word_dispatch.py` | **37** | `from fastmcp.tools.tool import ToolResult` (inline) | **BREAKS** |
| `python/tests/test_live_word_dispatch.py` | 36 | `from fastmcp.exceptions import ToolError` | OK |
| `python/tests/test_server.py` | 8, 96–116 | `ToolError`; `mcp.call_tool(...)`; `mcp.list_tools()`; `mcp.version` | OK / see Phase 4 |
| `python/tests/test_server.py` | 86–89 | `@patch("fastmcp.server.context.Context.info"/.debug/.warning/.error)` | **VERIFY** path still exists |
| `python/tests/test_cli_bug_repro.py` | 94, 530–533, 550–553, 657–660, 767–770, 813–816, 859–862 | same `fastmcp.server.context.Context.*` patch targets | **VERIFY** |
| `python/tests/test_repro_qa_customer_assessment_2026_07_23.py` | 88–90, 128–146 | `mcp.list_tools()`, `getattr(tool, "inputSchema", ...)`, `from fastmcp import Client` | **camelCase read → Phase 5** |
| `python/tests/test_repro_qa_mcp_2026_07_23_mcp.py` | 57 | `from fastmcp.exceptions import ToolError` | OK |
| `python/tests/test_repro_qa_round3_2026_07_24.py` | 78 | `from fastmcp.exceptions import ToolError` | OK |
| `python/tests/test_repro_qa_report.py` | 267 | `from fastmcp import Context` | OK |
| `python/tests/test_repro_qa_report_v6.py` | 942–962 | asserts `fastmcp` is **never** imported by `adeu extract` | Must stay green |
| `python/scripts/verify_reasoning_order.py` | 18–26 | tries `parameters` / `inputSchema` / `input_schema` / `schema` in that order | **Phase 5** (reorder) |

### 2.2 Things the audit proves are NOT a problem (do not go looking for them)

- **No `McpError` and no `ErrorData` anywhere in `python/`.** The `McpError(code=..., message=...)` keyword-construction fix is a **no-op** for this repo. Do not invent call sites.
- **No `httpx` and no `httpx2` import anywhere in `python/`; no `except httpx.` blocks.** `httpx` only appeared in `uv.lock` transitively via FastMCP 3. FastMCP 4 drops it entirely. No code change needed; `httpx` will simply disappear from the lockfile.
- **No `ctx.elicit()`, `ctx.sample()`, `ctx.sample_step()`, `ctx.list_roots()`, `ctx.set_state()`, `ctx.get_state()`.** The tools use only `ctx.info/debug/warning/error`, which work on every protocol era (an SDK `MCPDeprecationWarning` about the logging capability is expected and benign).
- **No `task=True`, no `TaskConfig`, no `add_extension`.** `TasksExtension` is NOT required. Do **not** add `fastmcp[tasks]`.
- **No middleware, no `on_initialize`, no `Middleware` subclass.**
- **No `as_proxy`, `import_server`, `mount(...)`, `add_tool_transformation`, `remove_tool`, tool `serializer=`, tool `exclude_args=`, `StreamableHttpTransport`, `sampling_handler=`, `FASTMCP_DECORATOR_MODE`.**
- **No templated resources.** The only resource is the static `MARKDOWN_UI_URI` in `markdown_ui.py`, so the new default path-traversal screening for templated resources cannot affect it.
- **No FastAPI and no direct Starlette pin.** `python/uv.lock` already resolves `starlette 1.3.1`, which satisfies the new `starlette>=1.0.1` floor.
- **`langchain/` does not import `fastmcp` or `mcp` at all** (verified). It only depends on `adeu` (editable, `[tool.uv.sources]`).

### 2.3 Dependency facts (verified against PyPI, 2026-07-31)

- `fastmcp` releases in the 4 line: `4.0.0a1`, `4.0.0a2`, `4.0.0b1`. **Use `4.0.0b1`.** Latest stable is `3.4.5`.
- `fastmcp==4.0.0b1` extras: `anthropic`, `apps`, `azure`, `code-mode`, `gemini`, `openai`, `tasks`. **The `apps` extra still exists** — keep it, `markdown_ui.py` ships an MCP-Apps UI resource.
  `fastmcp[apps]==4.0.0b1` → `fastmcp-slim[client,server]==4.0.0b1` + `fastmcp-slim[apps]==4.0.0b1`.
- `fastmcp-slim==4.0.0b1` core requires: `mcp-types>=2.0.0,<3.0.0`, `pydantic[email]>=2.12.0`, `pydantic-settings>=2.0.0`, `platformdirs>=4.0.0`, `python-dotenv>=1.1.0`, `rich>=13.9.4`, `typing-extensions>=4.0.0`.
  `server`/`client` extras add `mcp>=2.0.0,<3.0.0`, `httpx2>=2.5.0`, `starlette>=1.0.1`, `cyclopts>=4.0.0`, `authlib>=1.6.11`, `uvicorn>=0.35`, `openapi-pydantic>=0.5.1`, etc. `apps` extra adds `prefab-ui>=0.18.0`.
- `mcp` **2.0.0 stable is published** on PyPI. Therefore, per the spec, do **NOT** add an `mcp==2.0.0b2` constraint — that would break resolution.
- Only `fastmcp-slim` needs a uv constraint, because uv allows prereleases only for packages named directly and `fastmcp-slim` arrives transitively.

### 2.4 Repo conventions to respect

- Python `>=3.12`; ruff `line-length = 120`, `target-version = "py312"`, lint select `["E","F","I","B","W"]` (so **import order matters** — `I`).
- mypy: `strict = false`, `check_untyped_defs = true`, `ignore_missing_imports = true`. Only `uv run mypy src` is gated (tests are not type-checked).
- pytest `addopts = "-n auto --dist loadgroup"`; live-Word COM tests are pinned to one xdist worker by `tests/conftest.py::pytest_collection_modifyitems`. Debug serially with `uv run pytest -n 0`.
- There are **no** `filterwarnings` settings, so new `FastMCPDeprecationWarning` / `MCPDeprecationWarning` output will NOT fail the suite. Do not add `-W error`.
- Live-Word COM tests will *skip* if Word is unavailable — that is expected, not a failure.
- `python/src/adeu/mcp_components/tools/live_word.py` defines *implementation* functions only; the `@tool`-decorated wrappers for the Windows live-Word path live in `document.py` inside an `if sys.platform == "win32":` block starting at line ~972.
- `python/src/adeu/mcp_components/_response_builders.py` is deliberately **framework-free** (it must not import `fastmcp` — enforced by `test_repro_qa_report_v6.py::test_extract_never_imports_fastmcp`). Never add a `fastmcp` import there.

---

## 3. Phases

### Phase 0 — Baseline capture — `pending`

No file changes. Establish the "before" state so regressions are attributable.

1. From `python/`, run and **record the exact summary lines** (paste into `progress.md`):
   - `uv run pytest -q`  → record `N passed, M skipped, ...`
   - `uv run mypy src`
   - `uv run ruff check . ; uv run ruff format --check .`
2. Record the installed versions: `uv pip show fastmcp fastmcp-slim mcp pydantic httpx starlette` (expected `fastmcp 3.4.4`, `mcp 1.28.1`, `pydantic 2.13.4`, `httpx 0.28.1`, `starlette 1.3.1`).
3. Create `progress.md` at repo root and log the baseline. Update it at the end of every phase.

**Exit criteria:** baseline numbers written down. If the baseline is already red, STOP and report — do not migrate on top of a broken suite.

---

### Phase 1 — Dependency bump and lock — `pending`

**Files:** `python/pyproject.toml`, `python/uv.lock` (regenerated), later `langchain/uv.lock`.

1. In `python/pyproject.toml` `[project].dependencies`:
   - `"fastmcp[apps]>=3.1.1"` → `"fastmcp[apps]==4.0.0b1"`
   - `"pydantic>=2.0.0"` → `"pydantic>=2.12"`
   Leave every other dependency untouched.
2. Add a new top-level table (place it after `[project.scripts]` / before `[build-system]`, or anywhere top-level — but keep it out of `[tool.hatch...]`):

   ```toml
   # FastMCP 4 is a prerelease. uv only allows prereleases for packages named
   # directly, and `fastmcp` pulls `fastmcp-slim` transitively at the same
   # version, so the constraint has to be spelled out here. Do NOT add an
   # `mcp` constraint: mcp 2.0.0 ships stable and a prerelease pin there makes
   # the resolution unsatisfiable.
   [tool.uv]
   constraint-dependencies = ["fastmcp-slim==4.0.0b1"]
   ```

3. Regenerate the lock and sync: `uv lock` then `uv sync --all-extras --dev` (from `python/`).
4. Confirm the resolution with `uv pip show fastmcp fastmcp-slim mcp mcp-types pydantic httpx2 prefab-ui`.
   Expected: `fastmcp 4.0.0b1`, `fastmcp-slim 4.0.0b1`, `mcp 2.0.0`, `mcp-types 2.x`,
   `pydantic >= 2.12`, `httpx2 >= 2.5.0`, `prefab-ui >= 0.18.0`. `httpx` should now be
   **absent** (or present only via an unrelated dep).
5. Sanity-check the CLI reports v4: `uv run fastmcp version` (expect `FastMCP version: 4.0.0b1`).

**Edge cases / gotchas:**
- If `uv lock` refuses the prerelease with something like *"is a pre-release; consider `--prerelease=allow`"* naming **`fastmcp-slim`**, the `[tool.uv] constraint-dependencies` entry is missing or misplaced — fix that rather than reaching for `--prerelease allow` (which would opt the *entire* graph into prereleases).
- If it complains about `fastmcp[apps]` having no `apps` extra, re-check the pin — the extra is confirmed to exist on `4.0.0b1`.
- Do **not** hand-edit `python/uv.lock`.

**Test for this phase (write/run first):** none new; this is a resolver step. But immediately after syncing, run
`uv run python -c "import fastmcp; print(fastmcp.__version__)"` and expect `4.0.0b1`.

**Exit criteria:** `uv sync` succeeds, versions above confirmed. `uv run pytest -q` is expected to FAIL here (collection errors on `fastmcp.tools.tool`) — that is the input to Phase 2.

---

### Phase 2 — Fix the removed `fastmcp.tools.tool` import — `pending`

`fastmcp.tools.tool` is removed in 4.0 (spec `### Moved Imports`): `Tool` / `ToolResult`
now live in `fastmcp.tools`. This is the only hard `ImportError` in the repo.

**Write the test first.** Create `python/tests/test_fastmcp4_compat.py` with:

```python
"""FastMCP 4 (MCP SDK v2) compatibility guards.

Each test pins one surface the 3.x → 4.x migration depended on, so a future
FastMCP bump that moves it again fails here with a clear name instead of
somewhere deep in a tool test.
"""

def test_toolresult_imports_from_fastmcp_tools():
    from fastmcp.tools import ToolResult  # the v4 home
    assert ToolResult is not None


def test_legacy_toolresult_module_is_gone():
    import pytest
    with pytest.raises(ImportError):
        __import__("fastmcp.tools.tool", fromlist=["ToolResult"])


def test_fastmcp_is_v4():
    import fastmcp
    assert fastmcp.__version__.startswith("4."), fastmcp.__version__
```

Then make these five edits (`from fastmcp.tools.tool import ToolResult` →
`from fastmcp.tools import ToolResult`), keeping ruff's `I` import ordering valid
(alphabetically `fastmcp.tools` sorts before `fastmcp.tools.tool`'s old position, so in
`document.py` the two lines `from fastmcp.tools import tool` and the new
`from fastmcp.tools import ToolResult` must be **merged**):

1. `python/src/adeu/mcp_components/tools/document.py` — lines 12–13 become a single line:
   `from fastmcp.tools import ToolResult, tool`
   (ruff's isort sorts `ToolResult` before `tool` — CamelCase first is `force-sort-within-sections` default behaviour; if `ruff check --fix` reorders it, accept ruff's output.)
2. `python/src/adeu/mcp_components/tools/live_word.py` — line 12 → `from fastmcp.tools import ToolResult`
3. `python/tests/utils.py` — line 4 → `from fastmcp.tools import ToolResult`
4. `python/tests/test_live_word.py` — line 356 (inline import inside a test) → `from fastmcp.tools import ToolResult`
5. `python/tests/test_live_word_dispatch.py` — line 37 (inline import) → `from fastmcp.tools import ToolResult`

Also note `python/src/adeu/mcp_components/tools/sanitize.py` line 8 and `document.py`
line 12 already use `from fastmcp.tools import tool`, and `markdown_ui.py` line 5 uses
`from fastmcp.resources import resource` — **both are correct for v4, leave them alone.**

**Exit criteria:** `uv run pytest -q python/tests/test_fastmcp4_compat.py` passes, and
`uv run pytest -q --collect-only` completes with zero collection errors.

---

### Phase 3 — Probe the SDK v2 surfaces the server bootstrap depends on — `pending`

Three surfaces in `server.py` and the test suite are *not* documented well enough to
change blind. **Determine each empirically before editing**, and record the answers in
`progress.md`. Run each probe from `python/`.

**Probe A — `Icon` field name (SDK v2 renamed camelCase → snake_case):**

```
uv run python -c "from mcp.types import Icon; print(sorted(Icon.model_fields)); print(Icon(src='x', mime_type='image/png')); print(Icon(src='x', mimeType='image/png'))"
```

Interpretation:
- If `model_fields` contains `mime_type`, the snake_case spelling is canonical → use `Icon(src=..., mime_type="image/png")` in Phase 4.
- If the `mimeType=` construction raises, snake_case is mandatory.
- If both work, still prefer `mime_type=` (the camelCase bridge is scheduled for removal — spec `## Deprecation Timeline`).

**Probe B — `Image.to_data_uri()` still exists:**

```
uv run python -c "from fastmcp.utilities.types import Image; print(hasattr(Image, 'to_data_uri'))"
```

If `False`, find the replacement in the installed package
(`uv run python -c "from fastmcp.utilities.types import Image; print([m for m in dir(Image) if not m.startswith('_')])"`)
and, if there is no data-URI helper, build the URI inline:
`"data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode()`.
Do not guess an undocumented method name.

**Probe C — the pipeline surfaces used by `server.py` and the tests:**

```
uv run python -c "from fastmcp import FastMCP; m=FastMCP('p'); print('list_tools', hasattr(m,'list_tools')); print('call_tool', hasattr(m,'call_tool')); print('enable', hasattr(m,'enable')); print('disable', hasattr(m,'disable')); print('local_provider', hasattr(m,'local_provider'))"
uv run python -c "from fastmcp.server.transforms import Transform, GetToolNext; print('transforms ok')"
uv run python -c "from fastmcp.server.providers import FileSystemProvider; print(hasattr(FileSystemProvider,'list_tools'))"
uv run python -c "import fastmcp.server.context as c; print(all(hasattr(c.Context,n) for n in ('info','debug','warning','error')))"
```

The last probe validates the `@patch("fastmcp.server.context.Context.info")` targets used
in `test_server.py` and `test_cli_bug_repro.py`. If `fastmcp.server.context.Context` no
longer resolves, locate the new module path
(`uv run python -c "from fastmcp import Context; print(Context.__module__)"`) and update
**every** patch string listed in §2.1 to the new path — there are 25 of them across the
two files.

**Exit criteria:** all three probe answers recorded in `progress.md`. No source edits in
this phase.

---

### Phase 4 — Modernize `python/src/adeu/server.py` — `pending`

This is the riskiest phase. `server.py` currently does two things that are not public
FastMCP API and must be re-grounded on v4:

1. Line 69: `Icon(src=..., mimeType="image/png")` — camelCase construction.
2. Lines 88–125: it monkeypatches **both** `provider.list_tools` (the `FileSystemProvider`
   instance method) and `mcp.list_tools` (the server method), mutating each `Tool`'s
   `.description` in place to append `" [Adeu v{version}+{git_sha}]"`, and — in the
   server-level wrapper only — filtering tools by tag when `requested_scope != "all"`.
   The provider-level wrapper is fully redundant with the server-level one (both append
   the same tag, both guard with `if build_tag not in tool.description`).

**Write the behaviour test FIRST.** Append to `python/tests/test_fastmcp4_compat.py`:

```python
import asyncio


def _list_tools_via_client():
    """List tools the way a real client sees them (through the full transform
    pipeline), not through an internal server method."""
    from fastmcp import Client

    from adeu.server import mcp

    async def _run():
        async with Client(mcp) as client:
            return await client.list_tools()

    return asyncio.run(_run())


def test_every_listed_tool_carries_the_build_tag():
    tools = _list_tools_via_client()
    assert tools, "server published no tools"
    for t in tools:
        assert "[Adeu v" in (t.description or ""), f"{t.name} lost the build tag"


def test_build_tag_is_not_duplicated():
    for t in _list_tools_via_client():
        assert (t.description or "").count("[Adeu v") == 1, t.name


def test_read_docx_declares_its_output_schema_and_ui_meta():
    """Regression guard for the MCP Apps contract: the host only forwards
    structured_content to the UI when the tool advertises an output schema."""
    tool = next(t for t in _list_tools_via_client() if t.name == "read_docx")
    assert tool.output_schema is not None
    assert tool.meta and tool.meta.get("ui", {}).get("resourceUri")


def test_scope_docx_lists_only_docx_tagged_tools(monkeypatch):
    import adeu.server as srv

    monkeypatch.setattr(srv, "requested_scope", "docx")
    names = {t.name for t in _list_tools_via_client()}
    assert names, "scope=docx hid every tool"
    assert "read_docx" in names
```

Note on `tool.meta`: if the SDK v2 `Tool` model exposes the `_meta` wire field under a
different attribute, read it with
`getattr(tool, "meta", None) or getattr(tool, "_meta", None)` — verify with
`uv run python -c "from mcp.types import Tool; print(sorted(Tool.model_fields))"` before
writing the assertion. Do not assert a field name you have not confirmed.

**Then make the edits:**

1. **Icon construction (line 64–71).** Use the spelling Probe A confirmed:

   ```python
   img = Image(path=str(logo_path))
   server_icons.append(Icon(src=img.to_data_uri(), mime_type="image/png"))
   ```

2. **Replace both monkeypatches with one server-level `Transform`.** Spec
   `## Custom Transforms` (`fastmcp.server.transforms.Transform`, `GetToolNext`) shows
   `list_tools(tools)` as a pure sequence→sequence function and `Tool` as a pydantic
   model copied with `model_copy(update={...})`. Delete lines 87–125 entirely (including
   the two `# type: ignore[method-assign]` comments) and add:

   ```python
   from collections.abc import Sequence

   from fastmcp.server.transforms import Transform
   from fastmcp.tools import Tool


   class AdeuBuildTag(Transform):
       """Appends the build stamp to every listed tool description and applies
       the `--scope` tag filter.

       Replaces the FastMCP 3-era monkeypatching of `provider.list_tools` and
       `FastMCP.list_tools`; `Transform` is the supported v4 seam for altering
       how components are presented (spec: Transforms Overview → Custom
       Transforms). `list_tools` is a pure function in v4, so tools are copied
       rather than mutated in place.

       Only `list_tools` is overridden, deliberately: the 3.x code filtered by
       scope on LISTING only, never on `get_tool`, so a scoped-out tool stayed
       callable by name. Overriding `get_tool` here would silently tighten
       that; scope is a presentation hint, not an access control.
       """

       async def list_tools(self, tools: "Sequence[Tool]") -> "Sequence[Tool]":
           # `requested_scope` is read at call time (module global), because
           # main() rewrites it after this module is imported.
           if requested_scope != "all":
               tools = [t for t in tools if requested_scope in (t.tags or set())]
           build_tag = f" [Adeu v{version}+{git_sha}]"
           out: list[Tool] = []
           for t in tools:
               desc = t.description
               if desc and build_tag not in desc:
                   t = t.model_copy(update={"description": desc.strip() + build_tag})
               out.append(t)
           return out
   ```

   and register it on the server:

   ```python
   mcp = FastMCP(
       "Adeu Redlining Service",
       version=version,
       icons=server_icons if server_icons else None,
       providers=[provider],
       transforms=[AdeuBuildTag()],
   )
   ```

   The class must be defined **before** the `FastMCP(...)` call but may reference the
   module globals `requested_scope`, `version`, and `git_sha` (resolved at call time).

3. Leave `_parse_server_args`, the import-time `--scope` argv scan, `logging.basicConfig`,
   the `structlog.configure` block, the `to_client_logger` level tweak, and `main()`
   unchanged.

**Edge cases:**
- `t.tags` may be `None` or a `set`; the `or set()` guard covers both. The v3 code used
  `getattr(tool, "tags", []) or []` — keep equivalent tolerance.
- Tools with `description=None` must pass through untouched (the v3 code guarded with
  `hasattr(tool, "description") and tool.description`).
- `model_copy` on a frozen pydantic model is fine; in-place `t.description = ...`
  may now raise if the v4 `Tool` model is frozen — that is precisely why `model_copy` is used.

**Fallback if `transforms=` does not reach `mcp.list_tools()`:**
`python/tests/test_server.py::test_python_server_version_and_descriptions` (line 114)
asserts the build tag via `asyncio.run(mcp.list_tools())`, and
`test_repro_qa_customer_assessment_2026_07_23.py::_batch_tool_schema` (line 88) also uses
`mcp.list_tools()`. If server-level transforms are applied *above* `FastMCP.list_tools`
(so the internal call returns untagged descriptions) then:
- Keep the `AdeuBuildTag` transform (it is the client-facing contract), **and**
- Rewrite `test_python_server_version_and_descriptions` to list through
  `Client(mcp).list_tools()` (the helper already written above), leaving the
  `mcp.version` assertions intact.
- `_batch_tool_schema` only needs the input schema, not the description, so it can keep
  using `mcp.list_tools()`.
Decide this from the actual test output, not from speculation. If, conversely, the
`Transform` import from `fastmcp.server.transforms` fails (Probe C says otherwise), keep
the existing single **server-level** monkeypatch, delete only the redundant
provider-level one, and record why in `progress.md`.

**Exit criteria:** all new tests in `test_fastmcp4_compat.py` pass; `test_server.py` and
`test_repro_qa_customer_assessment_2026_07_23.py` pass; `server.py` contains no
`# type: ignore[method-assign]`.

---

### Phase 5 — camelCase field reads → snake_case — `pending`

FastMCP 4 bridges camelCase reads but emits a `FastMCPDeprecationWarning` and the shims
are scheduled for removal (spec `### camelCase Field Access`, `## Deprecation Timeline`).
Two places read camelCase off protocol objects:

1. `python/tests/test_repro_qa_customer_assessment_2026_07_23.py` line 90:
   ```python
   schema = getattr(tool, "inputSchema", None) or getattr(tool, "parameters", None)
   ```
   → prefer snake_case first:
   ```python
   schema = getattr(tool, "input_schema", None) or getattr(tool, "parameters", None)
   ```
   (Keep the `parameters` fallback — it is a FastMCP-side alias, not an SDK camelCase name.)
2. `python/scripts/verify_reasoning_order.py` lines 21 and 26: the attribute tuples
   `("parameters", "inputSchema", "input_schema", "schema")` and
   `("parameters", "inputSchema", "input_schema")` → put `input_schema` **before**
   `inputSchema` in both, so the deprecated name is only a last resort. Update the
   line-18 comment to say v4 prefers `input_schema`.

Then grep to prove nothing was missed (from repo root, scoped — exclude `.venv` and
`.mypy_cache`):
`rg -n --glob 'python/**/*.py' 'inputSchema|outputSchema|isError|mimeType|structuredContent|nextCursor|serverInfo|protocolVersion|requestedSchema|readOnlyHint|destructiveHint|idempotentHint|openWorldHint' python`

**Expected surviving hits — these are NOT camelCase field reads; leave them:**
- `document.py` lines 76–80 (a comment) and `markdown_ui.py` line 39-46 (a comment).
- `annotations={"readOnlyHint": True}` / `{"destructiveHint": True}` / `{"openWorldHint": True}`
  in `document.py` (622, 820, 883, 985, 1230, 1345, 1418), `sanitize.py` (22) and
  `markdown_ui.py` (51). These are **dicts validated into `ToolAnnotations` by wire
  alias**, not attribute reads.
  **VERIFY once** that alias-keyed construction still validates under SDK v2:
  ```
  uv run python -c "from mcp.types import ToolAnnotations as A; print(sorted(A.model_fields)); print(A.model_validate({'readOnlyHint': True}))"
  ```
  If alias validation has been turned off, convert these dict keys to snake_case
  (`{"read_only_hint": True}`) **only if** the probe proves the camelCase key is rejected —
  and then confirm the wire JSON still serializes as `readOnlyHint` with
  `uv run python -c "from mcp.types import ToolAnnotations as A; print(A(read_only_hint=True).model_dump(by_alias=True))"`.
  Client compatibility depends on the camelCase wire form.
- `meta={"ui": {"resourceUri": ...}}` and `{"csp": {"connectDomains": ..., "resourceDomains": ...}}`
  are **free-form `_meta` payloads defined by the MCP Apps host**, not SDK model fields.
  Never rename these keys.

**Exit criteria:** the grep shows only the allowlisted hits above; `uv run pytest -q` is
no noisier than before for camelCase warnings.

---

### Phase 6 — Full verification — `pending`

From `python/`, in this order:

1. `uv run ruff check .`
2. `uv run ruff format --check .`
3. `uv run mypy src`
4. `uv run pytest`  (parallel, the project default)
5. `uv run pytest -n 0 -q` — a serial confirmation run. Import-order and module-global
   mutation (`requested_scope`, the `AdeuBuildTag` transform, `adeu.server` being
   imported by several test modules) are exactly the class of bug that only shows up
   under one scheduling. If the parallel and serial results differ, the transform is
   holding state it should read lazily.
6. **camelCase bridge off** (spec checklist item 12) — surfaces any remaining camelCase
   read as a hard `AttributeError`:
   - PowerShell: `$env:FASTMCP_MCP_CAMELCASE_COMPAT="false"; uv run pytest -q; Remove-Item Env:\FASTMCP_MCP_CAMELCASE_COMPAT`
   - This run must also be green. If it fails, the failing attribute is a camelCase read Phase 5 missed — fix it there.
7. `uv run python -c "import adeu.server as s; print(s.mcp.name, s.mcp.version)"` — the
   server module must import cleanly outside a request.
8. Smoke the stdio entry point's pre-transport argument handling:
   `uv run adeu-server --version` and `uv run adeu-server --help` must print and exit
   (this is the QA 2026-07-19 v8 F-06 contract) **without** starting the server.
9. Confirm the cold-start guard still holds: `uv run pytest -q -k extract_never_imports_fastmcp`.

**Expected outcome:** every command above green; `pytest` pass/skip counts equal to the
Phase 0 baseline **plus** the new tests in `test_fastmcp4_compat.py`. Any test that went
from passed → skipped must be explained (live-Word COM skips are legitimate only if they
also skipped at baseline).

---

### Phase 7 — Downstream lock consistency — `pending`

`langchain/` depends on `adeu` as an editable path source, so its lock pins the FastMCP
graph too.

1. From `langchain/`: `uv lock` then `uv sync --group dev --group test`.
2. If `uv lock` rejects the `fastmcp-slim` prerelease reaching it through `adeu`, add the
   same table to `langchain/pyproject.toml`:
   ```toml
   [tool.uv]
   constraint-dependencies = ["fastmcp-slim==4.0.0b1"]
   ```
   (merge into the existing `[tool.uv...]` area near `[tool.uv.sources]` — note
   `[tool.uv.sources]` already exists at line ~60, so add `constraint-dependencies`
   under a `[tool.uv]` table placed **before** `[tool.uv.sources]`).
3. Verify: `uv run ruff check . ; uv run ruff format --check . ; uv run mypy langchain_adeu ; uv run pytest` — all green.
4. From repo root: `node scripts/check_release_consistency.mjs` must still pass (it reads
   `python/pyproject.toml` and `langchain/pyproject.toml` for the **project version**,
   which this change does not touch — confirm it is unaffected).

**Exit criteria:** both `python/` and `langchain/` suites green; release-consistency check green.

---

## 4. Verification (the exact command list)

Run from `python/` unless stated:

| # | Command | Expected |
| --- | --- | --- |
| 1 | `uv lock && uv sync --all-extras --dev` | resolves `fastmcp 4.0.0b1`, `fastmcp-slim 4.0.0b1`, `mcp 2.0.0`, `httpx2` present, `httpx` gone |
| 2 | `uv run fastmcp version` | `FastMCP version: 4.0.0b1` |
| 3 | `uv run ruff check .` | `All checks passed!` |
| 4 | `uv run ruff format --check .` | `N files already formatted` |
| 5 | `uv run mypy src` | `Success: no issues found` (or exactly the baseline's issue count) |
| 6 | `uv run pytest` | baseline pass count + new `test_fastmcp4_compat.py` tests; 0 failed, 0 errors |
| 7 | `uv run pytest -n 0 -q` | same result as #6 |
| 8 | `FASTMCP_MCP_CAMELCASE_COMPAT=false uv run pytest -q` | green |
| 9 | `uv run adeu-server --version` / `--help` | prints and exits 0 |
| 10 | from `langchain/`: `uv lock && uv sync --group dev --group test && uv run ruff check . && uv run mypy langchain_adeu && uv run pytest` | green |
| 11 | from repo root: `node scripts/check_release_consistency.mjs` | green |

Paste the real summary lines for #3, #5, #6, #7, #8, #10 into the final report. Do not
claim success for any command whose output you have not read.

---

## 5. Risks & fallbacks

| Risk | Signal | Fallback |
| --- | --- | --- |
| **uv refuses the prerelease** (`fastmcp-slim ... is a pre-release`) | `uv lock` error naming `fastmcp-slim` | The `[tool.uv] constraint-dependencies` table is missing/misplaced. Fix placement. Only as a documented last resort use `uv lock --prerelease=allow` and say so loudly — it opts the whole graph in. |
| **`transforms=` not reflected by `mcp.list_tools()`** | `test_server.py::test_python_server_version_and_descriptions` fails on a missing `[Adeu v` | Keep the transform (client-facing contract is what matters) and switch that test to list via `Client(mcp)`. See Phase 4 fallback. |
| **`fastmcp.server.transforms` import fails** | Probe C errors | Keep the single server-level `mcp.list_tools` monkeypatch, delete the redundant provider-level one, and record the deviation in `progress.md`. Do not invent an API. |
| **`Icon(mime_type=)` rejected / `to_data_uri` gone** | Probe A / Probe B | Use the spelling that Probe A proves; build the data URI with `base64` inline if the helper is gone. |
| **`fastmcp.server.context.Context` patch path moved** | 25 `@patch(...)` targets raise `AttributeError`/`ModuleNotFoundError` in `test_server.py` and `test_cli_bug_repro.py` | Resolve the real path with `Context.__module__` and update every patch string. |
| **`ToolAnnotations` alias-keyed dicts rejected** | Tool registration raises `ValidationError` at import of `document.py` | Convert dict keys to snake_case **and** verify `model_dump(by_alias=True)` still emits camelCase so the wire form (and Claude Desktop) is unchanged. |
| **`Tool` model became frozen** | `AttributeError`/`ValidationError` on `t.description = ...` | Already mitigated: the new transform uses `model_copy(update=...)`. Never assign in place. |
| **New SDK deprecation warnings flood output** | Noisy pytest output mentioning `logging capability is deprecated as of 2026-07-28 (SEP-2577)` | Benign per spec `## SDK Deprecation Warnings`. Do **not** silence it with a `filterwarnings` entry and do **not** stop using `ctx.info`. |
| **`prefab-ui` (new `apps` extra dep) fails to install on this platform** | `uv sync` build error | Confirm whether the MCP-Apps UI resource is still required; it is (`markdown_ui.py`). Report the install error rather than dropping the `apps` extra. |
| **Live-Word COM tests behave differently** | new failures in `test_live_word*.py` | These are Windows/COM-environment dependent and were pinned to one xdist worker for that reason. Re-run with `uv run pytest -n 0 -k live_word`. A *skip* is acceptable if it also skipped at baseline. |
| **`adeu extract` cold-start regression** | `test_extract_never_imports_fastmcp` fails | Something new imports `fastmcp` from `_response_builders.py` or its import chain. Revert that import; the module is deliberately framework-free. |

---

## 6. Definition of done

Every box must be checked with observed evidence, not assumption:

- [ ] `python/pyproject.toml` pins `fastmcp[apps]==4.0.0b1` and `pydantic>=2.12`, and carries `[tool.uv] constraint-dependencies = ["fastmcp-slim==4.0.0b1"]`.
- [ ] `python/uv.lock` regenerated; `uv pip show` confirms `fastmcp 4.0.0b1`, `fastmcp-slim 4.0.0b1`, `mcp 2.0.0`, `httpx2` present, `httpx` absent.
- [ ] Zero occurrences of `fastmcp.tools.tool`, `fastmcp.resources.resource`, or `fastmcp.prompts.prompt` remain in `python/` (verify with a scoped `rg`).
- [ ] `python/src/adeu/server.py` builds its `Icon` with the field spelling Probe A confirmed, and contains no `# type: ignore[method-assign]` / no `list_tools` monkeypatching (or a recorded, justified fallback).
- [ ] `python/tests/test_fastmcp4_compat.py` exists and covers: `ToolResult` import home, old module gone, `fastmcp.__version__` is 4.x, build tag on every listed tool, no duplicate tag, `read_docx` output schema + `ui` meta, `--scope docx` filtering.
- [ ] `inputSchema` no longer read in `python/tests/` or `python/scripts/` (comments and `annotations=`/`meta=` dict keys excepted, per Phase 5 allowlist).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean in `python/`.
- [ ] `uv run mypy src` clean in `python/` (or exactly the Phase 0 baseline).
- [ ] `uv run pytest` green in `python/`, with pass count ≥ baseline + new tests, and every baseline→skip transition explained.
- [ ] `uv run pytest -n 0 -q` green (same result as the parallel run).
- [ ] `FASTMCP_MCP_CAMELCASE_COMPAT=false uv run pytest -q` green.
- [ ] `uv run adeu-server --version` and `--help` print and exit without starting the transport.
- [ ] `langchain/` relocked and its full suite (ruff, mypy, pytest) green.
- [ ] `node scripts/check_release_consistency.mjs` green from repo root.
- [ ] `progress.md` records: the Phase 0 baseline, all three Phase 3 probe answers, and any fallback taken with its reason.
- [ ] No changes to `node/`, `desktop-extension/`, or the `adeu` project version; git tree contains only the intended files (`git status --short` reviewed).
- [ ] Final report pastes the real output summaries for ruff, mypy, and all three pytest runs.
