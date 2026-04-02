# FILE: src/adeu/mcp_components/tools/email.py
import base64
import json
import re
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastmcp import Context
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult

from adeu.mcp_components.desktop_auth import DesktopAuthManager, get_cloud_auth_token
from adeu.mcp_components.shared import BACKEND_URL, EMAIL_UI_URI


class MLStripper(HTMLParser):
    """Simple HTML stripper to provide clean text to the LLM."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []

    def handle_data(self, d):
        self.text.append(d)

    def get_data(self):
        return "".join(self.text).strip()


def strip_tags(html: str) -> str:
    if not html:
        return ""
    try:
        s = MLStripper()
        s.feed(html)
        # Collapse multiple newlines/spaces to save tokens

        text = s.get_data()
        return re.sub(r"\n\s*\n", "\n\n", text)
    except Exception:
        return html


@tool(
    description=(
        "Searches the user's live email inbox. "
        "Use filters to find specific emails (e.g., 'is_unread=True' for new emails, "
        "'days_ago=7' for last week, 'folder=sent' for sent items). "
        "It returns a list of lightweight email previews. "
        "To read the full email body, thread history, and automatically download attachments "
        "to local disk, call this tool again and provide the specific `email_id`."
    ),
    annotations={"openWorldHint": True, "readOnlyHint": True},
    meta={"ui": {"resourceUri": EMAIL_UI_URI}},
)
async def search_and_fetch_emails(
    ctx: Context,
    sender: Annotated[Optional[str], "Filter by the sender's email address or name."] = None,
    subject: Annotated[Optional[str], "Filter by keywords in the subject line."] = None,
    has_attachments: Annotated[Optional[bool], "If True, only returns emails that contain file attachments."] = None,
    attachment_name: Annotated[Optional[str], "Filter by a specific attachment filename."] = None,
    is_unread: Annotated[
        Optional[bool],
        "If True, returns ONLY unread emails. If False, returns ONLY read emails. Leave empty for both.",
    ] = None,
    days_ago: Annotated[
        Optional[int],
        "Filter emails received in the last N days (e.g., 7 for last week).",
    ] = None,
    folder: Annotated[
        Optional[Literal["inbox", "sent", "all"]],
        "The mailbox folder to search in (default is all).",
    ] = None,
    limit: Annotated[int, "Maximum number of emails to retrieve (default: 10)."] = 10,
    offset: Annotated[int, "Pagination offset to skip the first N emails."] = 0,
    email_id: Annotated[
        Optional[str],
        "If provided, fetches the exact full email and downloads its attachments, ignoring other filters.",
    ] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    await ctx.info("Starting live email search", extra={"email_id": email_id, "subject": subject})

    payload_dict = {
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "has_attachments": has_attachments,
        "attachment_name": attachment_name,
        "is_unread": is_unread,
        "days_ago": days_ago,
        "folder": folder,
        "limit": limit,
        "offset": offset,
    }
    payload_dict = {k: v for k, v in payload_dict.items() if v is not None}

    body = json.dumps(payload_dict).encode("utf-8")
    url = f"{BACKEND_URL}/api/v1/emails/search"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        await ctx.debug("Sending search request to Adeu Cloud", extra={"url": url})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            raise ToolError("Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
        error_body = e.read().decode("utf-8")
        raise ToolError(f"Cloud search failed (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e

    response_type = data.get("type")

    # ==========================================
    # SCENARIO A: PREVIEWS (Multiple Emails)
    # ==========================================
    if response_type == "previews":
        previews = data.get("previews", [])
        if not previews:
            return ToolResult(
                content="No emails found matching your search criteria.",
                structured_content=data,
            )

        llm_lines = [f"Found {len(previews)} email(s). Here are the previews:", ""]
        for p in previews:
            att_flag = "📎 (Has Attachments)" if p.get("has_attachments") else ""
            unread_flag = "🟢 [UNREAD]" if p.get("is_read") is False else ""  # Let the LLM see it's unread

            llm_lines.append(f"- **ID**: `{p['id']}`")
            llm_lines.append(f"  **Subject**: {p['subject']} {att_flag} {unread_flag}")
            llm_lines.append(f"  **From**: {p['sender_name']} <{p['sender_email']}>")
            llm_lines.append(f"  **Date**: {p['received_datetime']}")
            llm_lines.append(f"  **Preview**: {p['preview_text']}")
            llm_lines.append("")

        llm_lines.append(
            "⚠️ **ACTION REQUIRED**: To read the full body of an email and download its attachments to the local disk, "
            "you must call this tool again and provide the exact `email_id` of the message you want to open.\n"
            f"*(If you need to see more results, call this tool again with `offset={offset + limit}`)*"
        )
        return ToolResult(content="\n".join(llm_lines), structured_content=data)

    # ==========================================
    # SCENARIO B: FULL EMAIL (Single Email Drill-down)
    # ==========================================
    elif response_type == "full_email":
        full_email = data.get("full_email", {})
        if not full_email:
            return ToolResult(content="Failed to retrieve full email.", structured_content=data)

        # 1. Download Attachments locally
        base_temp_dir = Path(tempfile.gettempdir()) / "adeu_downloads"
        email_id_str = full_email.get("id", "unknown_id")
        local_files = []

        for att in full_email.get("attachments", []):
            filename = att.get("filename", "unnamed_file")
            b64_data = att.pop("base64_data", None)  # 💥 REMOVE from UI payload to prevent iframe bridge crashing

            if b64_data:
                try:
                    save_dir = base_temp_dir / email_id_str
                    save_dir.mkdir(parents=True, exist_ok=True)

                    file_path = save_dir / filename
                    file_path.write_bytes(base64.b64decode(b64_data))
                    local_files.append(str(file_path))

                    # Tag with local path for the UI
                    att["local_path"] = str(file_path)
                except Exception as e:
                    await ctx.warning(f"Failed to save attachment {filename}: {e}")

        # 2. Format LLM Output (Thread History + Main Body)
        llm_lines = [f"# Email Thread: {full_email.get('subject')}", ""]

        # Process Older Thread Messages First
        if full_email.get("is_thread") and full_email.get("messages"):
            llm_lines.append("## Previous Messages in Thread:")
            for idx, hist_msg in enumerate(full_email.get("messages", [])):
                clean_hist = strip_tags(hist_msg.get("body_html", ""))
                llm_lines.append(f"### Message {idx + 1}")
                llm_lines.append(f"**From**: {hist_msg.get('sender_name')} <{hist_msg.get('sender_email')}>")
                llm_lines.append(f"**Date**: {hist_msg.get('received_datetime')}")
                llm_lines.append(f"**Body**:\n```\n{clean_hist}\n```\n")
            llm_lines.append("---")

        # Process Main/Newest Message
        clean_body = strip_tags(full_email.get("body_html", ""))
        llm_lines.append("## Target Message (Newest):")
        llm_lines.append(f"**From**: {full_email.get('sender_name')} <{full_email.get('sender_email')}>")
        llm_lines.append(f"**Date**: {full_email.get('received_datetime')}")
        llm_lines.append(f"**Body**:\n```\n{clean_body}\n```\n")

        # Process Attachments List
        if local_files:
            llm_lines.append("## 📎 Attachments Saved Locally:")
            for path in local_files:
                llm_lines.append(f"- `{path}`")
            llm_lines.append(
                "\n*You can now use tools like `read_docx`, `diff_docx_files`, or `validate_documents` "
                "on the local file paths listed above.*"
            )
        else:
            llm_lines.append("*No attachments found in this email.*")

        return ToolResult(content="\n".join(llm_lines), structured_content=data)

    # Fallback
    return ToolResult(content="Unknown response format from backend.", structured_content=data)
