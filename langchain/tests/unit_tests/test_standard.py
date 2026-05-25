# FILE: langchain/tests/unit_tests/test_standard.py
"""Standard `langchain-tests` suite for each tool in langchain-adeu.

Subclassing `ToolsUnitTests` runs LangChain's canonical conformance
checks: name presence, args_schema presence, schema/example alignment,
init correctness. If any subclass fails its `test_*` methods, the tool
deviates from the standard LangChain interface and won't compose well
with other tools or agents.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool
from langchain_tests.unit_tests import ToolsUnitTests

from langchain_adeu import (
    AdeuAcceptAllChanges,
    AdeuApplyChanges,
    AdeuDiffDocx,
    AdeuReadDocx,
    AdeuSanitizeDocx,
)


class TestAdeuReadDocxStandard(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuReadDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:

        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:

        return {
            "file_path": "/tmp/example.docx",
            "clean_view": False,
            "mode": "full",
            "page": 1,
        }


class TestAdeuDiffDocxStandard(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuDiffDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        return {
            "original_path": "/tmp/original.docx",
            "modified_path": "/tmp/modified.docx",
            "compare_clean": True,
        }


class TestAdeuAcceptAllChangesStandard(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuAcceptAllChanges

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:

        return {
            "file_path": "/tmp/draft.docx",
            "output_path": "/tmp/draft_clean.docx",
        }


class TestAdeuSanitizeDocxStandard(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuSanitizeDocx

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:

        return {
            "file_path": "/tmp/draft.docx",
            "output_path": "/tmp/draft_sanitized.docx",
        }


class TestAdeuApplyChangesStandard(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return AdeuApplyChanges

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:

        return {
            "file_path": "/tmp/draft.docx",
            "author_name": "AI Reviewer",
            "changes": [
                {
                    "type": "modify",
                    "target_text": "old phrase",
                    "new_text": "new phrase",
                    "comment": "Aligning terminology.",
                }
            ],
            "output_path": "/tmp/draft_processed.docx",
        }
