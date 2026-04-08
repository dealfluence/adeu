# FILE: src/adeu/mcp_components/tools/validation.py
import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Optional

from fastmcp import Context
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult

from adeu.mcp_components.desktop_auth import DesktopAuthManager, get_cloud_auth_token
from adeu.mcp_components.shared import (
    BACKEND_URL,
    MARKDOWN_UI_URI,
    _encode_multipart_formdata,
)


@tool(
    description=(
        "Validates documents for inconsistencies, contradictions, and risk assessments. "
        "To START a new validation, provide 'file_paths' as a JSON-encoded string representing a list of file paths. "
        "This will immediately return a task_id. "
        "To CHECK the status of a validation, call this tool AGAIN and provide ONLY the 'task_id'. "
        "The checking process will poll for up to 50 seconds. If it times out, continue checking."
    ),
    timeout=300.0,
    annotations={"openWorldHint": True},
    meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
)
async def validate_documents(
    ctx: Context,
    file_paths: Annotated[
        Optional[str],
        (
            "A JSON-encoded string of a list of absolute paths to documents (DOCX, PDF) "
            "OR directories to start a new job. "
            'Example: \'["/path/to/doc1.pdf", "/path/to/doc2.docx"]\''
        ),
    ] = None,
    task_id: Annotated[Optional[int], "If resuming a pending check, provide the task ID here."] = None,
    api_key: str = Depends(get_cloud_auth_token),
) -> ToolResult:
    if not file_paths and not task_id:
        raise ToolError(
            "You must provide either 'file_paths' to start a new validation, or 'task_id' to check an existing one."
        )

    # ==========================================
    # PHASE 1: INIT (Upload and get task_id)
    # ==========================================
    if file_paths:
        # Parse the JSON string into a Python list
        try:
            parsed_paths = json.loads(file_paths)
        except json.JSONDecodeError:
            # Fallback: LLMs often fail to double-escape Windows paths in nested JSON.
            # This replaces single backslashes with double backslashes and tries again.
            try:
                sanitized_paths = file_paths.replace("\\", "\\\\")
                parsed_paths = json.loads(sanitized_paths)
            except json.JSONDecodeError as e:
                raise ToolError(
                    f"Failed to parse 'file_paths'. Ensure it is a valid JSON array of strings. Error: {e}"
                ) from e

        if not isinstance(parsed_paths, list):
            raise ToolError("The 'file_paths' argument must resolve to a list of strings.")

        await ctx.info(
            "Starting new document validation task",
            extra={"provided_paths": parsed_paths},
        )
        resolved_files: list[Path] = []
        valid_extensions = {".docx", ".pdf"}

        for path_str in parsed_paths:
            p = Path(path_str)
            if not p.exists():
                raise ToolError(f"Path not found on local disk: {path_str}")

            if p.is_dir():
                for child in p.iterdir():
                    if child.is_file() and child.suffix.lower() in valid_extensions:
                        resolved_files.append(child)
            elif p.is_file():
                if p.suffix.lower() not in valid_extensions:
                    raise ToolError(f"Unsupported file type for {path_str}. Only .docx and .pdf are supported.")
                resolved_files.append(p)

        resolved_files = list(set(resolved_files))
        if not resolved_files:
            raise ToolError("No supported documents (.docx or .pdf) were found in the provided paths.")

        files_data = []
        for p in resolved_files:
            with open(p, "rb") as f:
                files_data.append(("files", p.name, f.read()))

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
            response = await asyncio.to_thread(urllib.request.urlopen, req)
            data = json.loads(response.read().decode("utf-8"))
            new_task_id = data.get("task_id")

            msg = (
                f"Validation task started successfully. Task ID: {new_task_id}. "
                f"Please call `validate_documents` again immediately with "
                f"task_id={new_task_id} to monitor the progress."
            )
            await ctx.info(f"Task started: {new_task_id}")
            return ToolResult(content=msg, structured_content={"status": "pending", "message": msg})

        except urllib.error.HTTPError as e:
            if e.code == 401:
                DesktopAuthManager.clear_api_key()
                raise ToolError(
                    "Your authentication expired. Please call `login_to_adeu_cloud` to re-authenticate."
                ) from e
            error_body = e.read().decode("utf-8")
            raise ToolError(f"Cloud analysis failed (HTTP {e.code}): {error_body}") from e
        except Exception as e:
            raise ToolError(f"Unexpected error: {str(e)}") from e

    # ==========================================
    # PHASE 2: POLL (Wait for completion)
    # ==========================================
    poll_url = f"{BACKEND_URL}/api/v1/documents/validate/{task_id}"

    # Poll up to 10 times (5 seconds each) = 50 seconds total
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
            data = json.loads(response.read().decode("utf-8"))
            status = data.get("status")

            if status == "COMPLETED":
                markdown_report = data.get("report_markdown", "No report generated.")
                return ToolResult(
                    content=markdown_report,
                    structured_content={
                        "markdown": markdown_report,
                        "title": f"Validation Report #{task_id}",
                        "status": "completed",
                    },
                )

            if status == "FAILED":
                error_msg = data.get("error", "Unknown internal error")
                raise ToolError(f"Validation task failed on the server: {error_msg}")

            await ctx.debug(f"Task {task_id} status is {status}. Attempt {attempt + 1}/10. Sleeping 5s.")

        except urllib.error.HTTPError as e:
            if e.code == 401:
                DesktopAuthManager.clear_api_key()
                raise ToolError("Your authentication expired. Please re-authenticate.") from e
            error_body = e.read().decode("utf-8")
            raise ToolError(f"Failed to check task status (HTTP {e.code}): {error_body}") from e
        except Exception as e:
            raise ToolError(f"Unexpected error checking task status: {str(e)}") from e

        # Sleep 5 seconds before the next poll
        await asyncio.sleep(5)

    # If we reach here, the 50s timeout has been reached but it's still pending
    msg = f"Task {task_id} is still processing. Please call `validate_documents` again with task_id={task_id}."
    return ToolResult(content=msg, structured_content={"status": "pending", "message": msg})
