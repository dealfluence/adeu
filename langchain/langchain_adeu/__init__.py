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
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("langchain-adeu")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]
