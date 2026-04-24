import mimetypes
import os
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

# Centralized MCP Configuration
FRONTEND_URL = os.environ.get("ADEU_FRONTEND_URL", "https://app.adeu.ai")
BACKEND_URL = os.environ.get("ADEU_BACKEND_URL", "https://app.adeu.ai")
MARKDOWN_UI_URI = "ui://adeu/markdown-ui"
EMAIL_UI_URI = "ui://adeu/email-ui"


def read_file_bytes(path: str) -> BytesIO:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(p, "rb") as f:
        return BytesIO(f.read())


def add_timing_if_debug(start_time: float, result: Any) -> Any:
    """Appends execution time to the tool result if ADEU_ENABLE_TEST_TOOLS is active."""
    if os.getenv("ADEU_ENABLE_TEST_TOOLS") not in ("1", "true", "True", "yes"):
        return result

    elapsed = time.perf_counter() - start_time
    timing_msg = f"\n\n[Debug] Tool execution time: {elapsed:.3f}s"

    if isinstance(result, str):
        return result + timing_msg
    elif hasattr(result, "content") and hasattr(result, "structured_content"):
        # Handle ToolResult via duck typing to avoid circular imports
        if isinstance(result.content, str):
            result.content += timing_msg
        if isinstance(result.structured_content, dict) and "markdown" in result.structured_content:
            result.structured_content["markdown"] += timing_msg
    elif isinstance(result, dict) and "report_text" in result:
        # Handle dicts from tools like sanitize
        result["report_text"] += timing_msg

    return result


def save_stream(stream: BytesIO, path: str):
    with open(path, "wb") as f:
        f.write(stream.getvalue())


def encode_multipart_formdata(
    fields: Optional[Dict[str, str]] = None,
    files: Optional[List[tuple[str, str, bytes]]] = None,
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    buffer = BytesIO()

    if fields:
        for key, value in fields.items():
            buffer.write(f"--{boundary}\r\n".encode("utf-8"))
            buffer.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            buffer.write(value.encode("utf-8"))
            buffer.write(b"\r\n")

    if files:
        for field_name, file_name, file_bytes in files:
            buffer.write(f"--{boundary}\r\n".encode("utf-8"))
            buffer.write(
                f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'.encode("utf-8")
            )
            content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            buffer.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            buffer.write(file_bytes)
            buffer.write(b"\r\n")

    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"
