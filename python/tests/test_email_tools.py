# FILE: python/tests/test_email_tools.py
import asyncio
import base64
import json
import re
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from adeu.mcp_components.tools.email import (
    StaleShortIdError,
    _format_backend_error,
    create_email_draft,
    list_available_mailboxes,
    resolve_email_id,
    search_and_fetch_emails,
)


def _get_tool_text(res) -> str:
    """Helper to extract raw text content from an MCP ToolResult."""
    if hasattr(res, "content") and isinstance(res.content, list) and len(res.content) > 0:
        return res.content[0].text
    return str(res)


def test_finding_6_error_formatting_helpers():
    """Verify backend errors map to actionable instructions for the agent."""
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
    assert "Cloud search failed (HTTP 500): Some unexpected internal error." in formatted_generic


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

    result = asyncio.run(list_available_mailboxes(reasoning="test", ctx=ctx, api_key="test_api_key"))

    assert "### Connected Mailboxes" in result
    assert "Below is the list of connected mailboxes you have access to." in result
    assert "Use the `email_address` as the `mailbox_address` parameter" in result
    assert "**Personal Mailbox**" in result

    idx_primary = result.find("primary@adeu.ai")
    idx_secondary = result.find("secondary@adeu.ai")
    assert idx_primary != -1
    assert idx_secondary != -1
    assert idx_primary < idx_secondary, "Mailboxes should be sorted alphabetically by email_address"

    assert "- **Email Address**:" in result
    assert "- **Auto-Processing**:" in result
    assert "- **Write-Back Mode**:" in result


