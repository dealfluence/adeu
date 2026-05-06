import asyncio
from unittest.mock import AsyncMock

from fastmcp.tools.tool import ToolResult


def run_async(coro):
    """Simple wrapper to run a coroutine in a new event loop."""
    return asyncio.run(coro)


def get_mock_ctx():
    """Returns a mock FastMCP Context."""
    return AsyncMock()


def extract_content(res):
    """Extracts markdown from a ToolResult or string."""
    if isinstance(res, ToolResult):
        return res.structured_content["markdown"]
    return str(res)
