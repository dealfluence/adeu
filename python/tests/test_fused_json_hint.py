"""
Tests for fused-JSON hint on invalid change type values containing JSON markers.
"""

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from adeu.cli import _extract_schema_failures
from adeu.mcp_components.tools.document import _normalize_changes
from adeu.models import StrictBatchChanges
from adeu.payloads import FUSED_JSON_HINT, has_fused_json_marker


def test_cli_fused_tag_gets_specific_hint(tmp_path):
    """fused JSON element receives fused hint on CLI."""
    batch_file = tmp_path / "changes.json"
    batch_data = [
        {
            "type": "modify}],{comment: foo",
            "target_text": "hello",
            "new_text": "world",
        }
    ]
    batch_file.write_text(json.dumps(batch_data), encoding="utf-8")

    adapter = TypeAdapter(StrictBatchChanges)
    with pytest.raises(ValidationError) as exc_info:
        adapter.validate_python(batch_data)

    failed_pairs, prose = _extract_schema_failures(exc_info.value)
    assert len(failed_pairs) == 1
    idx, reason = failed_pairs[0]
    assert idx == 0
    assert FUSED_JSON_HINT in reason
    assert FUSED_JSON_HINT in prose


def test_mcp_fused_tag_gets_specific_hint():
    """fused JSON element receives fused hint on MCP."""
    changes = [
        {
            "type": '{"type":"modify","target_text":"a"}',
            "target_text": "hello",
            "new_text": "world",
        }
    ]
    valid, rejected = _normalize_changes(changes)
    assert len(valid) == 0
    assert len(rejected) == 1
    assert FUSED_JSON_HINT in rejected[0]
    assert len(rejected.pairs) == 1
    assert FUSED_JSON_HINT in rejected.pairs[0][1]


def test_ordinary_bad_tag_is_unchanged():
    """ordinary typos (modfy) retain standard error without fused hint."""
    changes = [
        {
            "type": "modfy",
            "target_text": "hello",
            "new_text": "world",
        }
    ]
    # MCP check
    valid, rejected = _normalize_changes(changes)
    assert len(valid) == 0
    assert len(rejected) == 1
    assert FUSED_JSON_HINT not in rejected[0]

    # CLI check
    adapter = TypeAdapter(StrictBatchChanges)
    with pytest.raises(ValidationError) as exc_info:
        adapter.validate_python(changes)

    failed_pairs, prose = _extract_schema_failures(exc_info.value)
    assert FUSED_JSON_HINT not in failed_pairs[0][1]
    assert FUSED_JSON_HINT not in prose


def test_hint_triggers_on_each_marker():
    """triggers when type contains {, }, or ":."""
    assert has_fused_json_marker("modify{") is True
    assert has_fused_json_marker("modify}") is True
    assert has_fused_json_marker('"type": "modify"') is True


def test_hint_does_not_trigger_on_punctuation_only():
    """single : does not trigger hint."""
    assert has_fused_json_marker("modfy:") is False
    assert has_fused_json_marker("modfy") is False
    assert has_fused_json_marker("") is False
