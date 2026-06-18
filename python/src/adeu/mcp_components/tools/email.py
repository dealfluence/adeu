import asyncio
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

_KNOWN_ERROR_HINTS: dict[str, str] = {
    "Email not found.": (
        "The email ID was not found. If this was a short ID (msg_*), it may have been "
        "evicted from the local cache or come from a different machine — re-run "
        "search_and_fetch_emails with filters to get a fresh ID. If it was an "
        "adeu_<numeric> or raw provider ID, verify it's correct."
    ),
    "Adeu email reference not found.": (
        "The adeu_<id> reference doesn't resolve to any processed email for this user. "
        "Verify the ID, or re-run search_and_fetch_emails with filters to find the message."
    ),
    "Invalid adeu_ email ID format.": ("The adeu_<id> reference is malformed. Expected format: adeu_<integer>."),
}


def _format_backend_error(status_code: int, response_body: str) -> str:
    """Wrap a backend HTTPError into a single-format message with recovery hints when possible.

    The wrapper format is consistent across all calls: `Cloud search failed (HTTP {code}): {message}`.
    When the backend returns a known error detail (or a Mailbox-not-found error), we substitute
    the raw detail with actionable guidance instead.
    """
    detail = response_body
    try:
        parsed = json.loads(response_body)
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = str(parsed["detail"])
    except Exception:
        pass

    hint = _KNOWN_ERROR_HINTS.get(detail)
    if hint is None:
        # Mailbox-not-found has a dynamic name baked into the string; match by prefix/suffix.
        if detail.startswith("Mailbox '") and detail.endswith("' not found."):
            mailbox = detail[len("Mailbox '") : -len("' not found.")]
            hint = (
                f"The mailbox '{mailbox}' is not connected to your Adeu account. "
                "Call list_available_mailboxes to see valid mailbox addresses, then retry "
                "with one of those as `mailbox_address`."
            )

    message = hint if hint else detail
    return f"Cloud search failed (HTTP {status_code}): {message}"


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
            cache = dict(list(cache.items())[-MAX_CACHE_SIZE:])
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def minify_email_id(real_id: str, cache: dict[str, str]) -> str:
    """Hashes a giant provider ID into a short ID and adds it to the current cache dict."""
    if not real_id:
        return real_id
    short_id = f"msg_{hashlib.md5(real_id.encode('utf-8')).hexdigest()[:6]}"
    cache[short_id] = real_id
    return short_id


class StaleShortIdError(ToolError):
    """Raised when a short-form `msg_xxx` ID isn't in the local cache."""


def resolve_email_id(short_id: str) -> str:
    """Looks up a short ID from disk and returns the real provider ID.

    Behavior:
    - Empty string → return as-is.
    - 'adeu_<id>' → server-side reference; pass through untouched.
    - 'msg_<hash>' present in cache → return real provider ID.
    - 'msg_<hash>' NOT in cache → raise StaleShortIdError. The short ID was
      generated on a different machine, a previous session, or has been
      evicted from the local LRU. The agent should re-run search_and_fetch_emails
      to get fresh IDs.
    - Anything else → assume raw provider ID and pass through.
    """
    if not short_id:
        return short_id
    if short_id.startswith("adeu_"):
        return short_id
    cache = load_id_cache()
    resolved = cache.get(short_id)
    if resolved:
        return resolved
    if short_id.startswith("msg_"):
        raise StaleShortIdError(
            f"Short ID '{short_id}' is not in the local cache (it may have been evicted, "
            f"or it came from a different machine/session). Short IDs only persist on the "
            f"machine where they were generated. Re-run search_and_fetch_emails with filters "
            f"(sender, subject, days_ago) to fetch fresh IDs, then use the new ID from those results."
        )
    return short_id


