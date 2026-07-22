# FILE: tests/test_gemini_schema_compat.py
"""Regression tests for issue #37.

Gemini's function-calling API rejects JSON-Schema ``const``. Pydantic v2 emits
``const`` for single-value ``Literal`` types — most importantly the ``type``
discriminator on every ``DocumentChange`` variant exposed through
``process_document_batch.changes``, and ``read_docx``'s ``page`` (``Literal["all"]``).
These tests assert the published tool schemas use ``enum`` instead, so a
Gemini-based client can call the Python server directly.
"""

import asyncio

import pytest
from pydantic import TypeAdapter, ValidationError

from adeu.models import DocumentChange, const_to_enum


def _find_const(node) -> bool:
    """True if ``const`` appears anywhere in the (nested) schema."""
    if isinstance(node, dict):
        if "const" in node:
            return True
        return any(_find_const(v) for v in node.values())
    if isinstance(node, list):
        return any(_find_const(v) for v in node)
    return False


def test_const_to_enum_rewrites_top_level():
    schema = {"const": "modify", "type": "string"}
    const_to_enum(schema)
    assert schema == {"enum": ["modify"], "type": "string"}


def test_const_to_enum_rewrites_nested_union():
    schema = {"anyOf": [{"type": "integer"}, {"const": "all", "type": "string"}]}
    const_to_enum(schema)
    assert schema == {"anyOf": [{"type": "integer"}, {"enum": ["all"], "type": "string"}]}


def test_document_change_discriminators_use_enum_not_const():
    """The canonical union keeps its per-variant $defs, each using enum."""
    schema = TypeAdapter(list[DocumentChange]).json_schema()
    assert "$defs" in schema, "DocumentChange must stay a discriminated union"
    assert set(schema["$defs"]) == {
        "AcceptChange",
        "RejectChange",
        "ReplyComment",
        "ModifyText",
        "InsertTableRow",
        "DeleteTableRow",
    }
    for name, definition in schema["$defs"].items():
        type_field = definition["properties"]["type"]
        assert "const" not in type_field, f"{name}.type still emits const"
        assert "enum" in type_field, f"{name}.type missing enum"
        assert len(type_field["enum"]) == 1
    assert not _find_const(schema)


def test_flat_schema_variant_is_a_single_object_without_const():
    """
    The MCP-boundary alias publishes ONE flat object (hosts that cannot parse
    oneOf/anyOf), still free of `const`. Asserted positively so the union and
    the flattened form can never silently swap places.
    """
    from adeu.models import FlatSchemaDocumentChange

    schema = TypeAdapter(list[FlatSchemaDocumentChange]).json_schema()
    assert "$defs" not in schema
    items = schema["items"]
    assert items["required"] == ["type"]
    type_field = items["properties"]["type"]
    assert "const" not in type_field
    assert set(type_field["enum"]) == {
        "accept",
        "reject",
        "reply",
        "modify",
        "insert_row",
        "delete_row",
    }
    assert not _find_const(schema)


def test_cli_schema_keeps_the_precise_union():
    """The flattening is MCP-only; the CLI's strict schema stays discriminated."""
    from adeu.models import StrictBatchChanges

    schema = TypeAdapter(StrictBatchChanges).json_schema()
    assert "$defs" in schema
    assert "ModifyText" in schema["$defs"]


def test_no_tool_schema_emits_const():
    """No published MCP tool schema may contain `const` (Gemini rejects it)."""
    from adeu.server import mcp

    tools = asyncio.run(mcp.list_tools())
    offenders = [t.name for t in tools if getattr(t, "parameters", None) and _find_const(t.parameters)]
    assert not offenders, f"tools still emit const discriminators: {offenders}"


def test_discriminated_union_still_validates():
    """Rewriting const->enum must not weaken validation."""
    ta = TypeAdapter(list[DocumentChange])
    parsed = ta.validate_python(
        [
            {"type": "accept", "target_id": "Chg:1"},
            {"type": "modify", "target_text": "a", "new_text": "b"},
        ]
    )
    assert [type(p).__name__ for p in parsed] == ["AcceptChange", "ModifyText"]

    with pytest.raises(ValidationError):
        ta.validate_python([{"type": "not_a_real_type"}])
