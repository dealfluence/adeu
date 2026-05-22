# FILE: langchain/langchain_adeu/__init__.py
"""LangChain integration for Adeu — track-changes for DOCX in the LLM era.

This package exposes the local-only, offline-capable subset of Adeu's
document-editing capabilities as native LangChain tools. Use these tools
with `create_agent` or any LangGraph workflow to build agents that can
read, edit, diff, and sanitize Microsoft Word documents while preserving
the underlying OOXML structure.

Live MS Word integration (Windows-only COM) and Adeu Cloud features
(email, validation) are intentionally excluded from this package. Use the
Adeu MCP server directly for those workflows.

Quick start:

    from langchain.agents import create_agent
    from langchain_adeu import AdeuToolkit

    agent = create_agent(
        model="anthropic:claude-sonnet-4-6",
        tools=AdeuToolkit().get_tools(),
    )
"""

from importlib.metadata import PackageNotFoundError, version

from langchain_adeu.accept_all_changes import (
    AdeuAcceptAllChanges,
    AdeuAcceptAllChangesInput,
)
from langchain_adeu.apply_changes import AdeuApplyChanges, AdeuApplyChangesInput
from langchain_adeu.diff_docx import AdeuDiffDocx, AdeuDiffDocxInput
from langchain_adeu.read_docx import AdeuReadDocx, AdeuReadDocxInput
from langchain_adeu.sanitize_docx import AdeuSanitizeDocx, AdeuSanitizeDocxInput
from langchain_adeu.toolkit import AdeuToolkit, get_tools

try:
    __version__ = version("langchain-adeu")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = [
    "AdeuAcceptAllChanges",
    "AdeuAcceptAllChangesInput",
    "AdeuApplyChanges",
    "AdeuApplyChangesInput",
    "AdeuDiffDocx",
    "AdeuDiffDocxInput",
    "AdeuReadDocx",
    "AdeuReadDocxInput",
    "AdeuSanitizeDocx",
    "AdeuSanitizeDocxInput",
    "AdeuToolkit",
    "__version__",
    "get_tools",
]
