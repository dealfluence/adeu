# FILE: src/adeu/server.py
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

import jinja2
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


templates_dir = Path(__file__).parent / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(templates_dir),
    variable_start_string="[[",
    variable_end_string="]]",
)


def _get_marked_js_content() -> str:
    """Reads the bundled marked.min.js file from the assets directory."""
    asset_path = Path(__file__).parent / "assets" / "marked.min.js"
    if asset_path.exists():
        with open(asset_path, "r", encoding="utf-8") as f:
            return f.read()
    return f"window.__MARKED_ERROR = 'File not found at: {asset_path}';"


@mcp.resource(VIEW_URI, app=AppConfig())
def html_viewer() -> str:
    """Interactive HTML Viewer App using standard Markdown."""
    marked_js_code = _get_marked_js_content()
    template = jinja_env.get_template("viewer.html")
    return template.render(marked_js_code=marked_js_code)


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


def _make_cloud_request(url: str, body: bytes, headers: dict) -> dict:
    """Helper to run the blocking request in a background thread."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


@mcp.tool(
    description=(
        "Analyzes documents to find inconsistencies, contradictions, and risk assessments. "
        "Always present the complete report to the user, including every verbatim evidence quote "
        "exactly as returned, without summarizing or omitting any findings."
        "Run this on validation request for files or directories."
    ),
    app=AppConfig(resourceUri=VIEW_URI),
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

            # The backend now provides the fully formatted markdown report
            markdown_report = data.get("report_markdown", "No report generated.")

            return ToolResult(
                content=markdown_report,
                structured_content={"markdown": markdown_report},
            )

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