class MLStripper(HTMLParser):
    """Simple HTML stripper to provide clean text to the LLM."""

    SUPPRESSED_TAGS = {"style", "script", "head"}

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []
        self._suppress_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SUPPRESSED_TAGS:
            self._suppress_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SUPPRESSED_TAGS and self._suppress_depth > 0:
            self._suppress_depth -= 1

    def handle_data(self, d):
        if self._suppress_depth == 0:
            self.text.append(d)

    def get_data(self):
        return "".join(self.text).strip()


def strip_tags(html: str) -> str:
    if not html:
        return ""
    try:
        normalized = re.sub(
            r"</(p|div|br|hr|tr|li|h[1-6]|blockquote)\s*/?>",
            "\n",
            html,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)

        s = MLStripper()
        s.feed(normalized)
        text = s.get_data()
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    except Exception:
        return html


def remove_nested_quotes(text: str) -> str:
    """Heuristically strips trailing quoted replies from email bodies.

    Supports English, Finnish, Swedish, German, French, Spanish, Italian,
    Dutch, Norwegian, Danish, and Portuguese Outlook/Gmail quote markers.
    """
    if not text:
        return ""

    # Localized "From:" / "Sent:" header tokens from Outlook in major European locales.
    from_tokens = "|".join(
        [
            "From",
            "Lähettäjä",
            "Från",
            "Von",
            "De",
            "Da",
            "Van",
            "Fra",
            "Mittente",
        ]
    )
    sent_tokens = "|".join(
        [
            "Sent",
            "Lähetetty",
            "Skickat",
            "Gesendet",
            "Envoyé",
            "Enviado",
            "Inviato",
            "Verzonden",
            "Sendt",
        ]
    )

    original_message_tokens = "|".join(
        [
            "Original Message",
            "Alkuperäinen viesti",
            "Ursprüngliches Nachricht",
            "Message d'origine",
            "Mensaje original",
            "Messaggio originale",
            "Oorspronkelijk bericht",
            "Original meddelande",
        ]
    )

    # Localized "Forwarded message" markers across the same locale set
    forwarded_tokens = "|".join(
        [
            "Forwarded message",
            "Välitetty viesti",
            "Vidarebefordrat meddelande",
            "Weitergeleitete Nachricht",
            "Message transféré",
            "Mensaje reenviado",
            "Messaggio inoltrato",
            "Doorgestuurd bericht",
            "Videresendt melding",
            "Videresendt meddelelse",
            "Mensagem encaminhada",
        ]
    )

    divider_patterns = [
        re.compile(r"_{10,}", re.MULTILINE),
        re.compile(
            rf"^({from_tokens})\s*:.*?\n(?:.*\n){{0,5}}?({sent_tokens})\s*:",
            re.MULTILINE | re.DOTALL,
        ),
        re.compile(rf"-----\s*({original_message_tokens})\s*-----", re.IGNORECASE),
        re.compile(rf"^({original_message_tokens})$", re.MULTILINE | re.IGNORECASE),
        # Forwarded message dividers (Gmail/Outlook): "---------- Forwarded message ---------"
        # or just the bare localized phrase on its own line. Once this marker is hit,
        # everything below is a quoted historical message and should be cut.
        re.compile(rf"-+\s*({forwarded_tokens})\s*-+", re.IGNORECASE),
        re.compile(rf"^({forwarded_tokens})$", re.MULTILINE | re.IGNORECASE),
        # "On ... wrote:" style across locales
        re.compile(r"On .{1,200}? wrote:", re.MULTILINE | re.DOTALL),
        re.compile(r"Le .{1,200}? a écrit\s*:", re.IGNORECASE | re.DOTALL),
        re.compile(r"Am .{1,200}? schrieb .{1,100}?:", re.IGNORECASE | re.DOTALL),
        re.compile(r"El .{1,200}? escribió\s*:", re.IGNORECASE | re.DOTALL),
        re.compile(r"Il .{1,200}? ha scritto\s*:", re.IGNORECASE | re.DOTALL),
        re.compile(r"Op .{1,200}? schreef .{1,100}?:", re.IGNORECASE | re.DOTALL),
        re.compile(r"Den .{1,200}? skrev .{1,100}?:", re.IGNORECASE | re.DOTALL),
        re.compile(r"Em .{1,200}? escreveu\s*:", re.IGNORECASE | re.DOTALL),
    ]

    earliest_cut = None
    for pattern in divider_patterns:
        match = pattern.search(text)
        if match is not None:
            if earliest_cut is None or match.start() < earliest_cut:
                earliest_cut = match.start()

    if earliest_cut is None:
        return text.strip()

    return text[:earliest_cut].strip()


