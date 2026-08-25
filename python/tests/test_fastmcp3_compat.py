"""FastMCP 3 (MCP SDK v1) compatibility guards.

Each test pins one surface the 4.x -> 3.x downgrade depends on, so a future
FastMCP bump that moves it again fails here with a clear name instead of
somewhere deep in a tool test.
"""


def test_fastmcp_is_v3():
    import fastmcp

    assert fastmcp.__version__.startswith("3."), fastmcp.__version__


def test_mcp_sdk_is_v1():
    """FastMCP 3 rides the MCP Python SDK v1 line (`mcp<2.0`); v2 arrived with
    FastMCP 4 and renames protocol fields to snake_case."""
    from importlib.metadata import version

    assert version("mcp").startswith("1."), version("mcp")


def test_toolresult_imports_from_fastmcp_tools():
    """`fastmcp.tools` re-exports ToolResult in both 3.x and 4.x — the import
    style the tools modules use is the one that survives either line."""
    from fastmcp.tools import Tool, ToolResult, tool

    assert ToolResult is not None
    assert Tool is not None
    assert callable(tool)


def test_legacy_toolresult_module_still_resolves():
    """In 3.x `fastmcp.tools.tool` is a sys.modules shim onto
    `fastmcp.tools.base`; in 4.x that module is gone. Nothing in adeu imports
    the legacy path — this only nails down which layout we are running on."""
    from fastmcp.tools import ToolResult

    legacy = __import__("fastmcp.tools.tool", fromlist=["ToolResult"])
    assert legacy.ToolResult is ToolResult


def test_server_icon_uses_camelcase_mime_type():
    """`mcp.types.Icon` (SDK v1) declares `mimeType` and allows extra fields, so
    the v4 spelling `Icon(mime_type=...)` is silently accepted and leaves
    `mimeType` None — a wire regression with no error. Pin the real field."""
    from adeu.server import server_icons

    assert server_icons, "server icon failed to load from assets/logo.png"
    for icon in server_icons:
        assert icon.mimeType == "image/png", f"icon mimeType is {icon.mimeType!r}"
        assert icon.src.startswith("data:image/png"), icon.src[:32]


def _list_tools_via_client():
    """List tools the way a real client sees them (through the full transform
    pipeline), not through an internal server method."""
    import asyncio

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


def test_internal_list_tools_also_carries_the_build_tag():
    """`FastMCP.list_tools()` must see the server-level Transform too: several
    tests (e.g. test_server.py) read descriptions through the internal method
    rather than a Client."""
    import asyncio

    from adeu.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert tools, "internal list_tools returned no tools"
    for t in tools:
        assert "[Adeu v" in (t.description or ""), f"{t.name} lost the build tag"


def test_read_docx_declares_its_output_schema_and_ui_meta():
    """Regression guard for the MCP Apps contract: the host only forwards
    structuredContent to the UI when the tool advertises an output schema.
    Client-side tools are `mcp.types.Tool` (SDK v1), so the field is the
    camelCase `outputSchema`, not FastMCP's server-side `output_schema`."""
    tool = next(t for t in _list_tools_via_client() if t.name == "read_docx")
    assert tool.outputSchema is not None
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None)
    assert meta and meta.get("ui", {}).get("resourceUri")


def test_scope_docx_lists_only_docx_tagged_tools(monkeypatch):
    import adeu.server as srv

    monkeypatch.setattr(srv, "requested_scope", "docx")
    names = {t.name for t in _list_tools_via_client()}
    assert names, "scope=docx hid every tool"
    assert "read_docx" in names
    assert "sanitize_docx" not in names, "scope=docx must hide untagged tools"


def test_progress_token_detected_from_sdk_v1_meta_object():
    """`ctx.request_context.meta` is a `RequestParams.Meta` model in SDK v1,
    carrying a single camelCase `progressToken`. `_has_progress_token` also
    tolerates the SDK v2 dict form, and that tolerance is kept deliberately."""
    from types import SimpleNamespace

    from mcp.types import RequestParams

    from adeu.mcp_components.tools.document import _ProgressRelay

    def ctx_with(meta):
        return SimpleNamespace(request_context=SimpleNamespace(meta=meta))

    assert _ProgressRelay._has_progress_token(ctx_with(RequestParams.Meta(progressToken=7)))
    assert not _ProgressRelay._has_progress_token(ctx_with(RequestParams.Meta()))
    # Dict form (SDK v2 / defensive path) must keep working.
    assert _ProgressRelay._has_progress_token(ctx_with({"progressToken": 7}))
    assert _ProgressRelay._has_progress_token(ctx_with({"progress_token": 7}))
    assert not _ProgressRelay._has_progress_token(ctx_with({}))
    assert not _ProgressRelay._has_progress_token(ctx_with(None))
    assert not _ProgressRelay._has_progress_token(SimpleNamespace(request_context=None))
