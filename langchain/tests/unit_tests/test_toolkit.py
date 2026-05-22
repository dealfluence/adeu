# FILE: langchain/tests/unit_tests/test_toolkit.py
"""Unit tests for `AdeuToolkit` and the `get_tools` convenience function.

The toolkit is a thin wrapper around tool constructors, so the tests are
behavioral rather than schema-level: we verify that the toolkit exposes
exactly the tools we expect, that each call returns fresh instances
(per LangChain toolkit convention), and that the convenience function
matches the class-based path.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, BaseToolkit

from langchain_adeu import (
    AdeuAcceptAllChanges,
    AdeuApplyChanges,
    AdeuDiffDocx,
    AdeuReadDocx,
    AdeuSanitizeDocx,
    AdeuToolkit,
    get_tools,
)

EXPECTED_TOOL_CLASSES = {
    AdeuReadDocx,
    AdeuApplyChanges,
    AdeuDiffDocx,
    AdeuAcceptAllChanges,
    AdeuSanitizeDocx,
}

EXPECTED_TOOL_NAMES = {
    "adeu_read_docx",
    "adeu_apply_changes",
    "adeu_diff_docx",
    "adeu_accept_all_changes",
    "adeu_sanitize_docx",
}


class TestAdeuToolkitContract:
    def test_subclasses_base_toolkit(self) -> None:

        assert issubclass(AdeuToolkit, BaseToolkit)

    def test_constructs_without_arguments(self) -> None:

        toolkit = AdeuToolkit()
        assert isinstance(toolkit, BaseToolkit)

    def test_get_tools_returns_list_of_base_tools(self) -> None:
        tools = AdeuToolkit().get_tools()
        assert isinstance(tools, list)
        assert all(isinstance(t, BaseTool) for t in tools)

    def test_exposes_exactly_the_expected_tool_classes(self) -> None:

        tools = AdeuToolkit().get_tools()
        got_classes = {type(t) for t in tools}
        assert got_classes == EXPECTED_TOOL_CLASSES

    def test_tool_names_are_stable(self) -> None:

        tools = AdeuToolkit().get_tools()
        got_names = {t.name for t in tools}
        assert got_names == EXPECTED_TOOL_NAMES

    def test_tool_names_are_unique(self) -> None:

        tools = AdeuToolkit().get_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))


class TestAdeuToolkitInstancing:
    def test_get_tools_returns_fresh_instances_each_call(self) -> None:

        first = AdeuToolkit().get_tools()
        second = AdeuToolkit().get_tools()

        first_by_name = {t.name: t for t in first}
        second_by_name = {t.name: t for t in second}

        # Same set of tool names, but each name's instance is a distinct
        # object.
        assert first_by_name.keys() == second_by_name.keys()
        for name in first_by_name:
            assert first_by_name[name] is not second_by_name[name], (
                f"Toolkit returned the SAME instance of {name!r} across "
                f"two calls; expected fresh instances per call."
            )

    def test_same_toolkit_instance_also_returns_fresh_tools(self) -> None:

        toolkit = AdeuToolkit()
        first = toolkit.get_tools()
        second = toolkit.get_tools()
        for a, b in zip(first, second, strict=True):
            assert a is not b


class TestGetToolsConvenienceFunction:
    def test_returns_same_tool_set_as_toolkit(self) -> None:

        toolkit_tools = AdeuToolkit().get_tools()
        function_tools = get_tools()
        assert {type(t) for t in toolkit_tools} == {type(t) for t in function_tools}
        assert {t.name for t in toolkit_tools} == {t.name for t in function_tools}
