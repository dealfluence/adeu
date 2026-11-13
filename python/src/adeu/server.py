import argparse
import json
import logging
import queue
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from adeu.mcp_components.shared import get_build_info

_MISSING_MCP_MESSAGE = (
    "Error: Adeu's MCP server dependencies are not installed.\n"
    "Core `adeu` installs only the DOCX engine and CLI; FastMCP lives in the `mcp` extra.\n\n"
    "Install or launch with the `mcp` extra:\n"
    '  * uvx:         uvx --from "adeu[mcp]" adeu-server\n'
    '  * uv tool:     uv tool install "adeu[mcp]"\n'
    '  * pip / uv:    pip install "adeu[mcp]"   (or: uv add "adeu[mcp]")\n'
    "  * Claude App:  uvx adeu init             (updates claude_desktop_config.json)"
)

_MISSING_MCP_AGENT_MESSAGE = (
    _MISSING_MCP_MESSAGE + "\n\nIf you have shell/CLI access, core `adeu` CLI is already available without `[mcp]`:\n"
    "  * Read / inspect:  uvx adeu extract <file.docx> --page all  (or --mode outline / changes / fields)\n"
    "  * Diff two files:  uvx adeu diff <orig.docx> <mod.docx>\n"
    "  * Apply edits:     uvx adeu apply <file.docx> <changes.json> -o <out.docx>\n"
    "  * Sanitize DOCX:   uvx adeu sanitize <file.docx> -o <out.docx>"
)

_DEGRADED_TOOL_NAMES: tuple[str, ...] = (
    "accept_all_changes",
    "apply_text_revision",
    "diff_docx_files",
    "process_document_batch",
    "read_docx",
    "sanitize_docx",
)


