import json
import logging
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path
from typing import Annotated, List, Optional

import structlog
from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.apps import AppConfig
from fastmcp.tools import ToolResult

from adeu.auth import DesktopAuthManager
from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentChange, ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine

BACKEND_URL = os.environ.get("ADEU_BACKEND_URL", "https://app.adeu.ai")
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

to_client_logger = logging.getLogger("fastmcp.server.context.to_client")
to_client_logger.setLevel(level=logging.DEBUG)

mcp = FastMCP("Adeu Redlining Service")

VIEW_URI = "ui://adeu/html-viewer"


@mcp.resource(VIEW_URI, app=AppConfig())
def html_viewer() -> str:
    """Interactive HTML Viewer App."""
    return """\
<!DOCTYPE html>
<html>
<head>
  <meta name="color-scheme" content="light dark">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; line-height: 1.6; color: #333; margin: 0; background: transparent; }
    @media (prefers-color-scheme: dark) { body { color: #eee; } }
    h1, h2, h3, h4 { margin-top: 0; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: bold; margin-left: 10px; vertical-align: middle; }
    .badge-success { background: #d4edda; color: #155724; }
    .badge-warning { background: #fff3cd; color: #856404; }
    .badge-danger { background: #f8d7da; color: #721c24; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
    @media (prefers-color-scheme: dark) { .card { border-color: #444; } }
    .evidence { border-left: 3px solid #ccc; padding-left: 10px; margin-top: 10px; font-size: 0.9em; font-style: italic; }
  </style>
</head>
<body>
  <div id="app">Loading report...</div>
  <script>
    const INIT_ID = 1;

    window.addEventListener("message", (event) => {
      if (!event.data || event.data.jsonrpc !== "2.0") return;
      const msg = event.data;

      // 1. Handle Initialization Response from Host
      if (msg.id === INIT_ID) {
        window.parent.postMessage({
          jsonrpc: "2.0",
          method: "ui/notifications/initialized",
          params: {}
        }, "*");
        return;
      }

      // 2. Handle Tool Result Injection
      if (msg.method === "ui/notifications/tool-result") {
        const result = msg.params;
        if (result.structuredContent && result.structuredContent.html) {
          document.getElementById('app').innerHTML = result.structuredContent.html;
        } else if (result.content) {
          const txt = result.content.find(c => c.type === 'text');
          if (txt) document.getElementById('app').textContent = txt.text;
        }
      }
    });

    // 3. Auto-resize iframe when content changes
    const observer = new ResizeObserver(() => {
      window.parent.postMessage({
        jsonrpc: "2.0",
        method: "ui/notifications/size-changed",
        params: { height: Math.min(Math.ceil(document.documentElement.getBoundingClientRect().height), 400), width: 600 }
      }, "*");
    });
    observer.observe(document.documentElement);
    observer.observe(document.body);

    // 0. Start Handshake
    window.parent.postMessage({
      jsonrpc: "2.0",
      id: INIT_ID,
      method: "ui/initialize",
      params: {
        appInfo: { name: "Adeu Viewer", version: "1.0.0" },
        appCapabilities: {},
        protocolVersion: "2025-11-21"
      }
    }, "*");
  </script>
</body>
</html>"""


def get_cloud_auth_token() -> str:
    """Dependency to enforce cloud authentication before tool execution."""
    api_key = DesktopAuthManager.get_api_key()
    if not api_key:
        raise ToolError(
            "Authentication Required: You are not logged in. "
            "Please call the `login_to_adeu_cloud` tool first to authenticate, "
            "then try this task again."
        )
    return api_key


def _read_file_bytes(path: str) -> BytesIO:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(p, "rb") as f:
        return BytesIO(f.read())


def _save_stream(stream: BytesIO, path: str):
    with open(path, "wb") as f:
        f.write(stream.getvalue())