@patch("urllib.request.urlopen")
def test_finding_6_tool_boundary_error_handling_bogus_mailbox(mock_urlopen):
    """Verify search_and_fetch_emails propagates mapped mailbox 404 errors as a ToolError."""
    ctx = AsyncMock()

    mock_fp = MagicMock()
    mock_fp.read.return_value = json.dumps({"detail": "Mailbox 'bogus@nowhere.invalid' not found."}).encode("utf-8")

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
                reasoning="test",
                ctx=ctx,
                mailbox_address="bogus@nowhere.invalid",
                api_key="test_api_key",
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
    mock_fp.read.return_value = json.dumps({"detail": "Email not found."}).encode("utf-8")

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
                reasoning="test",
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
                reasoning="test",
                ctx=ctx,
                mailbox_address="sales@company.com",
                api_key="test_api_key",
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
            reasoning="test",
            ctx=ctx,
            subject="Invoice",
            limit=2,
            offset=0,
            api_key="test_api_key",
        )
    )

    previews_text = _get_tool_text(res_previews)
    # Corrected: Expect backticks around `offset=2` to match format exactly
    assert "*(If you need to see more results, call this tool again with `offset=2`)*" in previews_text

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
            reasoning="test",
            ctx=ctx,
            subject="Contract Review Required",
            api_key="test_api_key",
        )
    )

    escalation_text = _get_tool_text(res_escalation)
    assert escalation_text.startswith("_(Search returned exactly one result; auto-fetched full email below.)_")

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
                    "base64_data": base64.b64encode(b"dummy docx contents").decode("utf-8"),
                }
            ],
        },
    }

    mock_resp.read.return_value = json.dumps(mock_attachments_data).encode("utf-8")

    res_attachments = asyncio.run(
        search_and_fetch_emails(reasoning="test", ctx=ctx, email_id="adeu_12345", api_key="test_api_key")
    )

    attachments_text = _get_tool_text(res_attachments)
    assert "You can now use tools like `read_docx`, `diff_docx_files`, or `validate_documents`" in attachments_text


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_async_task_initiation_on_search_completed(mock_sleep, mock_urlopen):
    """Verify search_and_fetch_emails returns completed status.

    Triggered when backend indicates task creation but immediate poll succeeds.
    """
    ctx = AsyncMock()

    # 1. Search response (202 pending)
    mock_resp_init = MagicMock()
    mock_resp_init.__enter__.return_value.read.return_value = json.dumps(
        {"status": "pending", "task_id": "email_task_999", "message": "Task queued"}
    ).encode("utf-8")

    # 2. First poll (completed)
    mock_resp_poll = MagicMock()
    mock_resp_poll.__enter__.return_value.read.return_value = json.dumps(
        {"status": "COMPLETED", "type": "previews", "previews": []}
    ).encode("utf-8")

    mock_urlopen.side_effect = [
        mock_resp_init.__enter__.return_value,
        mock_resp_poll.__enter__.return_value,
    ]

    res = asyncio.run(
        search_and_fetch_emails(reasoning="test", ctx=ctx, subject="Heavy Search", api_key="test_api_key")
    )

    text = _get_tool_text(res)
    assert "No emails found matching your search criteria." in text
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_async_task_initiation_on_search_pending_timeout(mock_sleep, mock_urlopen):
    """Verify search_and_fetch_emails returns pending status.

    Triggered when backend indicates task creation and polling times out.
    """
    ctx = AsyncMock()

    # 1. Search response (202 pending)
    mock_resp_init = MagicMock()
    mock_resp_init.__enter__.return_value.read.return_value = json.dumps(
        {"status": "pending", "task_id": "email_task_999", "message": "Task queued"}
    ).encode("utf-8")

    # 2. Status polls (all return pending)
    mock_resp_poll = MagicMock()
    mock_resp_poll.__enter__.return_value.read.return_value = json.dumps({"status": "PENDING"}).encode("utf-8")

    mock_urlopen.side_effect = [mock_resp_init.__enter__.return_value] + [mock_resp_poll.__enter__.return_value] * 10

    res = asyncio.run(
        search_and_fetch_emails(reasoning="test", ctx=ctx, subject="Heavy Search", api_key="test_api_key")
    )

    text = _get_tool_text(res)
    assert "is still processing" in text
    assert "task_id=email_task_999" in text
    assert res.structured_content["status"] == "pending"
    assert res.structured_content["task_id"] == "email_task_999"
    assert mock_sleep.await_count == 10


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_polling_task_completed(mock_sleep, mock_urlopen):
    """Verify that providing task_id polls the status endpoint and handles success."""
    ctx = AsyncMock()

    # Simulate two polls: first is pending, second is completed
    mock_resp_pending = MagicMock()
    mock_resp_pending.__enter__.return_value.read.return_value = json.dumps({"status": "PENDING"}).encode("utf-8")

    mock_resp_completed = MagicMock()
    mock_resp_completed.__enter__.return_value.read.return_value = json.dumps(
        {"status": "COMPLETED", "type": "previews", "previews": []}
    ).encode("utf-8")

    mock_urlopen.side_effect = [
        mock_resp_pending.__enter__.return_value,
        mock_resp_completed.__enter__.return_value,
    ]

    res = asyncio.run(
        search_and_fetch_emails(reasoning="test", ctx=ctx, task_id="email_task_999", api_key="test_api_key")
    )

    text = _get_tool_text(res)
    assert "No emails found matching your search criteria." in text
    assert mock_sleep.await_count == 1
    mock_sleep.assert_awaited_with(5)


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_polling_task_failed(mock_sleep, mock_urlopen):
    """Verify that polling a failed task raises a clear ToolError."""
    ctx = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.read.return_value = json.dumps(
        {"status": "FAILED", "error": "Outlook API rate limit reached."}
    ).encode("utf-8")
    mock_urlopen.return_value = mock_resp.__enter__.return_value

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            search_and_fetch_emails(
                reasoning="test",
                ctx=ctx,
                task_id="email_task_999",
                api_key="test_api_key",
            )
        )

    assert "Validation task failed on the server: Outlook API rate limit reached." in str(exc_info.value)


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_polling_task_timeout(mock_sleep, mock_urlopen):
    """Verify that a task remaining pending past the 50s limit returns gracefully."""
    ctx = AsyncMock()

    # Always return PENDING to trigger timeout
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.read.return_value = json.dumps({"status": "PENDING"}).encode("utf-8")
    mock_urlopen.return_value = mock_resp.__enter__.return_value

    res = asyncio.run(
        search_and_fetch_emails(reasoning="test", ctx=ctx, task_id="email_task_999", api_key="test_api_key")
    )

    text = _get_tool_text(res)
    assert "is still processing" in text
    assert "task_id=email_task_999" in text
    assert mock_sleep.await_count == 10


