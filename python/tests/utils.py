import asyncio
from unittest.mock import AsyncMock

from fastmcp.tools import ToolResult


def run_async(coro):
    """Simple wrapper to run a coroutine in a new event loop."""
    return asyncio.run(coro)


def get_mock_ctx():
    """Returns a mock FastMCP Context."""
    return AsyncMock()


def extract_content(res):
    """Extracts markdown from a ToolResult or string."""
    if isinstance(res, ToolResult) and res.structured_content is not None:
        return res.structured_content["markdown"]
    return str(res)


def approx_tokens(s: str) -> int:
    return len(s) // 4
