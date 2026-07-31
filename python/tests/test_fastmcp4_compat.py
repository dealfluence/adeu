"""FastMCP 4 (MCP SDK v2) compatibility guards.

Each test pins one surface the 3.x -> 4.x migration depended on, so a future
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


def test_read_docx_declares_its_output_schema_and_ui_meta():
    """Regression guard for the MCP Apps contract: the host only forwards
    structured_content to the UI when the tool advertises an output schema."""
    tool = next(t for t in _list_tools_via_client() if t.name == "read_docx")
    assert tool.output_schema is not None
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None)
    assert meta and meta.get("ui", {}).get("resourceUri")


def test_scope_docx_lists_only_docx_tagged_tools(monkeypatch):
    import adeu.server as srv

    monkeypatch.setattr(srv, "requested_scope", "docx")
    names = {t.name for t in _list_tools_via_client()}
    assert names, "scope=docx hid every tool"
    assert "read_docx" in names
    assert "sanitize_docx" not in names, "scope=docx must hide untagged tools"


def test_progress_token_detected_from_lifted_meta_dict():
    """`ctx.request_context.meta` is the raw `_meta` dict in SDK v2
    (fastmcp.server.dependencies._lift_meta), keyed by the wire name."""
    from types import SimpleNamespace

    from adeu.mcp_components.tools.document import _ProgressRelay

    def ctx_with(meta):
        return SimpleNamespace(request_context=SimpleNamespace(meta=meta))

    assert _ProgressRelay._has_progress_token(ctx_with({"progressToken": 7}))
    assert _ProgressRelay._has_progress_token(ctx_with({"progress_token": 7}))
    assert not _ProgressRelay._has_progress_token(ctx_with({}))
    assert not _ProgressRelay._has_progress_token(ctx_with(None))
    assert not _ProgressRelay._has_progress_token(SimpleNamespace(request_context=None))