def _parse_server_args(argv: "list[str]") -> argparse.Namespace:
    """
    Minimal argument handling BEFORE the stdio server starts: `--help` and
    `--version` must print and exit like every other executable instead of
    silently starting the server (QA 2026-07-19 v8 F-06). Unknown arguments
    are tolerated (host applications append transport flags), so
    parse_known_args — never parse_args — keeps startup permissive. Called
    from main() only: importing this module (e.g. from tests) never parses
    the host process's argv.
    """
    version, git_sha, _ = get_build_info()
    ver_str = f"{version}+{git_sha}" if git_sha and git_sha != "unknown" else version
    parser = argparse.ArgumentParser(
        prog="adeu-server",
        description="Adeu MCP server (stdio transport). Started by MCP hosts such as Claude Desktop.",
        epilog="Configure automatically with `adeu init`. Docs: https://github.com/dealfluence/adeu",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {ver_str}")
    parser.add_argument(
        "--scope",
        choices=["all", "docx"],
        default="all",
        help="Limit exposed tools to local DOCX manipulation ('docx') or everything ('all').",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


# Import-time default; main() re-reads it from the real argv. The legacy scan
# keeps `--scope` working for in-process embedders that import the module with
# a pre-set argv but never call main().
requested_scope = "all"
for i, arg in enumerate(sys.argv):
    if arg == "--scope" and i + 1 < len(sys.argv):
        requested_scope = sys.argv[i + 1].lower()

version, git_sha, _ = get_build_info()
_MCP_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from fastmcp import FastMCP
    from fastmcp.server.providers import FileSystemProvider
    from fastmcp.server.transforms import Transform
    from fastmcp.tools import Tool
    from fastmcp.utilities.types import Image
    from mcp.types import Icon
except ModuleNotFoundError as _exc:
    _MCP_IMPORT_ERROR = _exc
else:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO if requested_scope != "all" else logging.WARNING, force=True
    )

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

    to_client_logger = logging.getLogger("fastmcp.server.context.to_client")
    to_client_logger.setLevel(level=logging.DEBUG)

    server_icons = []
    logo_path = Path(__file__).parent / "assets" / "logo.png"
    if logo_path.exists():
        try:
            img = Image(path=str(logo_path))
            server_icons.append(Icon(src=img.to_data_uri(), mimeType="image/png"))
        except Exception as e:
            logging.warning(f"Failed to load server icon: {e}")

    # Set up the filesystem provider to auto-discover tools and resources
    mcp_dir = Path(__file__).parent / "mcp_components"
    provider = FileSystemProvider(root=mcp_dir)

    class AdeuBuildTag(Transform):
        """Appends the build stamp to every listed tool description and applies
        the `--scope` tag filter.

        `Transform` is FastMCP's supported seam for altering how components are
        presented, and it exists on the 3.4 line as well as on 4.x: server-level
        transforms are applied inside `Provider.list_tools()`, which
        `FastMCP.list_tools()` calls, so both a real client and the internal
        `list_tools()` see the tagged descriptions. It replaces the older
        monkeypatching of `provider.list_tools` / `FastMCP.list_tools`.

        `list_tools` is a pure function here, so tools are copied
        (`model_copy`) rather than mutated in place.

        Only `list_tools` is overridden, deliberately: the pre-transform code
        filtered by scope on LISTING only, never on `get_tool`, so a scoped-out
        tool stayed callable by name. Overriding `get_tool` here would silently
        tighten that; scope is a presentation hint, not an access control.
        """

        async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
            # `requested_scope` is read at call time (module global), because
            # main() rewrites it after this module is imported.
            if requested_scope != "all":
                tools = [t for t in tools if requested_scope in (t.tags or set())]
            build_tag = f" [Adeu v{version}+{git_sha}]"
            out: list[Tool] = []
            for t in tools:
                desc = t.description
                if desc and build_tag not in desc:
                    t = t.model_copy(update={"description": desc.strip() + build_tag})
                out.append(t)
            return out

    # Initialize MCP Server with the provider and transforms
    mcp = FastMCP(
        "Adeu Redlining Service",
        version=version,
        icons=server_icons if server_icons else None,
        providers=[provider],
        transforms=[AdeuBuildTag()],
    )


def __getattr__(name: str) -> Any:
    if _MCP_IMPORT_ERROR is not None and name in {"mcp", "server_icons", "provider", "AdeuBuildTag"}:
        raise ImportError(_MISSING_MCP_MESSAGE) from _MCP_IMPORT_ERROR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _handle_degraded_rpc_message(msg: dict[str, Any], scope: str) -> dict[str, Any] | None:
    """Minimal stdlib JSON-RPC 2.0 MCP responder used ONLY when `adeu[mcp]` is missing.

    Completes the stdio handshake so GUI MCP hosts (Claude Desktop, Cursor, Windsurf)
    keep the server visible instead of silently dropping it, and returns actionable
    `adeu[mcp]` + CLI fallback instructions (`isError: True`) on any `tools/call`.
    """
    if "id" not in msg or msg["id"] is None:
        return None  # JSON-RPC notification (e.g. notifications/initialized)

    msg_id = msg["id"]
    method = msg.get("method")
    params_raw = msg.get("params")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}

    if method == "server/discover":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-06-18", "2024-11-05"],
                "protocolVersions": ["2026-07-28", "2025-06-18", "2024-11-05"],
                "serverInfo": {"name": "adeu-redlining-service", "version": version},
                "capabilities": {"tools": {}},
            },
        }

    if method == "initialize":
        meta_raw = params.get("_meta")
        meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
        proto = params.get("protocolVersion") or meta.get("io.modelcontextprotocol/protocolVersion") or "2024-11-05"
        if not isinstance(proto, str) or not proto.strip():
            proto = "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "adeu-redlining-service", "version": version},
                "instructions": _MISSING_MCP_AGENT_MESSAGE,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        tool_names = list(_DEGRADED_TOOL_NAMES)
        if sys.platform == "win32" and scope == "all":
            tool_names.extend(["open_word_document", "save_active_word_document"])
            tool_names.sort()
        # Omit `$schema` and `additionalProperties: false` so Claude Desktop's
        # 2020-12 Ajv accepts the tool definitions and forwards real arguments
        # to `tools/call`, where the actionable setup message is returned.
        tools = [
            {
                "name": name,
                "description": (
                    "[SETUP REQUIRED: `adeu[mcp]` extra not installed] "
                    'Update MCP config to `--from "adeu[mcp]"` (or run `uvx adeu init`), '
                    "or use `uvx adeu` CLI directly."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to DOCX file"},
                    },
                },
            }
            for name in tool_names
        ]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}

    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": _MISSING_MCP_AGENT_MESSAGE}],
                "isError": True,
            },
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _run_missing_mcp_fallback(scope: str) -> "None":
    """Exit 1 with stderr instructions for interactive/CLI callers, or serve a
    stdlib degraded-mode MCP loop if an MCP host sent a JSON-RPC handshake on stdin.
    """
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        is_tty = True

    if is_tty:
        print(_MISSING_MCP_MESSAGE, file=sys.stderr)
        sys.exit(1)

    line_q: queue.Queue[str] = queue.Queue(maxsize=1)

    def _read_first_line() -> None:
        try:
            line_q.put(sys.stdin.readline())
        except Exception:
            line_q.put("")

    threading.Thread(target=_read_first_line, daemon=True).start()
    try:
        first_line = line_q.get(timeout=0.5)
    except queue.Empty:
        print(_MISSING_MCP_MESSAGE, file=sys.stderr)
        sys.exit(1)

    first_msg: dict[str, Any] | None = None
    if first_line and first_line.lstrip().startswith("{"):
        try:
            parsed = json.loads(first_line)
            if isinstance(parsed, dict) and parsed.get("method") in {
                "initialize",
                "server/discover",
                "ping",
            }:
                first_msg = parsed
        except Exception:
            first_msg = None

    print(_MISSING_MCP_MESSAGE, file=sys.stderr, flush=True)
    if first_msg is None:
        sys.exit(1)

    def _write_rpc(resp: dict[str, Any] | None) -> None:
        if resp is None:
            return
        sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    _write_rpc(_handle_degraded_rpc_message(first_msg, scope))
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        stripped = raw.strip()
        if not stripped.startswith("{"):
            continue
        try:
            msg = json.loads(stripped)
        except Exception:
            continue
        if isinstance(msg, dict):
            _write_rpc(_handle_degraded_rpc_message(msg, scope))
    sys.exit(0)


def main():
    # --help/--version print and exit inside argparse here — before the stdio
    # transport ever starts (QA 2026-07-19 v8 F-06), even without fastmcp.
    global requested_scope
    args = _parse_server_args(sys.argv[1:])
    requested_scope = (args.scope or "all").lower()
    if _MCP_IMPORT_ERROR is not None:
        _run_missing_mcp_fallback(requested_scope)
    mcp.run()


if __name__ == "__main__":
    main()
