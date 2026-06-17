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
        raise FileNotFoundError(
            f"File not found: {path}. Note: If you are running in a sandboxed/containerized environment, "
            "the host application or MCP server may not have access to your local workspace files. "
            "You can resolve this by installing Adeu directly inside your sandboxed environment using "
            "'uv tool install adeu' and executing the commands via the CLI."
        )
    with open(p, "rb") as f:
        return BytesIO(f.read())


def get_build_info() -> tuple[str, str, str]:
    """Retrieves version, git short SHA, and build timestamp dynamically."""
    import subprocess

    # 1. Resolve package version
    version = "unknown"
    # Try importlib.metadata first
    try:
        import importlib.metadata

        version = importlib.metadata.version("adeu")
    except Exception:
        pass

    # If unknown or in local dev, try reading pyproject.toml
    if version == "unknown" or os.environ.get("ADEU_DEV_MODE") == "1":
        try:
            # Look for pyproject.toml up from __file__
            current = Path(__file__).resolve()
            for parent in [current] + list(current.parents):
                pyproject = parent / "pyproject.toml"
                if pyproject.exists():
                    with open(pyproject, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip().startswith("version ="):
                                # Extract version
                                version = line.split("=")[1].strip().strip('"').strip("'")
                                break
                    if version != "unknown":
                        break
        except Exception:
            pass

    # 2. Get git short SHA
    git_sha = os.environ.get("GIT_SHA")
    build_ts = os.environ.get("BUILD_TIMESTAMP")

    # If not in env, check if pre-baked build_info.json exists (created during packaging)
    if not git_sha or not build_ts:
        try:
            import json

            build_info_path = Path(__file__).parent / "build_info.json"
            if build_info_path.exists():
                with open(build_info_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not git_sha:
                        git_sha = data.get("git_sha")
                    if not build_ts:
                        build_ts = data.get("build_timestamp")
        except Exception:
            pass

    if not git_sha:
        try:
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).parent),
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            git_sha = "unknown"

    if not build_ts:
        try:
            # Let's get the timestamp of the HEAD commit or the current time
            build_ts_raw = subprocess.check_output(
                ["git", "log", "-1", "--format=%ct"],
                cwd=str(Path(__file__).parent),
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            import datetime

            build_ts = datetime.datetime.fromtimestamp(int(build_ts_raw), datetime.timezone.utc).strftime(
                "%Y%m%d%H%M%S"
            )
        except Exception:
            build_ts = "unknown"

    return version, git_sha, build_ts


def add_timing_if_debug(start_time: float, result: Any) -> Any:
    """Appends execution time to the tool result if ADEU_ENABLE_TEST_TOOLS is active."""
    if os.getenv("ADEU_ENABLE_TEST_TOOLS") not in ("1", "true", "True", "yes"):
        return result

    elapsed = time.perf_counter() - start_time
    debug_msg = f"\n\n[Debug] Tool execution time: {elapsed:.3f}s"

    if isinstance(result, str):
        return result + debug_msg
    elif hasattr(result, "content") and hasattr(result, "structured_content"):
        # Handle ToolResult via duck typing to avoid circular imports
        if isinstance(result.content, str):
            result.content += debug_msg
        if isinstance(result.structured_content, dict) and "markdown" in result.structured_content:
            result.structured_content["markdown"] += debug_msg
    elif isinstance(result, dict) and "report_text" in result:
        # Handle dicts from tools like sanitize
        result["report_text"] += debug_msg

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
