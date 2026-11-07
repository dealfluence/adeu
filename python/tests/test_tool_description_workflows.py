"""Optional parameters that change the workflow are taught in the tool DESCRIPTION (AI_CONTEXT.md §7a).

Real MCP clients drop optional-parameter descriptions in transit, so the description is the only channel guaranteed to
reach the model. It must also be true: the batch tool claimed transactional rejection while defaulting to salvage.
Node twin: repro.qa_2026_07_23.client-compat.test.ts.
"""

import asyncio

from adeu.server import mcp

CLIENT_DESCRIPTION_BUDGET = 2048


def _tools():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_diff_tool_teaches_the_one_step_redline():
    desc = _tools()["diff_docx_files"].description or ""
    assert "output_path" in desc and "author_name" in desc
    assert len(desc) < CLIENT_DESCRIPTION_BUDGET


def test_batch_tool_teaches_manual_markup_and_states_the_real_partial_default():
    tool = _tools()["process_document_batch"]
    desc = tool.description or ""
    assert "markup_mode" in desc and "'manual'" in desc
    assert tool.parameters["properties"]["partial"]["default"] is True
    assert "transactionally" not in desc, "the description contradicts the salvage default"
    assert "partial=false" in desc
    assert len(desc) < CLIENT_DESCRIPTION_BUDGET
