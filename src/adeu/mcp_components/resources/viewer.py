# FILE: src/adeu/mcp/resources/viewer.py
from pathlib import Path

import jinja2
from fastmcp.resources import resource

from adeu.mcp_components.shared import VIEW_URI

# Resolve paths relative to the new file location (up 3 levels to src/adeu)
ADEU_DIR = Path(__file__).resolve().parent.parent.parent

templates_dir = ADEU_DIR / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(templates_dir),
    variable_start_string="[[",
    variable_end_string="]]",
)


def _get_marked_js_content() -> str:
    """Reads the bundled marked.min.js file from the assets directory."""
    asset_path = ADEU_DIR / "assets" / "marked.min.js"
    if asset_path.exists():
        with open(asset_path, "r", encoding="utf-8") as f:
            return f.read()
    return f"window.__MARKED_ERROR = 'File not found at: {asset_path}';"


def _get_adeu_svg_content() -> str:
    """Reads the adeu.svg file from the assets directory."""
    asset_path = ADEU_DIR / "assets" / "adeu.svg"
    if asset_path.exists():
        with open(asset_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


@resource(VIEW_URI, annotations={"readOnlyHint": True}, mime_type="text/html;profile=mcp-app")
def html_viewer() -> str:
    """Interactive HTML Viewer App using standard Markdown."""
    marked_js_code = _get_marked_js_content()
    adeu_svg_code = _get_adeu_svg_content()
    template = jinja_env.get_template("viewer.html")

    # Pass the SVG code into the template context
    return template.render(marked_js_code=marked_js_code, adeu_svg_code=adeu_svg_code)