def _make_full_email_payload(email_id: str) -> dict:
    return {
        "type": "full_email",
        "full_email": {
            "id": email_id,
            "subject": "Attachment Delivery",
            "sender_name": "Legal",
            "sender_email": "legal@adeu.ai",
            "received_datetime": "2026-01-01T12:00:00Z",
            "body_html": "<p>See attachment.</p>",
            "is_thread": False,
            "attachments": [
                {
                    "filename": "questionnaire.docx",
                    "size_bytes": 128,
                    "base64_data": base64.b64encode(b"questionnaire contents").decode("utf-8"),
                }
            ],
        },
    }


@patch("urllib.request.urlopen")
def test_working_directory_created_when_missing(mock_urlopen, tmp_path):
    """A missing working_directory is created recursively instead of silently falling back to temp."""
    ctx = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(_make_full_email_payload("adeu_777")).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    requested_dir = tmp_path / "questionnaires" / "nested"
    assert not requested_dir.exists()

    res = asyncio.run(
        search_and_fetch_emails(
            reasoning="test",
            ctx=ctx,
            email_id="adeu_777",
            working_directory=str(requested_dir),
            api_key="test_api_key",
        )
    )

    text = _get_tool_text(res)
    assert requested_dir.is_dir()
    assert "Attachment location notice" not in text

    saved_files = list((requested_dir / "adeu_attachments").rglob("questionnaire.docx"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == b"questionnaire contents"
    assert str(saved_files[0]) in text


@patch("adeu.mcp_components.tools.email.tempfile.gettempdir")
@patch("urllib.request.urlopen")
def test_working_directory_fallback_note_when_uncreatable(mock_urlopen, mock_gettempdir, tmp_path):
    """When the working_directory cannot be created, fall back to temp WITH an explicit notice."""
    ctx = AsyncMock()

    fake_tmp = tmp_path / "faketmp"
    fake_tmp.mkdir()
    mock_gettempdir.return_value = str(fake_tmp)

    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a directory")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(_make_full_email_payload("adeu_778")).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    res = asyncio.run(
        search_and_fetch_emails(
            reasoning="test",
            ctx=ctx,
            email_id="adeu_778",
            working_directory=str(blocker / "sub"),
            api_key="test_api_key",
        )
    )

    text = _get_tool_text(res)
    assert "Attachment location notice" in text
    assert "do NOT re-run the search" in text

    saved_files = list((fake_tmp / "adeu_downloads").rglob("questionnaire.docx"))
    assert len(saved_files) == 1
    assert str(saved_files[0]) in text


@patch("urllib.request.urlopen")
def test_short_id_fetch_reuses_search_mailbox(mock_urlopen, tmp_path):
    """Fetching by short ID without mailbox_address re-applies the mailbox from the original search.

    Provider message IDs are mailbox-scoped; without this, NULL-mailbox fetches
    resolve against the PRIMARY account and 404 with 'Email not found.'
    """
    ctx = AsyncMock()

    previews_payload = {
        "type": "previews",
        "previews": [
            {
                "id": "AAMkAD_shared_item_1",
                "subject": "Questionnaire",
                "sender_name": "Abo Shoten",
                "sender_email": "ops@aboshoten.example",
                "received_datetime": "2026-07-07T09:00:00Z",
                "preview_text": "Please fill in",
                "has_attachments": True,
                "is_read": False,
            },
            {
                "id": "AAMkAD_shared_item_2",
                "subject": "Other mail",
                "sender_name": "Abo Shoten",
                "sender_email": "ops@aboshoten.example",
                "received_datetime": "2026-07-07T09:01:00Z",
                "preview_text": "Something else",
                "has_attachments": False,
                "is_read": True,
            },
        ],
    }

    with patch("adeu.mcp_components.tools.email.CACHE_FILE", tmp_path / "cache.json"):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(previews_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res_search = asyncio.run(
            search_and_fetch_emails(
                reasoning="test",
                ctx=ctx,
                subject="Questionnaire",
                mailbox_address="risto.kariranta@ahti.io",
                api_key="test_api_key",
            )
        )
        short_id_match = re.search(r"msg_[0-9a-f]{6}", _get_tool_text(res_search))
        assert short_id_match is not None
        short_id = short_id_match.group(0)

        mock_resp.read.return_value = json.dumps(_make_full_email_payload("AAMkAD_shared_item_1")).encode("utf-8")

        asyncio.run(
            search_and_fetch_emails(
                reasoning="test",
                ctx=ctx,
                email_id=short_id,
                api_key="test_api_key",
            )
        )

    fetch_request = mock_urlopen.call_args_list[-1].args[0]
    body = json.loads(fetch_request.data.decode("utf-8"))
    assert body["email_id"] == "AAMkAD_shared_item_1"
    assert body["mailbox_address"] == "risto.kariranta@ahti.io"


@patch("urllib.request.urlopen")
def test_legacy_cache_entries_still_resolve(mock_urlopen, tmp_path):
    """Plain-string cache entries from older versions resolve fine and inject no mailbox."""
    ctx = AsyncMock()

    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"msg_legacy": "raw_provider_id_123"}), encoding="utf-8")

    with patch("adeu.mcp_components.tools.email.CACHE_FILE", cache_file):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_make_full_email_payload("raw_provider_id_123")).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        asyncio.run(
            search_and_fetch_emails(
                reasoning="test",
                ctx=ctx,
                email_id="msg_legacy",
                api_key="test_api_key",
            )
        )

    fetch_request = mock_urlopen.call_args_list[-1].args[0]
    body = json.loads(fetch_request.data.decode("utf-8"))
    assert body["email_id"] == "raw_provider_id_123"
    assert "mailbox_address" not in body


