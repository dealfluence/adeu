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
import logging
from adeu.auth import DesktopAuthManager
from adeu.diff import generate_edits_from_text
from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown as _apply_edits_to_markdown
from adeu.models import DocumentEdit, ReviewAction
from adeu.redline.engine import RedlineEngine
import json
import urllib.request
import urllib.error
from adeu.auth import DesktopAuthManager

BACKEND_URL = os.environ.get("ADEU_BACKEND_URL", "https://app.adeu.ai")
# --- LOGGING CONFIGURATION ---
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

# Enable debug logging on the server-side to see client logs locally in stderr too
to_client_logger = logging.getLogger("fastmcp.server.context.to_client")
to_client_logger.setLevel(level=logging.DEBUG)

mcp = FastMCP("Adeu Redlining Service")


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

        text = extract_text_from_stream(
            stream, filename=Path(file_path).name, clean_view=clean_view
        )
        await ctx.info(
            "Successfully extracted text from DOCX", extra={"text_length": len(text)}
        )
        return text

    except FileNotFoundError as e:
        await ctx.error("File not found", extra={"file_path": file_path})
        return f"Error reading file: {str(e)}"
    except Exception as e:
        await ctx.error(
            "Failed to parse DOCX", extra={"error": str(e), "file_path": file_path}
        )
        return f"Error reading file: {str(e)}"


@mcp.tool(description="Compares two DOCX files and returns a text-based Unified Diff.")
async def diff_docx_files(
    original_path: Annotated[str, "Path to the base document."],
    modified_path: Annotated[str, "Path to the new document."],
    ctx: Context,
    compare_clean: Annotated[
        bool, "If True, compares 'Accepted' state. If False, compares raw text."
    ] = True,
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
        text_orig = extract_text_from_stream(
            stream_orig, filename=Path(original_path).name, clean_view=compare_clean
        )

        await ctx.debug("Extracting text from modified document")
        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(
            stream_mod, filename=Path(modified_path).name, clean_view=compare_clean
        )

        await ctx.debug("Generating text differences")
        edits = generate_edits_from_text(text_orig, text_mod)

        if not edits:
            await ctx.warning("No text differences found between the documents.")
            return "No text differences found between the documents."

        await ctx.info(f"Diff complete. Found {len(edits)} differences.")

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

        return "\n".join(output)

    except Exception as e:
        await ctx.error("Failed to compute diff", extra={"error": str(e)})
        return f"Error computing diff: {str(e)}"


@mcp.tool(
    description="ATOMIC PIPELINE: Applies a mixed batch of review actions (ACCEPT/REJECT/REPLY) and text edits."
)
async def process_document_batch(
    original_docx_path: Annotated[str, "Absolute path to the source file."],
    author_name: Annotated[
        str, "Name to appear in Track Changes (e.g., 'Reviewer AI')."
    ],
    ctx: Context,
    actions: Annotated[
        Optional[List[ReviewAction]],
        "Optional list of review actions (ACCEPT, REJECT, REPLY)",
    ] = None,
    edits: Annotated[
        Optional[List[DocumentEdit]], "Optional list of text replacements"
    ] = None,
    output_path: Annotated[Optional[str], "Optional output path."] = None,
) -> str:
    await ctx.info(
        "Initializing atomic batch process",
        extra={
            "original_docx_path": original_docx_path,
            "author_name": author_name,
            "actions_count": len(actions) if actions else 0,
            "edits_count": len(edits) if edits else 0,
        },
    )

    try:
        if not author_name or not author_name.strip():
            await ctx.warning("Batch processing rejected: author_name is empty.")
            return "Error: author_name cannot be empty."

        actions = actions or []
        edits = edits or []

        if not actions and not edits:
            await ctx.warning(
                "Batch processing rejected: No actions or edits provided."
            )
            return "Error: No actions or edits provided."

        stream = _read_file_bytes(original_docx_path)
        engine = RedlineEngine(stream, author=author_name)
        await ctx.debug("Redline Engine initialized successfully")

        applied_actions, skipped_actions = 0, 0
        if actions:
            await ctx.debug("Applying structural review actions")
            applied_actions, skipped_actions = engine.apply_review_actions(actions)
            await ctx.info(
                "Review actions processed",
                extra={"applied": applied_actions, "skipped": skipped_actions},
            )

            # CRITICAL: Rebuild the mapper so text edits anchor against the post-action DOM state
            if edits:
                await ctx.debug("Rebuilding Virtual DOM mapper for text edits")
                engine.mapper._build_map()
                engine.clean_mapper = None

        if edits:
            await ctx.debug("Validating text edits")
            validation_errors = engine.validate_edits(edits)
            if validation_errors:
                await ctx.error(
                    "Edit validation failed",
                    extra={
                        "error_count": len(validation_errors),
                        "errors": validation_errors,
                    },
                )
                error_report = (
                    f"Batch rejected. {len(validation_errors)} out of {len(edits)} edits failed validation:\n\n"
                    + "\n\n".join(validation_errors)
                )
                return error_report

        applied_edits, skipped_edits = 0, 0
        if edits:
            await ctx.debug("Applying text edits to document")
            applied_edits, skipped_edits = engine.apply_edits(edits)
            await ctx.info(
                "Text edits processed",
                extra={"applied": applied_edits, "skipped": skipped_edits},
            )

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

        await ctx.info(
            "Batch process complete and saved", extra={"output_path": output_path}
        )

        return (
            f"Batch complete. Saved to: {output_path}\n"
            f"Actions: {applied_actions} applied, {skipped_actions} skipped.\n"
            f"Edits: {applied_edits} applied, {skipped_edits} skipped."
        )

    except Exception as e:
        await ctx.error(
            "Critical error during batch processing", extra={"error": str(e)}
        )
        return f"Error processing batch: {str(e)}"


@mcp.tool(
    description="Accepts all tracked changes in the document and removes comments, creating a clean version."
)
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
        await ctx.info(
            "Clean document saved successfully", extra={"output_path": output_path}
        )

        return f"Accepted all changes. Saved to: {output_path}"
    except Exception as e:
        await ctx.error(
            "Failed to accept all changes",
            extra={"error": str(e), "docx_path": docx_path},
        )
        return f"Error accepting changes: {str(e)}"


@mcp.tool(
    description="Reads a DOCX file, extracts its text, applies edits as CriticMarkup, and saves as a Markdown file."
)
async def apply_edits_as_markdown(
    docx_path: Annotated[str, "Absolute path to the DOCX file."],
    edits: Annotated[List[DocumentEdit], "List of edits."],
    ctx: Context,
    output_path: Annotated[
        Optional[str], "Optional path for the output .md file."
    ] = None,
    include_index: Annotated[
        bool, "If True, appends the edit's 0-based index."
    ] = False,
    highlight_only: Annotated[bool, "If True, only highlights target_text."] = False,
    clean_view: Annotated[
        bool, "If True (default), extracts the 'Accepted' state."
    ] = True,
) -> str:
    await ctx.info(
        "Starting CriticMarkup conversion",
        extra={
            "docx_path": docx_path,
            "edits_count": len(edits),
            "highlight_only": highlight_only,
        },
    )

    try:
        await ctx.debug("Reading and extracting text from DOCX")
        stream = _read_file_bytes(docx_path)
        markdown_text = extract_text_from_stream(
            stream,
            filename=Path(docx_path).name,
            clean_view=clean_view,
        )

        await ctx.debug("Applying CriticMarkup edits to extracted Markdown text")
        result = _apply_edits_to_markdown(
            markdown_text=markdown_text,
            edits=edits,
            include_index=include_index,
            highlight_only=highlight_only,
        )

        if not output_path:
            p = Path(docx_path)
            output_path = str(p.parent / f"{p.stem}_markup.md")

        await ctx.debug("Writing Markdown to disk", extra={"output_path": output_path})
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result)

        await ctx.info(
            "CriticMarkup saved successfully", extra={"output_path": output_path}
        )
        return f"Saved CriticMarkup to: {output_path}"

    except FileNotFoundError:
        await ctx.error("Source DOCX file not found", extra={"docx_path": docx_path})
        return f"Error: File not found: {docx_path}"
    except Exception as e:
        await ctx.error("Error applying edits as markdown", extra={"error": str(e)})
        return f"Error applying edits as markdown: {str(e)}"