@mcp.tool(
    description=(
        "Reads a DOCX file and returns its text content. Use this to ingest the document into your context window."
    )
)
async def read_docx(
    file_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    clean_view: Annotated[
        bool,
        "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
    ] = False,
) -> str:
    await ctx.info(
        f"Reading DOCX file: {Path(file_path).name}",
        extra={"file_path": file_path, "clean_view": clean_view},
    )

    try:
        stream = _read_file_bytes(file_path)
        await ctx.debug(
            "File bytes read successfully into memory",
            extra={"size_bytes": len(stream.getvalue())},
        )

        text = extract_text_from_stream(stream, filename=Path(file_path).name, clean_view=clean_view)
        await ctx.info("Successfully extracted text from DOCX", extra={"text_length": len(text)})
        return text

    except FileNotFoundError as e:
        await ctx.error("File not found", extra={"file_path": file_path})
        return f"Error reading file: {str(e)}"
    except Exception as e:
        await ctx.error("Failed to parse DOCX", extra={"error": str(e), "file_path": file_path})
        return f"Error reading file: {str(e)}"


@mcp.tool(description="Compares two DOCX files and returns a text-based Unified Diff.")
async def diff_docx_files(
    original_path: Annotated[str, "Path to the base document."],
    modified_path: Annotated[str, "Path to the new document."],
    ctx: Context,
    compare_clean: Annotated[bool, "If True, compares 'Accepted' state. If False, compares raw text."] = True,
) -> str:
    await ctx.info(
        "Starting document diff",
        extra={
            "original_path": original_path,
            "modified_path": modified_path,
            "compare_clean": compare_clean,
        },
    )

    try:
        await ctx.debug("Extracting text from original document")
        stream_orig = _read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name, clean_view=compare_clean)

        await ctx.debug("Extracting text from modified document")
        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name, clean_view=compare_clean)

        await ctx.debug("Generating text differences")
        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            await ctx.warning("No text differences found between the documents.")
            return "No text differences found between the documents."

        await ctx.info(f"Diff complete. Found {len(edits)} differences.")
        return _create_diff_output(original_path, modified_path, text_orig, edits)

    except Exception as e:
        await ctx.error("Failed to compute diff", extra={"error": str(e)})
        return f"Error computing diff: {str(e)}"


def _create_diff_output(original_path: str, modified_path: str, text_orig: str, edits: List[ModifyText]):
    output = [
        f"--- {Path(original_path).name}",
        f"+++ {Path(modified_path).name}",
        "",
    ]
    CONTEXT_SIZE = 40

    for edit in edits:
        start_idx = getattr(edit, "_match_start_index", 0) or 0
        pre_start = max(0, start_idx - CONTEXT_SIZE)
        pre_context = text_orig[pre_start:start_idx]
        if pre_start > 0:
            pre_context = "..." + pre_context

        target_len = len(edit.target_text) if edit.target_text else 0
        post_start = start_idx + target_len
        post_end = min(len(text_orig), post_start + CONTEXT_SIZE)
        post_context = text_orig[post_start:post_end]
        if post_end < len(text_orig):
            post_context = post_context + "..."

        pre_context = pre_context.replace("\n", " ").replace("\r", "")
        post_context = post_context.replace("\n", " ").replace("\r", "")

        output.append("@@ Word Patch @@")
        output.append(f" {pre_context}")
        if edit.target_text:
            output.append(f"- {edit.target_text}")
        if edit.new_text:
            output.append(f"+ {edit.new_text}")
        output.append(f" {post_context}")
        output.append("")
    result = "\n".join(output)
    return result


