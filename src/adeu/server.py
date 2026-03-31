# FILE: src/adeu/server.py
import logging
import sys
from pathlib import Path

import structlog
from fastmcp import FastMCP
from fastmcp.server.providers import FileSystemProvider
from fastmcp.utilities.types import Image
from mcp.types import Icon

logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

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

# Initialize MCP Server with the provider
mcp = FastMCP(
    "Adeu Redlining Service",
    icons=server_icons if server_icons else None,
    providers=[provider],
)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