@mcp.tool(
    description="Logs the user into the Adeu Cloud backend. Securely opens a browser window for authentication."
)
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


@mcp.tool(
    description="Logs out of the Adeu Cloud backend by clearing the local API key from the OS Keychain."
)
async def logout_of_adeu_cloud(ctx: Context) -> str:
    await ctx.info("Initiating cloud session logout")
    try:
        DesktopAuthManager.clear_api_key()
        await ctx.debug("API key cleared from OS Keychain successfully")
        return "Successfully logged out. The local API key has been removed from the Keychain."
    except Exception as e:
        await ctx.error(
            "Failed to clear API key during logout", extra={"error": str(e)}
        )
        raise ToolError(f"Error during logout: {str(e)}") from e


def _encode_multipart_formdata(
    files: List[tuple[str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    buffer = BytesIO()

    for field_name, file_name, file_bytes in files:
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'.encode(
                "utf-8"
            )
        )
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        buffer.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        buffer.write(file_bytes)
        buffer.write(b"\r\n")

    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"


@mcp.tool(
    description=(
        "Analyzes legal documents to find inconsistencies, contradictions, and risk assessments. "
        "Always present the complete report to the user, including every verbatim evidence quote "
        "exactly as returned, without summarizing or omitting any findings."
        "Run this on validation request for files or directories."
    )
)
async def validate_legal_documents(
    file_paths: Annotated[
        List[str], "List of absolute paths to documents (DOCX, PDF) OR directories."
    ],
    ctx: Context,
    api_key: str = Depends(get_cloud_auth_token),
) -> str:
    await ctx.info(
        "Starting legal document validation", extra={"provided_paths": file_paths}
    )

    if not file_paths:
        await ctx.warning("No file paths provided by client")
        raise ToolError(
            "You must provide at least 1 file path or directory to perform document validation."
        )

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
                await ctx.warning(
                    "Unsupported file type skipped", extra={"file": path_str}
                )
                raise ToolError(
                    f"Unsupported file type for {path_str}. Only .docx and .pdf are supported."
                )
            resolved_files.append(p)

    resolved_files = list(set(resolved_files))

    if not resolved_files:
        await ctx.error("No valid documents found in provided paths")
        raise ToolError(
            "No supported documents (.docx or .pdf) were found in the provided paths."
        )

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
            await ctx.error(
                "Failed to read file", extra={"filename": p.name, "error": str(e)}
            )
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
                "# Comprehensive Legal Validation Report\n",
                "## 1. Legal Consistency Check",
                f"**Summary**: {consistency.get('summary', 'No summary provided.')}\n",
            ]

            issues = consistency.get("issues", [])
            if not issues:
                output.append(
                    "No inconsistencies found! The document(s) appear to be structurally aligned.\n"
                )
            else:
                for i, issue in enumerate(issues, 1):
                    output.append(
                        f"### {i}. [{issue.get('severity')}] {issue.get('title')}"
                    )
                    output.append(f"**Description**: {issue.get('description')}")
                    output.append(format_evidence(issue.get("evidence", [])))

            output.append("## 2. Buyer vs. Seller Risk Assessment")
            output.append(
                f"**Summary**: {risk.get('summary', 'No summary provided.')}\n"
            )

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

            return "\n".join(output)

    except urllib.error.HTTPError as e:
        if e.code == 401:
            await ctx.warning("Cloud authentication expired during validation")
            DesktopAuthManager.clear_api_key()
            raise ToolError(
                "Your authentication expired. Please call `login_to_adeu_cloud` to re-authenticate."
            ) from e
        elif e.code == 403:
            await ctx.warning("Authorization Error: User lacks access to this tool")
            raise ToolError(
                "Authorization Error: You do not have access to use this tool."
            ) from e

        error_body = e.read().decode("utf-8")
        await ctx.error(
            "Cloud validation API failure",
            extra={"status_code": e.code, "body": error_body},
        )
        raise ToolError(f"Cloud analysis failed (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        await ctx.error(
            "Unexpected error communicating with Adeu Cloud", extra={"error": str(e)}
        )
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}") from e