def _resolve_attachment_dir(working_directory: str | None, email_id: str) -> Path:
    if working_directory:
        base = Path(working_directory)
        if base.exists() and base.is_dir():
            return base / "adeu_attachments" / email_id
    return Path(tempfile.gettempdir()) / "adeu_downloads" / email_id


def _format_bytes(size_bytes: int | None) -> str:
    """Human-readable byte count for LLM-facing messages."""
    if size_bytes is None:
        return "unknown size"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _get_unique_filepath(save_dir: Path, filename: str) -> Path:
    """Return the target filepath for an attachment.

    Re-fetches of the same email overwrite the existing file rather than
    accumulating `_1`, `_2`, `_3` copies. The `<short_id>/` subdirectory
    already disambiguates across emails, so collisions inside it always
    mean the same logical attachment.
    """
    return save_dir / filename


@tool(
    description=(
        "Lists all personal and shared/delegated mailboxes the authenticated Adeu user has access to, "
        "across ALL of their linked provider accounts. Returns each mailbox's email_address, "
        "display_name, auto-processing settings, and write-back preference.\n\n"
        "This is the right tool to answer 'which accounts/mailboxes am I logged into?' — Adeu login "
        "is user-level, so a single MCP session can see every mailbox listed here regardless of which "
        "provider account was used for SSO.\n\n"
        "Call this FIRST when the user names a specific mailbox or shared inbox, to resolve the "
        "canonical email_address. Then pass that address as `mailbox_address` to "
        "`search_and_fetch_emails` or `create_email_draft` to scope the operation. Omitting "
        "`mailbox_address` on those tools targets the user's primary personal mailbox."
    ),
    annotations={"readOnlyHint": True},
)
async def list_available_mailboxes(
    ctx: Context,
    api_key: str = Depends(get_cloud_auth_token),
) -> str:
    """Fetches and displays all available mailboxes that the user has authorization to access."""
    await ctx.info("Listing available mailboxes from Adeu Cloud")
    url = f"{BACKEND_URL}/api/v1/users/me/shared-mailboxes"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            mailboxes = json.loads(response.read().decode("utf-8"))

            if not mailboxes:
                return "No mailboxes configured on Adeu Cloud."

            # Sort alphabetically by email for deterministic ordering across clients.
            mailboxes = sorted(mailboxes, key=lambda mb: (mb.get("email_address") or "").lower())

            lines = [
                "### Connected Mailboxes",
                (
                    "Below is the list of connected mailboxes you have access to. "
                    "Use the `email_address` as the `mailbox_address` parameter in other "
                    "tools to query or draft from a specific mailbox:"
                ),
                "",
            ]
            for mb in mailboxes:
                display_name = mb.get("display_name") or "Personal Mailbox"
                email = mb.get("email_address", "")
                auto_process = "Enabled" if mb.get("auto_process_enabled") else "Disabled"
                writeback = mb.get("write_back_preference", "INTERNAL")

                lines.append(
                    f"- **{display_name}**\n"
                    f"  - **Email Address**: `{email}`\n"
                    f"  - **Auto-Processing**: {auto_process}\n"
                    f"  - **Write-Back Mode**: `{writeback}`"
                )

            return "\n".join(lines)
    # FILE: python/src/adeu/mcp_components/tools/email.py

    except urllib.error.HTTPError as e:
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            raise ToolError("Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
        error_body = e.read().decode("utf-8")
        raise ToolError(_format_backend_error(e.code, error_body)) from e
    except TimeoutError as e:
        raise ToolError(
            "Listing mailboxes timed out after 15s. The Adeu backend may be temporarily unavailable; retry shortly."
        ) from e
    except Exception as e:
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e


@tool(
    description=(
        "Searches the user's live email inbox via the Adeu cloud backend.\n\n"
        "TWO MODES:\n"
        "1. Search mode (no `email_id`): returns up to `limit` lightweight previews. Use filters "
        "(`sender`, `subject`, `is_unread`, `days_ago`, `folder`, `has_attachments`, "
        "`attachment_name`) to narrow down.\n"
        "2. Fetch mode (with `email_id`): returns the full email body, thread history, and downloads "
        "attachments under `max_attachment_size_mb` to the local disk.\n\n"
        "AUTO-ESCALATION: If a search returns exactly one preview, the backend automatically fetches "
        "the full email in the same call. Plan around the response shape — check the `type` field "
        "(`previews` vs `full_email`) before assuming.\n\n"
        "EMAIL ID FORMATS (`email_id` parameter accepts any of):\n"
        "- `msg_<6 chars>` — short ID returned by previews on THIS machine. NOT portable across machines "
        "or sessions; the local cache holds the most recent 1000. If you reference one that's been evicted, "
        "the tool returns a StaleShortIdError telling you to re-search.\n"
        "- `adeu_<numeric>` — server-side reference for emails Adeu has previously processed. Portable "
        "across machines and sessions for the same authenticated user.\n"
        "- Raw provider ID (Gmail/Outlook native ID) — works if you have it, but you usually won't.\n\n"
        "FOLDER DEFAULT: omitting `folder` searches the Inbox only (matching what the user sees in their "
        "mail client). Use `folder='sent'` for sent items, `folder='all'` to include Deleted Items, "
        "Drafts, and other folders.\n\n"
        "ATTACHMENTS: attachments larger than `max_attachment_size_mb` (default 10) are listed in the "
        "response but NOT downloaded — raise the cap if you need them. Always set `working_directory` "
        "when calling from a project so attachments land alongside the user's other files. This directory "
        "path refers to the user's native operating system, not the LLM's sandbox environment."
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
        (
            "The mailbox folder to search in. Defaults to 'inbox' when omitted, "
            "which matches what the user sees in their mail client and excludes "
            "deleted items, drafts, and spam. Use 'sent' to search sent items. "
            "Use 'all' ONLY when the user explicitly asks to search across the "
            "entire mailbox including trash/deleted items."
        ),
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
    mailbox_address: Annotated[
        Optional[str],
        "Optional. The specific mailbox email address to search (e.g. 'sales@org.com'). "
        "Omit to use the user's primary mailbox.",
    ] = None,
    max_attachment_size_mb: Annotated[
        Optional[int],
        "Maximum attachment size in MB to download (default 10). Attachments larger than this "
        "are listed in the response but not downloaded. Raise this to fetch large files.",
    ] = None,
    task_id: Annotated[
        Optional[str],
        "If resuming a pending check, provide the task ID here.",
    ] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    await ctx.info(
        "Starting live email search",
        extra={
            "email_id": email_id,
            "subject": subject,
            "mailbox_address": mailbox_address,
            "task_id": task_id,
        },
    )

    data = None

    if task_id:
        # ==========================================
        # PHASE 2: POLL (Wait for completion)
        # ==========================================
        poll_url = f"{BACKEND_URL}/api/v1/emails/tasks/{task_id}"

        for attempt in range(10):
            req = urllib.request.Request(
                poll_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            )

            try:
                response = await asyncio.to_thread(urllib.request.urlopen, req)
                task_data = json.loads(response.read().decode("utf-8"))
                status = task_data.get("status")

                if status == "COMPLETED":
                    data = task_data
                    break

                if status == "FAILED":
                    error_msg = task_data.get("error", "Unknown internal error")
                    raise ToolError(f"Validation task failed on the server: {error_msg}")

                await ctx.debug(f"Task {task_id} status is {status}. Attempt {attempt + 1}/10. Sleeping 5s.")

            except urllib.error.HTTPError as e:
                if e.code == 401:
                    DesktopAuthManager.clear_api_key()
                    raise ToolError(
                        "Your authentication expired. Please call `login_to_adeu_cloud` to re-authenticate."
                    ) from e
                error_body = e.read().decode("utf-8")
                raise ToolError(f"Failed to check task status (HTTP {e.code}): {error_body}") from e
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(f"Unexpected error checking task status: {str(e)}") from e

            await asyncio.sleep(5)
        else:
            msg = (
                f"Task {task_id} is still processing. "
                + f"Please call `search_and_fetch_emails` again with task_id={task_id}."
            )
            return ToolResult(content=msg, structured_content={"status": "pending", "task_id": task_id, "message": msg})

    else:
        # ==========================================
        # PHASE 1: INIT / SEARCH (Search/Fetch standard)
        # ==========================================
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
            "mailbox_address": mailbox_address,
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
            response = await asyncio.to_thread(urllib.request.urlopen, req)
            data = json.loads(response.read().decode("utf-8"))

            status_code = getattr(response, "status", response.getcode())
            if status_code == 202 or (
                isinstance(data, dict) and (data.get("status") == "pending" or "task_id" in data) and "type" not in data
            ):
                new_task_id = data.get("task_id")
                msg = (
                    f"Email processing task started successfully. Task ID: {new_task_id}. "
                    f"Please call `search_and_fetch_emails` again immediately with "
                    f"task_id={new_task_id} to monitor the progress."
                )
                await ctx.info(f"Task started: {new_task_id}")
                return ToolResult(
                    content=msg, structured_content={"status": "pending", "task_id": str(new_task_id), "message": msg}
                )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                DesktopAuthManager.clear_api_key()
                raise ToolError("Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
            error_body = e.read().decode("utf-8")
            raise ToolError(_format_backend_error(e.code, error_body)) from e
        except TimeoutError as e:
            raise ToolError(
                "Email search timed out after 45s. The mail provider (Outlook/Gmail) may be slow. "
                "Try narrowing the search with more filters (sender, subject, days_ago), or retry shortly."
            ) from e
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

        save_id_cache(id_cache)

        llm_lines.append(
            "⚠️ **ACTION REQUIRED**: To read the full body of an email and download its attachments to the local disk, "
            "you must call this tool again and provide the exact `email_id` of the message you want to open.\n"
            f"*(If you need to see more results, call this tool again with `offset={offset + limit}`)*"
        )
        return ToolResult(content="\n".join(llm_lines), structured_content=data)

    # FILE: python/src/adeu/mcp_components/tools/email.py

    # ==========================================
    # SCENARIO B: FULL EMAIL (Single Email Drill-down)
    # ==========================================
    elif response_type == "full_email":
        full_email = data.get("full_email", {})
        if not full_email:
            return ToolResult(content="Failed to retrieve full email.", structured_content=data)

        # Detect auto-escalation: the caller asked for previews (no email_id) but
        # the backend found exactly one match and returned a full email instead.
        # Flag it so the agent doesn't get blindsided by a wall of body text when
        # it asked for a list.
        auto_escalated = email_id is None and any(
            v is not None
            for v in (
                sender,
                subject,
                has_attachments,
                attachment_name,
                is_unread,
                days_ago,
                folder,
            )
        )

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

        effective_cap_mb = (
            max_attachment_size_mb if (isinstance(max_attachment_size_mb, int) and max_attachment_size_mb > 0) else 10
        )
        max_bytes = effective_cap_mb * 1024 * 1024

        async def process_message_attachments(
            message_data: dict,
        ) -> tuple[list[str], list[dict]]:
            local_files: list[str] = []
            skipped: list[dict] = []

            for att in message_data.get("attachments", []):
                filename = att.get("filename", "unnamed_file")
                size_bytes = att.get("size_bytes")

                # Size cap: skip download but record for the agent
                if isinstance(size_bytes, int) and size_bytes > max_bytes:
                    skipped.append(
                        {
                            "filename": filename,
                            "size_bytes": size_bytes,
                            "reason": f"exceeds {effective_cap_mb} MB cap",
                        }
                    )
                    # Drop the payload from the structured response too
                    att.pop("base64_data", None)
                    continue

                b64_data = att.pop("base64_data", None)
                if b64_data:
                    try:
                        file_path = _get_unique_filepath(save_dir, filename)
                        file_path.write_bytes(base64.b64decode(b64_data))
                        local_files.append(str(file_path))
                        att["local_path"] = str(file_path)
                    except Exception as e:
                        await ctx.warning(f"Failed to save attachment {filename}: {e}")
                        skipped.append(
                            {
                                "filename": filename,
                                "size_bytes": size_bytes,
                                "reason": f"download failed: {e}",
                            }
                        )

            return local_files, skipped

        llm_lines = []
        if auto_escalated:
            llm_lines.append("_(Search returned exactly one result; auto-fetched full email below.)_\n")
        llm_lines.append(f"# Email Thread: {full_email.get('subject')}")
        llm_lines.append("")

        target_local_files, target_skipped = await process_message_attachments(full_email)
        raw_clean_body = strip_tags(full_email.get("body_html", ""))
        clean_body = remove_nested_quotes(raw_clean_body)

        llm_lines.append("## Target Message (Newest):")
        llm_lines.append(f"**From**: {full_email.get('sender_name')} <{full_email.get('sender_email')}>")
        llm_lines.append(f"**Date**: {full_email.get('received_datetime')}")

        if target_local_files:
            llm_lines.append("**Attachments Saved Locally**:")
            for path in target_local_files:
                llm_lines.append(f"- 📎 `{path}`")

        if target_skipped:
            llm_lines.append(
                f"**Attachments Skipped (not downloaded)** — pass `max_attachment_size_mb` "
                f"to raise the {effective_cap_mb} MB cap:"
            )
            for s in target_skipped:
                llm_lines.append(f"- ⚠️ `{s['filename']}` ({_format_bytes(s['size_bytes'])}, {s['reason']})")

        llm_lines.append(f"**Body**:\n```\n{clean_body}\n```\n")

        brief_html = full_email.get("brief_content")
        if brief_html:
            clean_brief = strip_tags(brief_html).strip()
            if clean_brief:
                llm_lines.append("## 🧠 AI Strategy Brief (Previously Generated):")
                llm_lines.append(f"```\n{clean_brief}\n```\n")
                llm_lines.append(
                    "*This brief was previously generated by Adeu for this email. "
                    "It reflects the AI's analysis at the time of processing.*\n"
                )

        if full_email.get("is_thread") and full_email.get("messages"):
            llm_lines.append("## Previous Messages in Thread (Historical Context):")
            for idx, hist_msg in enumerate(full_email.get("messages", [])):
                hist_local_files, hist_skipped = await process_message_attachments(hist_msg)

                raw_clean_hist = strip_tags(hist_msg.get("body_html", ""))
                clean_hist = remove_nested_quotes(raw_clean_hist)

                llm_lines.append(f"### Message {-1 * (idx + 1)} (Older)")
                llm_lines.append(f"**From**: {hist_msg.get('sender_name')} <{hist_msg.get('sender_email')}>")
                llm_lines.append(f"**Date**: {hist_msg.get('received_datetime')}")

                if hist_local_files:
                    llm_lines.append("**Attachments Saved Locally**:")
                    for path in hist_local_files:
                        llm_lines.append(f"- 📎 `{path}`")

                if hist_skipped:
                    llm_lines.append(
                        f"**Attachments Skipped (not downloaded)** — pass `max_attachment_size_mb` "
                        f"to raise the {effective_cap_mb} MB cap:"
                    )
                    for s in hist_skipped:
                        llm_lines.append(f"- ⚠️ `{s['filename']}` ({_format_bytes(s['size_bytes'])}, {s['reason']})")

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
        "Creates an email draft in the user's native draft box (Outlook Drafts or Gmail Drafts).\n\n"
        "TWO MODES:\n"
        "1. Reply mode: pass `reply_to_email_id` to create a threaded reply. The draft inherits subject, "
        "recipients, and threading headers from the original — do NOT pass `subject` or `to_recipients`.\n"
        "2. New email mode: omit `reply_to_email_id` and pass BOTH `subject` and `to_recipients`.\n\n"
        "`reply_to_email_id` accepts the same ID formats as search_and_fetch_emails (`msg_*` short IDs, "
        "`adeu_*` references, or raw provider IDs). Short IDs are validated against the local cache before "
        "the call; stale ones fail fast with a clear error telling you to re-search.\n\n"
        "`body_markdown` is converted server-side to styled HTML with inlined CSS for email-client "
        "compatibility. Write the body in plain Markdown — do not pre-render HTML.\n\n"
        "`attachment_paths` takes absolute file paths on the user's local disk and uploads them with the "
        "draft. Useful right after search_and_fetch_emails downloaded attachments — those local paths can "
        "be passed directly here."
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
    mailbox_address: Annotated[
        Optional[str],
        "Optional. The specific mailbox email address to draft the email from (e.g. 'sales@org.com').",
    ] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    if not reply_to_email_id and (not subject or not to_recipients):
        return ToolResult(
            "Error: You must provide either 'reply_to_email_id' (to reply) OR "
            "both 'subject' and 'to_recipients' (to start a new email).",
        )

    await ctx.info(
        "Creating email draft",
        extra={
            "reply_to": reply_to_email_id,
            "subject": subject,
            "mailbox_address": mailbox_address,
        },
    )
    url = f"{BACKEND_URL}/api/v1/emails/drafts/new"

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

    real_reply_to_id = resolve_email_id(reply_to_email_id) if reply_to_email_id else None

    fields = {
        "body_markdown": body_markdown,
    }
    if real_reply_to_id:
        fields["reply_to_email_id"] = real_reply_to_id
    if subject:
        fields["subject"] = subject
    if parsed_recipients:
        fields["to_recipients"] = json.dumps(parsed_recipients)
    if mailbox_address:
        fields["mailbox_address"] = mailbox_address

    files_to_upload = []
    if parsed_attachments:
        for path in parsed_attachments:
            try:
                file_bytes = read_file_bytes(path).getvalue()
                filename = Path(path).name
                files_to_upload.append(("files", filename, file_bytes))
            except Exception as e:
                raise ToolError(f"Failed to read attachment {path}: {e}") from e

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
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
            draft_id = data.get("id")
            return ToolResult(content=f"Successfully created email draft! Draft ID: {draft_id}")
    # FILE: python/src/adeu/mcp_components/tools/email.py

    except urllib.error.HTTPError as e:
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            raise ToolError("Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
        error_body = e.read().decode("utf-8")
        raise ToolError(_format_backend_error(e.code, error_body)) from e
    except TimeoutError as e:
        raise ToolError(
            "Draft creation timed out after 90s. If the draft includes large attachments, "
            "try splitting them across multiple drafts or omitting the largest files."
        ) from e
    except Exception as e:
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e
