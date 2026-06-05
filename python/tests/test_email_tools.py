# FILE: python/tests/test_email_tools.py
import base64
import asyncio
import json
import urllib.error
from email.message import Message
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from adeu.mcp_components.tools.email import (
    _format_backend_error,
    list_available_mailboxes,
    resolve_email_id,
    search_and_fetch_emails,
    StaleShortIdError,
)


def _get_tool_text(res) -> str:
    """Helper to extract raw text content from an MCP ToolResult."""
    if (
        hasattr(res, "content")
        and isinstance(res.content, list)
        and len(res.content) > 0
    ):
        return res.content[0].text
    return str(res)


def test_finding_6_error_formatting_helpers():
    """Verify that backend error messages are correctly mapped to developer-friendly and agent-actionable instructions."""
    # Test 1: Email not found message mapping
    err_body_1 = json.dumps({"detail": "Email not found."})
    formatted_1 = _format_backend_error(404, err_body_1)
    assert "Cloud search failed (HTTP 404):" in formatted_1
    assert "The email ID was not found" in formatted_1
    assert "evicted from the local cache" in formatted_1

    # Test 2: Mailbox not found message mapping
    err_body_2 = json.dumps({"detail": "Mailbox 'sales@company.com' not found."})
    formatted_2 = _format_backend_error(404, err_body_2)
    assert "Cloud search failed (HTTP 404):" in formatted_2
    assert "The mailbox 'sales@company.com' is not connected" in formatted_2
    assert "Call list_available_mailboxes to see valid mailbox addresses" in formatted_2

    # Test 3: Unmapped generic details bypass transformation gracefully
    err_body_generic = json.dumps({"detail": "Some unexpected internal error."})
    formatted_generic = _format_backend_error(500, err_body_generic)
    assert (
        "Cloud search failed (HTTP 500): Some unexpected internal error."
        in formatted_generic
    )


@patch("adeu.mcp_components.tools.email.load_id_cache")
def test_finding_6_stale_short_id_resolution(mock_load_cache):
    """Verify that looking up a stale or missing short ID triggers the clear StaleShortIdError."""
    # Mock an empty cache
    mock_load_cache.return_value = {}

    with pytest.raises(StaleShortIdError) as exc_info:
        resolve_email_id("msg_abc123")

    error_msg = str(exc_info.value)
    assert "Short ID 'msg_abc123' is not in the local cache" in error_msg
    assert "evicted" in error_msg
    assert "Re-run search_and_fetch_emails" in error_msg


@patch("urllib.request.urlopen")
def test_finding_2_list_mailboxes_parity_and_fallback(mock_urlopen):
    """Verify that mailboxes list with Node-parity headers, fallback labels, and sorting."""
    ctx = AsyncMock()

    mock_response_data = [
        {
            "email_address": "secondary@adeu.ai",
            "display_name": "Secondary Mailbox",
            "auto_process_enabled": False,
            "write_back_preference": "INTERNAL",
        },
        {
            "email_address": "primary@adeu.ai",
            "display_name": None,  # Will test the fallback name "Personal Mailbox"
            "auto_process_enabled": True,
            "write_back_preference": "DRAFT",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    result = asyncio.run(list_available_mailboxes(ctx=ctx, api_key="test_api_key"))

    assert "### Connected Mailboxes" in result
    assert "Below is the list of connected mailboxes you have access to." in result
    assert "Use the `email_address` as the `mailbox_address` parameter" in result
    assert "**Personal Mailbox**" in result

    idx_primary = result.find("primary@adeu.ai")
    idx_secondary = result.find("secondary@adeu.ai")
    assert idx_primary != -1
    assert idx_secondary != -1
    assert (
        idx_primary < idx_secondary
    ), "Mailboxes should be sorted alphabetically by email_address"

    assert "- **Email Address**:" in result
    assert "- **Auto-Processing**:" in result
    assert "- **Write-Back Mode**:" in result


@patch("urllib.request.urlopen")
def test_finding_6_tool_boundary_error_handling_bogus_mailbox(mock_urlopen):
    """Verify search_and_fetch_emails propagates mapped mailbox 404 errors as a ToolError."""
    ctx = AsyncMock()

    mock_fp = MagicMock()
    mock_fp.read.return_value = json.dumps(
        {"detail": "Mailbox 'bogus@nowhere.invalid' not found."}
    ).encode("utf-8")

    empty_headers = Message()
    http_error = urllib.error.HTTPError(
        url="http://mock-endpoint",
        code=404,
        msg="Not Found",
        hdrs=empty_headers,
        fp=mock_fp,
    )
    mock_urlopen.side_effect = http_error

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            search_and_fetch_emails(
                ctx=ctx, mailbox_address="bogus@nowhere.invalid", api_key="test_api_key"
            )
        )

    error_msg = str(exc_info.value)
    assert "Cloud search failed (HTTP 404):" in error_msg
    assert "The mailbox 'bogus@nowhere.invalid' is not connected" in error_msg
    assert "Call list_available_mailboxes" in error_msg


@patch("urllib.request.urlopen")
def test_finding_6_tool_boundary_error_handling_missing_email(mock_urlopen):
    """Verify search_and_fetch_emails propagates mapped email 404 errors as a ToolError."""
    ctx = AsyncMock()

    mock_fp = MagicMock()
    mock_fp.read.return_value = json.dumps({"detail": "Email not found."}).encode(
        "utf-8"
    )

    empty_headers = Message()
    http_error = urllib.error.HTTPError(
        url="http://mock-endpoint",
        code=404,
        msg="Not Found",
        hdrs=empty_headers,
        fp=mock_fp,
    )
    mock_urlopen.side_effect = http_error

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            search_and_fetch_emails(
                ctx=ctx,
                email_id="adeu_99999",  # Direct adeu ID passes through cache check
                api_key="test_api_key",
            )
        )

    error_msg = str(exc_info.value)
    assert "Cloud search failed (HTTP 404):" in error_msg
    assert "The email ID was not found" in error_msg
    assert "evicted from the local cache" in error_msg


