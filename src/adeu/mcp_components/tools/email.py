# FILE: src/adeu/mcp_components/tools/email.py
import base64
import hashlib
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
from adeu.mcp_components.shared import (
    BACKEND_URL,
    EMAIL_UI_URI,
    encode_multipart_formdata,
    read_file_bytes,
)

CACHE_FILE = Path.home() / ".adeu" / "mcp_id_cache.json"
MAX_CACHE_SIZE = 1000


def load_id_cache() -> dict[str, str]:
    """Loads the ID mapping cache from disk."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_id_cache(cache: dict[str, str]) -> None:
    """Saves the ID mapping cache to disk, keeping only the most recent MAX_CACHE_SIZE items."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if len(cache) > MAX_CACHE_SIZE:
            # Python 3.7+ dicts maintain insertion order. Keep newest.
            cache = dict(list(cache.items())[-MAX_CACHE_SIZE:])
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass  # Non-fatal if we cannot write to the cache


def minify_email_id(real_id: str, cache: dict[str, str]) -> str:
    """Hashes a giant provider ID into a short ID and adds it to the current cache dict."""
    if not real_id:
        return real_id
    short_id = f"msg_{hashlib.md5(real_id.encode('utf-8')).hexdigest()[:6]}"
    cache[short_id] = real_id
    return short_id


def resolve_email_id(short_id: str) -> str:
    """Looks up a short ID from disk and returns the real provider ID."""
    if not short_id:
        return short_id
    cache = load_id_cache()
    return cache.get(short_id, short_id)


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
        text = s.get_data()
        return re.sub(r"\n\s*\n", "\n\n", text)
    except Exception:
        return html


def remove_nested_quotes(text: str) -> str:
    """Heuristically strips trailing quoted replies from email bodies."""
    if not text:
        return ""
    lines = text.split("\n")
    clean_lines = []
    for line in lines:
        # Outlook common dividers
        if re.search(r"_{10,}", line):
            break
        if re.search(r"From:.*Sent:", line):
            break
        # Gmail / Generic dividers
        if re.search(r"On .* wrote:", line):
            break
        if line.strip() == "Original Message":
            break
        if re.search(r"-----Original Message-----", line):
            break
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def _resolve_attachment_dir(working_directory: str | None, email_id: str) -> Path:
    """
    Returns the directory where attachments for a given email should be saved.
    Uses working_directory if provided and valid, otherwise falls back to temp.
    """
    if working_directory:
        base = Path(working_directory)
        if base.exists() and base.is_dir():
            return base / "adeu_attachments" / email_id

    # Fallback to system temp
    return Path(tempfile.gettempdir()) / "adeu_downloads" / email_id


def _get_unique_filepath(save_dir: Path, filename: str) -> Path:
    """Ensures filename uniqueness by appending _1, _2 etc. if the file exists."""
    base_path = save_dir / filename
    if not base_path.exists():
        return base_path

    stem = base_path.stem
    suffix = base_path.suffix
    counter = 1
    while True:
        new_path = save_dir / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


