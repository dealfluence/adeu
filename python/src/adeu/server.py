import argparse
import logging
import sys
from pathlib import Path

import structlog
from fastmcp import FastMCP
from fastmcp.server.providers import FileSystemProvider
from fastmcp.utilities.types import Image
from mcp.types import Icon

from adeu.mcp_components.shared import get_build_info


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

logging.basicConfig(stream=sys.stderr, level=logging.INFO if requested_scope != "all" else logging.WARNING, force=True)

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

version, git_sha, _ = get_build_info()

# Initialize MCP Server with the provider
mcp = FastMCP(
    "Adeu Redlining Service",
    version=version,
    icons=server_icons if server_icons else None,
    providers=[provider],
)

# Dynamically append the build info to tool descriptions
orig_list_tools = provider.list_tools


async def wrapped_list_tools(*args, **kwargs):
    tools = await orig_list_tools(*args, **kwargs)
    build_tag = f" [Adeu v{version}+{git_sha}]"
    for tool in tools:
        if hasattr(tool, "description") and tool.description:
            if build_tag not in tool.description:
                tool.description = tool.description.strip() + build_tag
    return tools


provider.list_tools = wrapped_list_tools  # type: ignore[method-assign]

orig_mcp_list_tools = mcp.list_tools


async def wrapped_mcp_list_tools(*args, **kwargs):
    tools = await orig_mcp_list_tools(*args, **kwargs)
    build_tag = f" [Adeu v{version}+{git_sha}]"

    if requested_scope != "all":
        filtered = []
        for tool in tools:
            tags = getattr(tool, "tags", []) or []
            if requested_scope in tags:
                filtered.append(tool)
        tools = filtered

    for tool in tools:
        if hasattr(tool, "description") and tool.description:
            if build_tag not in tool.description:
                tool.description = tool.description.strip() + build_tag
    return tools


mcp.list_tools = wrapped_mcp_list_tools  # type: ignore[method-assign]


def main():
    # --help/--version print and exit inside argparse here — before the stdio
    # transport ever starts (QA 2026-07-19 v8 F-06).
    global requested_scope
    args = _parse_server_args(sys.argv[1:])
    requested_scope = (args.scope or "all").lower()
    mcp.run()


if __name__ == "__main__":
    main()