def _encode_multipart_formdata(
    files: List[tuple[str, str, bytes]],
) -> tuple[bytes, str]:
    """Encodes files into a multipart/form-data payload for urllib."""
    boundary = uuid.uuid4().hex
    buffer = BytesIO()

    for field_name, file_name, file_bytes in files:
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'.encode(
                "utf-8"
            )
        )
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        buffer.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        buffer.write(file_bytes)
        buffer.write(b"\r\n")

    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"


@mcp.tool(
    description="""
Analyzes a package of multiple legal documents (e.g., MSA + SOW + DPA) to find 
inconsistencies, contradictions, defined term leakage, and structural misalignments.
Use this tool when you need to verify that multiple related files agree with each other.
Returns a structured Markdown report.
"""
)
def check_legal_consistency(
    file_paths: Annotated[
        List[str],
        "List of absolute paths to the documents (DOCX, PDF, etc.) to compare. Must contain at least 2 files.",
    ],
    api_key: str = Depends(get_cloud_auth_token),
) -> str:
    if not file_paths or len(file_paths) < 2:
        raise ToolError(
            "You must provide at least 2 file paths to perform a consistency check."
        )

    files_data = []
    for path_str in file_paths:
        p = Path(path_str)
        if not p.exists():
            raise ToolError(f"File not found on local disk: {path_str}")

        with open(p, "rb") as f:
            # We use 'files' as the field name to match the FastAPI `files: List[UploadFile]` parameter
            files_data.append(("files", p.name, f.read()))

    body, content_type = _encode_multipart_formdata(files_data)
    url = f"{BACKEND_URL}/api/v1/documents/consistency"

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
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))

            # Format the JSON response into a readable Markdown report for the LLM
            output = [
                f"# Legal Consistency Report",
                f"\n**Summary**: {data.get('summary', '')}\n",
                "## Identified Issues\n",
            ]

            issues = data.get("issues", [])
            if not issues:
                output.append(
                    "No inconsistencies found! The documents appear to be structurally aligned."
                )

            for i, issue in enumerate(issues, 1):
                output.append(
                    f"### {i}. [{issue.get('severity')}] {issue.get('issue_title')}"
                )
                output.append(
                    f"**Affected Documents**: {', '.join(issue.get('affected_documents', []))}"
                )
                output.append(f"**Description**: {issue.get('description')}")
                output.append(f"**Recommendation**: {issue.get('recommendation')}\n")

            return "\n".join(output)

    except urllib.error.HTTPError as e:
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            raise ToolError(
                "Your authentication expired. Please call `login_to_adeu_cloud` to re-authenticate."
            )

        # Try to read backend validation errors (e.g. 400 Bad Request)
        error_body = e.read().decode("utf-8")
        raise ToolError(f"Cloud analysis failed (HTTP {e.code}): {error_body}")
    except Exception as e:
        raise ToolError(f"Failed to communicate with Adeu Cloud: {str(e)}")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