@tool(
    description=(
        "Searches the user's live email inbox. "
        "Use filters to find specific emails (e.g., 'is_unread=True' for new emails, "
        "'days_ago=7' for last week, 'folder=sent' for sent items). "
        "It returns a list of lightweight email previews. "
        "To read the full email body, thread history, and automatically download attachments "
        "to local disk, call this tool again and provide the specific `email_id`. "
        "Emails often contain attachments. It is highly recommended to always provide "
        "the `working_directory` parameter so attachments are saved directly to the user's "
        "actual project folder. This directory path refers to the user's native operating system, "
        "not the LLM's sandbox environment."
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
        "If provided, fetches the exact full email and downloads its attachments. "
        "Accepts short IDs from search results (e.g., 'msg_abc123') OR direct Adeu IDs (e.g., 'adeu_4052').",
    ] = None,
    working_directory: Annotated[
        Optional[str],
        "Optional. The current working directory of the project or task. "
        "If provided, attachments will be saved here under an 'adeu_attachments' subfolder. "
        "If omitted, attachments are saved to the system temp directory.",
    ] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    await ctx.info("Starting live email search", extra={"email_id": email_id, "subject": subject})
    real_email_id = resolve_email_id(email_id) if email_id else None
    payload_dict = {
        "email_id": real_email_id,
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

        # Load the cache to update it with new IDs
        id_cache = load_id_cache()

        llm_lines = [f"Found {len(previews)} email(s). Here are the previews:", ""]
        for p in previews:
            short_id = minify_email_id(p["id"], id_cache)
            p["id"] = short_id
            att_flag = "📎 (Has Attachments)" if p.get("has_attachments") else ""
            unread_flag = "🟢 [UNREAD]" if p.get("is_read") is False else ""

            llm_lines.append(f"- **ID**: `{short_id}`")
            llm_lines.append(f"  **Subject**: {p['subject']} {att_flag} {unread_flag}")
            llm_lines.append(f"  **From**: {p['sender_name']} <{p['sender_email']}>")
            llm_lines.append(f"  **Date**: {p['received_datetime']}")
            llm_lines.append(f"  **Preview**: {p['preview_text']}")
            llm_lines.append("")

        # Save the updated cache back to disk
        save_id_cache(id_cache)

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

        email_id_str = full_email.get("id", "unknown_id")
        id_cache = load_id_cache()
        short_target_id = minify_email_id(email_id_str, id_cache) if email_id_str != "unknown_id" else "unknown_id"
        full_email["id"] = short_target_id
        for hist_msg in full_email.get("messages", []):
            if "id" in hist_msg:
                hist_msg["id"] = minify_email_id(hist_msg["id"], id_cache)

        save_id_cache(id_cache)

        save_dir = _resolve_attachment_dir(working_directory, short_target_id)
        save_dir.mkdir(parents=True, exist_ok=True)

        # Helper to process attachments for a given message block
        async def process_message_attachments(message_data: dict) -> list[str]:
            local_files = []
            for att in message_data.get("attachments", []):
                filename = att.get("filename", "unnamed_file")
                b64_data = att.pop("base64_data", None)  # Remove from UI payload to save IPC memory

                if b64_data:
                    try:
                        file_path = _get_unique_filepath(save_dir, filename)
                        file_path.write_bytes(base64.b64decode(b64_data))
                        local_files.append(str(file_path))
                        att["local_path"] = str(file_path)
                    except Exception as e:
                        await ctx.warning(f"Failed to save attachment {filename}: {e}")
            return local_files

        # 1. Format LLM Output (Thread History + Main Body)
        llm_lines = [f"# Email Thread: {full_email.get('subject')}", ""]

        # Process Target Message (Newest) FIRST
        target_local_files = await process_message_attachments(full_email)
        raw_clean_body = strip_tags(full_email.get("body_html", ""))
        clean_body = remove_nested_quotes(raw_clean_body)

        llm_lines.append("## Target Message (Newest):")
        llm_lines.append(f"**From**: {full_email.get('sender_name')} <{full_email.get('sender_email')}>")
        llm_lines.append(f"**Date**: {full_email.get('received_datetime')}")

        if target_local_files:
            llm_lines.append("**Attachments Saved Locally**:")
            for path in target_local_files:
                llm_lines.append(f"- 📎 `{path}`")

        llm_lines.append(f"**Body**:\n```\n{clean_body}\n```\n")

        # 2. Surface Brief (if available from DB)
        brief_html = full_email.get("brief_content")
        if brief_html:
            clean_brief = strip_tags(brief_html)
            llm_lines.append("## 🧠 AI Strategy Brief (Previously Generated):")
            llm_lines.append(f"```\n{clean_brief}\n```\n")
            llm_lines.append(
                "*This brief was previously generated by Adeu for this email. "
                "It reflects the AI's analysis at the time of processing.*\n"
            )

        # 3. Process Older Thread Messages NEXT
        if full_email.get("is_thread") and full_email.get("messages"):
            llm_lines.append("## Previous Messages in Thread (Historical Context):")
            for idx, hist_msg in enumerate(full_email.get("messages", [])):
                hist_local_files = await process_message_attachments(hist_msg)

                raw_clean_hist = strip_tags(hist_msg.get("body_html", ""))
                clean_hist = remove_nested_quotes(raw_clean_hist)

                llm_lines.append(f"### Message {-1 * (idx + 1)} (Older)")
                llm_lines.append(f"**From**: {hist_msg.get('sender_name')} <{hist_msg.get('sender_email')}>")
                llm_lines.append(f"**Date**: {hist_msg.get('received_datetime')}")

                if hist_local_files:
                    llm_lines.append("**Attachments Saved Locally**:")
                    for path in hist_local_files:
                        llm_lines.append(f"- 📎 `{path}`")

                llm_lines.append(f"**Body**:\n```\n{clean_hist}\n```\n")
            llm_lines.append("---")

        if target_local_files or any(m.get("attachments") for m in full_email.get("messages", [])):
            llm_lines.append(
                "\n*You can now use tools like `read_docx`, `diff_docx_files`, or `validate_documents` "
                "on the local file paths listed under each message.*"
            )

        return ToolResult(content="\n".join(llm_lines), structured_content=data)
    return ToolResult(content="Unknown response format from backend.", structured_content=data)


@tool(
    name="create_email_draft",
    description=(
        "Creates an email draft in the user's native draft box (e.g., Outlook/Gmail). "
        "Can either start a NEW email, or REPLY to an existing thread. "
        "To REPLY, provide 'reply_to_email_id' (the short ID from search_and_fetch_emails). "
        "To start a NEW email, omit the ID but provide 'subject' and 'to_recipients'. "
        "Allows attaching local files (PDF/DOCX) by providing their absolute paths. "
        "The body should be formatted in Markdown."
    ),
)
async def create_email_draft(
    ctx: Context,
    body_markdown: Annotated[str, "The body of the email in Markdown format. Will be converted to HTML."],
    reply_to_email_id: Annotated[Optional[str], "Provide the short email ID to reply to an existing thread."] = None,
    subject: Annotated[Optional[str], "The subject line. Required if starting a NEW email."] = None,
    to_recipients: Annotated[Optional[list[str] | str], "List of emails. Required if starting a NEW email."] = None,
    attachment_paths: Annotated[
        Optional[list[str] | str],
        "List of absolute file paths on the local system to attach to the draft.",
    ] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    # 1. Validation
    if not reply_to_email_id and (not subject or not to_recipients):
        return ToolResult(
            "Error: You must provide either 'reply_to_email_id' (to reply) OR "
            "both 'subject' and 'to_recipients' (to start a new email).",
        )

    await ctx.info(
        "Creating email draft",
        extra={"reply_to": reply_to_email_id, "subject": subject},
    )
    url = f"{BACKEND_URL}/api/v1/emails/drafts/new"

    # Helper to safely parse stringified lists from Claude
    def _parse_list(val) -> list[str]:
        if not val:
            return []
        if isinstance(val, list):
            return val
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else [val]
        except Exception:
            return [x.strip() for x in val.split(",") if x.strip()]

    parsed_recipients = _parse_list(to_recipients)
    parsed_attachments = _parse_list(attachment_paths)

    # 2. Resolve Minified ID to Real Graph ID
    real_reply_to_id = resolve_email_id(reply_to_email_id) if reply_to_email_id else None

    # Prepare text fields
    fields = {
        "body_markdown": body_markdown,
    }
    if real_reply_to_id:
        fields["reply_to_email_id"] = real_reply_to_id
    if subject:
        fields["subject"] = subject
    if parsed_recipients:
        fields["to_recipients"] = json.dumps(parsed_recipients)

    # Prepare file bytes
    files_to_upload = []
    if parsed_attachments:
        for path in parsed_attachments:
            try:
                file_bytes = read_file_bytes(path).getvalue()
                filename = Path(path).name
                files_to_upload.append(("files", filename, file_bytes))
            except Exception as e:
                raise ToolError(f"Failed to read attachment {path}: {e}") from e

    # Encode payload
    body, content_type = encode_multipart_formdata(fields=fields, files=files_to_upload)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        await ctx.debug("Sending draft request to Adeu Cloud")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            draft_id = data.get("id")
            return ToolResult(content=f"Successfully created email draft! Draft ID: {draft_id}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            raise ToolError("Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
        error_body = e.read().decode("utf-8")
        raise ToolError(f"Cloud draft creation failed (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e