@mcp.tool(description="Applies a batch of document changes (review actions and text edits).")
async def process_document_batch(
    original_docx_path: Annotated[str, "Absolute path to the source file."],
    author_name: Annotated[str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."],
    ctx: Context,
    changes: Annotated[
        List[DocumentChange],
        "List of changes to apply. Each change must specify 'type' as 'accept', 'reject', 'reply', or 'modify'.",
    ],
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    await ctx.info(
        "Initializing atomic batch process",
        extra={
            "original_docx_path": original_docx_path,
            "author_name": author_name,
            "changes_count": len(changes) if changes else 0,
        },
    )

    try:
        if not author_name or not author_name.strip():
            await ctx.warning("Batch processing rejected: author_name is empty.")
            return "Error: author_name cannot be empty."

        if not changes:
            await ctx.warning("Batch processing rejected: No actions or edits provided.")
            return "Error: No changes provided."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        await ctx.debug("Redline Engine initialized successfully")

        try:
            await ctx.debug("Processing document batch")
            stats = engine.process_batch(changes)
            await ctx.info("Changes processed successfully", extra=stats)
        except BatchValidationError as e:
            await ctx.error(
                "Batch validation failed",
                extra={
                    "error_count": len(e.errors),
                    "errors": e.errors,
                },
            )
            error_report = "Batch rejected. Some edits failed validation:\n\n" + "\n\n".join(e.errors)
            return error_report

        if not output_path:
            p = Path(original_docx_path)
            if p.stem.endswith("_processed") or p.stem.endswith("_redlined"):
                output_path = str(p)
            else:
                output_path = str(p.parent / f"{p.stem}_processed{p.suffix}")

        await ctx.debug(
            "Saving processed document stream to disk",
            extra={"output_path": output_path},
        )
        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)

        await ctx.info("Batch process complete and saved", extra={"output_path": output_path})

        return (
            f"Batch complete. Saved to: {output_path}\n"
            f"Actions: {stats['actions_applied']} applied, {stats['actions_skipped']} skipped.\n"
            f"Edits: {stats['edits_applied']} applied, {stats['edits_skipped']} skipped."
        )

    except Exception as e:
        await ctx.error("Critical error during batch processing", extra={"error": str(e)})
        return f"Error processing batch: {str(e)}"


@mcp.tool(description="Accepts all tracked changes in the document and removes comments, creating a clean version.")
async def accept_all_changes(
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    ctx: Context,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    await ctx.info(f"Accepting all changes for document: {Path(docx_path).name}")
    try:
        stream = _read_file_bytes(docx_path)
        engine = RedlineEngine(stream)

        await ctx.debug("Engine loaded, executing accept_all_revisions()")
        engine.accept_all_revisions()

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_clean{p.suffix}")

        _save_stream(engine.save_to_stream(), output_path)
        await ctx.info("Clean document saved successfully", extra={"output_path": output_path})

        return f"Accepted all changes. Saved to: {output_path}"
    except Exception as e:
        await ctx.error(
            "Failed to accept all changes",
            extra={"error": str(e), "docx_path": docx_path},
        )
        return f"Error accepting changes: {str(e)}"


@mcp.tool(description="Logs the user into the Adeu Cloud backend. Securely opens a browser window for authentication.")
async def login_to_adeu_cloud(ctx: Context) -> str:
    await ctx.info("Initiating cloud authentication workflow")
    try:
        await ctx.debug("Checking DesktopAuthManager for API key")
        api_key = DesktopAuthManager.ensure_authenticated()
        if not api_key:
            await ctx.error("Failed to obtain API key from login flow")
            raise ToolError("Error: Could not obtain API key from the login flow.")

        url = f"{BACKEND_URL}/api/v1/auth/me"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

        try:
            await ctx.debug("Verifying token with backend", extra={"url": url})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))
                email = data.get("email", "Unknown Email")

                await ctx.info(
                    "Login successful",
                    extra={"email": email},
                )
                return f"Login successful! Connected to Adeu Cloud as: {email}."

        except urllib.error.HTTPError as e:
            if e.code == 401:
                await ctx.warning("Session expired or invalid token. Clearing API key.")
                DesktopAuthManager.clear_api_key()
                raise ToolError(
                    "Your previous session expired. The stale key has been cleared. "
                    "Please call the `login_to_adeu_cloud` tool ONE MORE TIME to log in fresh."
                ) from e
            await ctx.error(
                "HTTP Error verifying login",
                extra={"status_code": e.code, "reason": e.reason},
            )
            raise ToolError(f"HTTP Error verifying login: {e.code} - {e.reason}") from e

    except Exception as e:
        await ctx.error("Exception during login process", extra={"error": str(e)})
        raise ToolError(f"Error during login process: {str(e)}") from e


@mcp.tool(description="Logs out of the Adeu Cloud backend by clearing the local API key from the OS Keychain.")
async def logout_of_adeu_cloud(ctx: Context) -> str:
    await ctx.info("Initiating cloud session logout")
    try:
        DesktopAuthManager.clear_api_key()
        await ctx.debug("API key cleared from OS Keychain successfully")
        return "Successfully logged out. The local API key has been removed from the Keychain."
    except Exception as e:
        await ctx.error("Failed to clear API key during logout", extra={"error": str(e)})
        raise ToolError(f"Error during logout: {str(e)}") from e