@patch("urllib.request.urlopen")
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_failed_task_error_carries_recovery_hint(mock_sleep, mock_urlopen):
    """Async task failures map known errors to recovery hints (parity with sync 404s)."""
    ctx = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.read.return_value = json.dumps(
        {"status": "FAILED", "error": "Email not found."}
    ).encode("utf-8")
    mock_urlopen.return_value = mock_resp.__enter__.return_value

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            search_and_fetch_emails(
                reasoning="test",
                ctx=ctx,
                task_id="email_task_777",
                api_key="test_api_key",
            )
        )

    msg = str(exc_info.value)
    assert "Validation task failed on the server:" in msg
    assert "re-run search_and_fetch_emails with filters" in msg
    assert "mailbox_address" in msg


@patch("urllib.request.urlopen")
def test_reply_draft_reuses_cached_mailbox(mock_urlopen, tmp_path):
    """Reply drafts inherit the mailbox the original email's short ID was harvested from."""
    ctx = AsyncMock()

    cache_file = tmp_path / "cache.json"
    cache_file.write_text(
        json.dumps({"msg_rep01": {"id": "AAMkAD_reply_item_1", "mailbox": "sales@ahti.io"}}),
        encoding="utf-8",
    )

    with patch("adeu.mcp_components.tools.email.CACHE_FILE", cache_file):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "draft_1"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        asyncio.run(
            create_email_draft(
                reasoning="test",
                ctx=ctx,
                body_markdown="Thanks, will do!",
                reply_to_email_id="msg_rep01",
                api_key="test_api_key",
            )
        )

    draft_request = mock_urlopen.call_args_list[-1].args[0]
    body_bytes = draft_request.data
    assert b"AAMkAD_reply_item_1" in body_bytes
    assert b"sales@ahti.io" in body_bytes


def test_ui_templates_are_in_sync():
    """The Node package ships byte-identical copies of the canonical UI templates.

    There is no build step keeping them aligned — sync is manual — so this guard
    fails loudly when one copy is edited without the other.
    """
    python_templates = Path(__file__).resolve().parents[1] / "src" / "adeu" / "templates"
    node_templates = Path(__file__).resolve().parents[2] / "node" / "packages" / "mcp-server" / "src" / "templates"
    if not node_templates.exists():
        pytest.skip("node package not present in this checkout")

    for name in ("email_ui.html", "markdown_ui.html"):
        assert (python_templates / name).read_bytes() == (node_templates / name).read_bytes(), (
            f"{name} differs between python/src/adeu/templates and "
            f"node/packages/mcp-server/src/templates — copy the canonical version over."
        )
