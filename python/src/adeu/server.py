import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog
from fastmcp import FastMCP
from fastmcp.server.providers import FileSystemProvider
from fastmcp.server.transforms import Transform
from fastmcp.tools import Tool
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
        server_icons.append(Icon(src=img.to_data_uri(), mime_type="image/png"))
    except Exception as e:
        logging.warning(f"Failed to load server icon: {e}")

# Set up the filesystem provider to auto-discover tools and resources
mcp_dir = Path(__file__).parent / "mcp_components"
provider = FileSystemProvider(root=mcp_dir)

version, git_sha, _ = get_build_info()


class AdeuBuildTag(Transform):
    """Appends the build stamp to every listed tool description and applies
    the `--scope` tag filter.

    Replaces the FastMCP 3-era monkeypatching of `provider.list_tools` and
    `FastMCP.list_tools`; `Transform` is the supported v4 seam for altering
    how components are presented (spec: Transforms Overview -> Custom
    Transforms). `list_tools` is a pure function in v4, so tools are copied
    rather than mutated in place.

    Only `list_tools` is overridden, deliberately: the 3.x code filtered by
    scope on LISTING only, never on `get_tool`, so a scoped-out tool stayed
    callable by name. Overriding `get_tool` here would silently tighten
    that; scope is a presentation hint, not an access control.
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


def main():
    # --help/--version print and exit inside argparse here — before the stdio
    # transport ever starts (QA 2026-07-19 v8 F-06).
    global requested_scope
    args = _parse_server_args(sys.argv[1:])
    requested_scope = (args.scope or "all").lower()
    mcp.run()


if __name__ == "__main__":
    main()