def _encode_multipart_formdata(
    files: List[tuple[str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    buffer = BytesIO()

    for field_name, file_name, file_bytes in files:
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'.encode("utf-8"))
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        buffer.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        buffer.write(file_bytes)
        buffer.write(b"\r\n")

    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"


@mcp.tool(
    description=(
        "Analyzes documents to find inconsistencies, contradictions, and risk assessments. "
        "Always present the complete report to the user, including every verbatim evidence quote "
        "exactly as returned, without summarizing or omitting any findings."
        "Run this on validation request for files or directories."
    ),
    app=AppConfig(resource_uri=VIEW_URI),
)
async def validate_documents(
    file_paths: Annotated[List[str], "List of absolute paths to documents (DOCX, PDF) OR directories."],
    ctx: Context,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    await ctx.info("Starting document validation", extra={"provided_paths": file_paths})

    if not file_paths:
        await ctx.warning("No file paths provided by client")
        raise ToolError("You must provide at least 1 file path or directory to perform document validation.")

    resolved_files: list[Path] = []
    valid_extensions = {".docx", ".pdf"}

    await ctx.debug("Resolving files and directories")
    for path_str in file_paths:
        p = Path(path_str)
        if not p.exists():
            await ctx.error("Path not found on disk", extra={"missing_path": path_str})
            raise ToolError(f"Path not found on local disk: {path_str}")

        if p.is_dir():
            for child in p.iterdir():
                if child.is_file() and child.suffix.lower() in valid_extensions:
                    resolved_files.append(child)
        elif p.is_file():
            if p.suffix.lower() not in valid_extensions:
                await ctx.warning("Unsupported file type skipped", extra={"file": path_str})
                raise ToolError(f"Unsupported file type for {path_str}. Only .docx and .pdf are supported.")
            resolved_files.append(p)

    resolved_files = list(set(resolved_files))

    if not resolved_files:
        await ctx.error("No valid documents found in provided paths")
        raise ToolError("No supported documents (.docx or .pdf) were found in the provided paths.")

    await ctx.info(
        f"Resolved {len(resolved_files)} file(s) for validation",
        extra={"files": [p.name for p in resolved_files]},
    )

    files_data = []
    for p in resolved_files:
        try:
            with open(p, "rb") as f:
                files_data.append(("files", p.name, f.read()))
        except Exception as e:
            await ctx.error("Failed to read file", extra={"filename": p.name, "error": str(e)})
            raise ToolError(f"Failed to read file {p.name}: {str(e)}") from e

    await ctx.debug("Encoding multipart/form-data payload")
    body, content_type = _encode_multipart_formdata(files_data)
    url = f"{BACKEND_URL}/api/v1/documents/validate"

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
        await ctx.info(
            "Sending validation request to Adeu Cloud",
            extra={"url": url, "payload_size_bytes": len(body)},
        )
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))

            await ctx.debug("Received successful response from cloud API")

            consistency = data.get("consistency_result", {})
            risk = data.get("risk_assessment_result", {})

            await ctx.info(
                "Validation Complete",
                extra={
                    "inconsistency_issues_found": len(consistency.get("issues", [])),
                    "total_risks_found": len(risk.get("risks", [])),
                },
            )

            def format_evidence(evidence_list):
                if not evidence_list:
                    return ""
                ev_lines = ["\n**Verbatim Evidence**:"]
                for ev in evidence_list:
                    if isinstance(ev, dict):
                        quote = ev.get("quote", str(ev))
                        doc = ev.get("filename", "Unknown Document")
                        ev_lines.append(f'> "{quote}" — *{doc}*')
                    else:
                        ev_lines.append(f"> {ev}")
                return "\n".join(ev_lines) + "\n"

            output = [
                "# Validation Report\n",
                "## 1. Consistency Check",
                f"**Summary**: {consistency.get('summary', 'No summary provided.')}\n",
            ]

            issues = consistency.get("issues", [])
            if not issues:
                output.append("No inconsistencies found! The document(s) appear to be structurally aligned.\n")
            else:
                for i, issue in enumerate(issues, 1):
                    output.append(f"### {i}. [{issue.get('severity')}] {issue.get('title')}")
                    output.append(f"**Description**: {issue.get('description')}")
                    output.append(format_evidence(issue.get("evidence", [])))

            output.append("## 2. Buyer vs. Seller Risk Assessment")
            output.append(f"**Summary**: {risk.get('summary', 'No summary provided.')}\n")

            def format_risk_section(section_title, risk_items):
                if not risk_items:
                    return f"### {section_title}\nNo specific risks identified.\n"
                section = [f"### {section_title}\n"]
                for item in risk_items:
                    section.append(f"#### {item.get('title')}")
                    section.append(f"**Description**: {item.get('description')}")
                    output.append(format_evidence(item.get("evidence", [])))
                return "\n".join(section)

            all_risks = risk.get("risks", [])
            buyer_risks = [r for r in all_risks if r.get("party") == "BUYER"]
            seller_risks = [r for r in all_risks if r.get("party") == "SELLER"]

            output.append(format_risk_section("Buyer-Side Risks", buyer_risks))
            output.append(format_risk_section("Seller-Side Risks", seller_risks))

            # --- Build Custom HTML App View ---
            html_parts = []
            html_parts.append("<h1>Validation Report</h1>")

            html_parts.append("<h2>1. Consistency Check</h2>")
            html_parts.append(f"<p><strong>Summary:</strong> {consistency.get('summary', 'No summary provided.')}</p>")

            if not issues:
                html_parts.append(
                    '<div class="badge badge-success" style="margin-bottom: 20px;">No inconsistencies found! Structurally aligned.</div>'
                )
            else:
                for i, issue in enumerate(issues, 1):
                    severity = issue.get("severity", "Unknown")
                    badge_class = "badge-danger" if severity.lower() in ["high", "critical"] else "badge-warning"

                    html_parts.append('<div class="card">')
                    html_parts.append(
                        f'<h3>{i}. {issue.get("title")} <span class="badge {badge_class}">{severity}</span></h3>'
                    )
                    html_parts.append(f"<p>{issue.get('description')}</p>")

                    if issue.get("evidence"):
                        html_parts.append('<div class="evidence"><strong>Verbatim Evidence:</strong><br>')
                        for ev in issue.get("evidence"):
                            if isinstance(ev, dict):
                                html_parts.append(
                                    f"&quot;{ev.get('quote', str(ev))}&quot; &mdash; {ev.get('filename', 'Unknown')}<br>"
                                )
                            else:
                                html_parts.append(f"{ev}<br>")
                        html_parts.append("</div>")
                    html_parts.append("</div>")

            html_parts.append('<h2 style="margin-top: 30px;">2. Buyer vs. Seller Risk Assessment</h2>')
            html_parts.append(f"<p><strong>Summary:</strong> {risk.get('summary', 'No summary provided.')}</p>")

            def render_ui_risk_section(title: str, items: list):
                html_parts.append(f"<h3>{title}</h3>")
                if not items:
                    html_parts.append('<p style="color: #666; font-style: italic;">No specific risks identified.</p>')
                else:
                    for item in items:
                        html_parts.append('<div class="card">')
                        html_parts.append(f"<h4>{item.get('title')}</h4>")
                        html_parts.append(f"<p>{item.get('description')}</p>")
                        if item.get("evidence"):
                            html_parts.append('<div class="evidence">')
                            for ev in item.get("evidence"):
                                if isinstance(ev, dict):
                                    html_parts.append(
                                        f"&quot;{ev.get('quote', str(ev))}&quot; &mdash; {ev.get('filename', 'Unknown')}<br>"
                                    )
                                else:
                                    html_parts.append(f"{ev}<br>")
                            html_parts.append("</div>")
                        html_parts.append("</div>")

            render_ui_risk_section("Buyer-Side Risks", buyer_risks)
            render_ui_risk_section("Seller-Side Risks", seller_risks)

            generated_html = "".join(html_parts)

            markdown_output = "\n".join(output)
            return ToolResult(content=markdown_output, structured_content={"html": generated_html})

    except urllib.error.HTTPError as e:
        if e.code == 401:
            await ctx.warning("Cloud authentication expired during validation")
            DesktopAuthManager.clear_api_key()
            raise ToolError("Your authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.") from e
        elif e.code == 403:
            await ctx.warning("Authorization Error: User lacks access to this tool")
            raise ToolError("Authorization Error: You do not have access to use this tool.") from e

        error_body = e.read().decode("utf-8")
        await ctx.error(
            "Cloud validation API failure",
            extra={"status_code": e.code, "body": error_body},
        )
        raise ToolError(f"Cloud analysis failed (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        await ctx.error("Unexpected error communicating with Adeu Cloud", extra={"error": str(e)})
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e


def main():
    mcp.run()


if __name__ == "__main__":
    main()
