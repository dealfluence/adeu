import os
import time
from io import BytesIO
from pathlib import Path
from typing import Any

# Centralized MCP Configuration
MARKDOWN_UI_URI = "ui://adeu/markdown-ui"

# MCP callers cannot run the CLI, so id-discovery advice inside engine errors
# must point at the MCP tool instead (QA 2026-07-23 F11). Passed to
# RedlineEngine(id_discovery_hint=...) by every MCP-surface engine construction.
MCP_ID_DISCOVERY_HINT = (
    "Call `read_docx` on the document again to list the current change (Chg:) "
    "and comment (Com:) ids — ids shift between document states."
)


def read_file_bytes(path: str) -> BytesIO:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"File not found: {path}.\n"
            "Provide an absolute path — the MCP server cannot resolve relative "
            "paths against your workspace.\n"
            "If you are running in a sandboxed/containerized environment\n"
            "(such as Claude Desktop or another containerized client),\n"
            "the host application or MCP server may not have direct access to your local workspace files.\n"
            "You can resolve this by installing and running the local 'adeu' CLI tool\n"
            "directly within your environment.\n"
            "Here is how the MCP tools map to their CLI equivalents:\n"
            f"- read_docx               -> adeu extract {path}\n"
            f"- process_document_batch  -> adeu apply {path} <changes.json>\n"
            f"- diff_docx_files         -> adeu diff {path} <modified_path>\n"
            f"- accept_all_changes      -> adeu accept-all {path}\n\n"
            "All of these commands accept a --json flag that emits a machine-readable\n"
            "result on stdout; logs and errors go to stderr.\n"
            "To run the local tool, install it via:\n"
            "  uv tool install adeu\n"
            "and run the mapped CLI command directly in your terminal."
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
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(stream.getvalue())
