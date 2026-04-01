# FILE: src/adeu/mcp_components/tools/validation.py
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, List

from adeu.mcp_components.desktop_auth import DesktopAuthManager, get_cloud_auth_token
from adeu.mcp_components.shared import (
    BACKEND_URL,
    MARKDOWN_UI_URI,
    _encode_multipart_formdata,
)
from fastmcp import Context
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult


@tool(
    description=(
        "Analyzes documents to find inconsistencies, contradictions, and risk assessments. "
        "Always present the complete report to the user, including every verbatim evidence quote "
        "exactly as returned, without summarizing or omitting any findings."
        "Run this on validation request for files or directories."
    ),
    timeout=300.0,
    annotations={"openWorldHint": True},
    meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
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
            report_title = (
                resolved_files[0].name
                if len(resolved_files) == 1
                else f"Validation Report ({len(resolved_files)} files)"
            )
            return ToolResult(
                content=markdown_report,
                structured_content={"markdown": markdown_report, "title": report_title},
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
