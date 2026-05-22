# FILE: langchain/langchain_adeu/toolkit.py
"""Convenience bundle of all langchain-adeu tools.

`AdeuToolkit` is the standard LangChain way to expose a related set of
tools as a single object. It inherits from `BaseToolkit`, which gives
you a uniform `.get_tools()` method that composes cleanly with
`create_agent`:

    from langchain.agents import create_agent
    from langchain_adeu import AdeuToolkit

    agent = create_agent(
        model="anthropic:claude-sonnet-4-6",
        tools=AdeuToolkit().get_tools(),
    )

The toolkit is intentionally configuration-free: every tool takes its
parameters per-invocation, so there's nothing to configure at toolkit
construction time. This may change in a future release if we add tools
that share configuration (e.g., a default `author_name`).
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, BaseToolkit

from langchain_adeu.accept_all_changes import AdeuAcceptAllChanges
from langchain_adeu.apply_changes import AdeuApplyChanges
from langchain_adeu.diff_docx import AdeuDiffDocx
from langchain_adeu.read_docx import AdeuReadDocx
from langchain_adeu.sanitize_docx import AdeuSanitizeDocx


class AdeuToolkit(BaseToolkit):
    """Bundle of all local, offline-capable Adeu tools.

    Includes:
      - AdeuReadDocx        — read .docx → Markdown with CriticMarkup
      - AdeuApplyChanges    — apply tracked-change batch edits
      - AdeuDiffDocx        — word-level diff between two .docx files
      - AdeuAcceptAllChanges — finalize all tracked changes
      - AdeuSanitizeDocx    — strip metadata + audit report

    Excluded (use the Adeu MCP server directly for these):
      - Live MS Word (Windows COM)
      - Adeu Cloud (email, multi-doc validation)
    """

    def get_tools(self) -> list[BaseTool]:
        """Instantiate one of each tool. Returns a fresh list per call.

        Each call instantiates new tool objects rather than caching a
        shared list. This matches the behavior of LangChain's other
        first-party toolkits (e.g. `GmailToolkit`) and lets callers
        modify individual tool attributes (e.g. `verbose=True`) without
        affecting other callers.
        """
        return [
            AdeuReadDocx(),
            AdeuApplyChanges(),
            AdeuDiffDocx(),
            AdeuAcceptAllChanges(),
            AdeuSanitizeDocx(),
        ]


def get_tools() -> list[BaseTool]:
    """Module-level convenience: equivalent to `AdeuToolkit().get_tools()`.

    Provided because some users prefer importing a function over
    instantiating a class for the trivial no-config case:

        from langchain_adeu import get_tools
        agent = create_agent(model="...", tools=get_tools())

    Functionally identical to `AdeuToolkit().get_tools()`.
    """
    return AdeuToolkit().get_tools()


__all__ = ["AdeuToolkit", "get_tools"]
