import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adeu.mcp_components.tools.sanitize import sanitize_docx


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_sanitize_docx_does_not_block_event_loop():
    """
    Bug #13 & #14: sanitize_docx is a heavy synchronous function that blocks the MCP
    event loop. It must be dispatched to a worker thread via asyncio.to_thread().
    """

    # Mock the synchronous sanitize core to sleep for 0.5 seconds
    def mock_sanitize_sync(*args, **kwargs):
        time.sleep(0.5)
        mock_result = MagicMock()
        mock_result.output_path = "out.docx"
        mock_result.status = "Success"
        mock_result.tracked_changes_found = 0
        mock_result.tracked_changes_accepted = 0
        mock_result.comments_removed = 0
        mock_result.comments_kept = 0
        mock_result.metadata_stripped = []
        mock_result.warnings = []
        mock_result.report_text = ""
        return mock_result

    ctx = MagicMock()
    ctx.info = AsyncMock()

    with (
        patch("adeu.mcp_components.tools.sanitize._sanitize", side_effect=mock_sanitize_sync, create=True),
        patch("adeu.sanitize.core.sanitize_docx", side_effect=mock_sanitize_sync),
        patch("pathlib.Path.exists", return_value=True),
    ):
        start = time.perf_counter()

        loop_ticks = []

        async def tick():
            for _ in range(5):
                loop_ticks.append(time.perf_counter())
                await asyncio.sleep(0.1)

        # Run concurrently
        await asyncio.gather(sanitize_docx("dummy.docx", ctx=ctx), tick())

        first_tick_delay = loop_ticks[0] - start
        assert first_tick_delay < 0.2, f"Event loop was blocked! First tick took {first_tick_delay}s"