@patch("urllib.request.urlopen")
def test_finding_6_tool_boundary_error_handling_timeout(mock_urlopen):
    """Verify search_and_fetch_emails converts native socket/HTTP timeouts to detailed ToolErrors."""
    ctx = AsyncMock()

    # Simulate connection timeout error
    mock_urlopen.side_effect = TimeoutError("Connection timed out")

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            search_and_fetch_emails(
                ctx=ctx, mailbox_address="sales@company.com", api_key="test_api_key"
            )
        )

    error_msg = str(exc_info.value)
    assert "Email search timed out after 45s." in error_msg
    assert "The mail provider (Outlook/Gmail) may be slow" in error_msg


@patch("urllib.request.urlopen")
def test_findings_3_and_11_and_9_formatting_parity(mock_urlopen):
    """Verify preview pagination, auto-escalation banners, and downstream tool suggestions."""
    ctx = AsyncMock()

    # --- Scenario 1: Previews listing with limit met (Finding #3 pagination hint) ---
    mock_previews_data = {
        "type": "previews",
        "previews": [
            {
                "id": "id1",
                "subject": "Subject 1",
                "sender_name": "Sender 1",
                "sender_email": "s1@adeu.ai",
                "received_datetime": "2026-01-01T12:00:00Z",
                "preview_text": "Text 1",
                "has_attachments": False,
                "is_read": True,
            },
            {
                "id": "id2",
                "subject": "Subject 2",
                "sender_name": "Sender 2",
                "sender_email": "s2@adeu.ai",
                "received_datetime": "2026-01-01T12:00:00Z",
                "preview_text": "Text 2",
                "has_attachments": False,
                "is_read": True,
            },
        ],
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_previews_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    # Request with limit=2 (matches size of previews array)
    res_previews = asyncio.run(
        search_and_fetch_emails(
            ctx=ctx, subject="Invoice", limit=2, offset=0, api_key="test_api_key"
        )
    )

    previews_text = _get_tool_text(res_previews)
    # Corrected: Expect backticks around `offset=2` to match format exactly
    assert (
        "*(If you need to see more results, call this tool again with `offset=2`)*"
        in previews_text
    )

    # --- Scenario 2: Single result auto-escalation (Finding #11 banner notice) ---
    mock_full_email_data = {
        "type": "full_email",
        "full_email": {
            "id": "adeu_12345",
            "subject": "Contract Review Required",
            "sender_name": "Legal",
            "sender_email": "legal@adeu.ai",
            "received_datetime": "2026-01-01T12:00:00Z",
            "body_html": "<p>Please look at this document.</p>",
            "is_thread": False,
            "attachments": [],
        },
    }

    mock_resp.read.return_value = json.dumps(mock_full_email_data).encode("utf-8")

    # Request without email_id, but with filters (simulating search finding exactly one result)
    res_escalation = asyncio.run(
        search_and_fetch_emails(
            ctx=ctx, subject="Contract Review Required", api_key="test_api_key"
        )
    )

    escalation_text = _get_tool_text(res_escalation)
    assert escalation_text.startswith(
        "_(Search returned exactly one result; auto-fetched full email below.)_"
    )

    # --- Scenario 3: Suggestions for attachments (Finding #9 downstream hint) ---
    mock_attachments_data = {
        "type": "full_email",
        "full_email": {
            "id": "adeu_12345",
            "subject": "Contract Attachment",
            "sender_name": "Legal",
            "sender_email": "legal@adeu.ai",
            "received_datetime": "2026-01-01T12:00:00Z",
            "body_html": "<p>Please see attachment.</p>",
            "is_thread": False,
            "attachments": [
                {
                    "filename": "draft_contract.docx",
                    "size_bytes": 1024,
                    "base64_data": base64.b64encode(b"dummy docx contents").decode(
                        "utf-8"
                    ),
                }
            ],
        },
    }

    mock_resp.read.return_value = json.dumps(mock_attachments_data).encode("utf-8")

    res_attachments = asyncio.run(
        search_and_fetch_emails(ctx=ctx, email_id="adeu_12345", api_key="test_api_key")
    )

    attachments_text = _get_tool_text(res_attachments)
    assert (
        "You can now use tools like `read_docx`, `diff_docx_files`, or `validate_documents`"
        in attachments_text
    )
