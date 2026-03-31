# FILE: src/adeu/mcp/shared.py
import mimetypes
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import List

from fastmcp.exceptions import ToolError

from adeu.auth import DesktopAuthManager

BACKEND_URL = os.environ.get("ADEU_BACKEND_URL", "http://localhost:8000")
VIEW_URI = "ui://adeu/html-viewer"


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
