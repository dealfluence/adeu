# FILE: src/adeu/mcp_components/resources/markdown_ui.py
from pathlib import Path

import jinja2
from adeu.mcp_components.shared import MARKDOWN_UI_URI
from fastmcp.resources import resource

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


@resource(
    MARKDOWN_UI_URI,
    annotations={"readOnlyHint": True},
    mime_type="text/html;profile=mcp-app",
)
def markdown_ui_app() -> str:
    """Interactive HTML App for rendering Markdown tool results."""
    marked_js_code = _get_marked_js_content()
    adeu_svg_code = _get_adeu_svg_content()
    template = jinja_env.get_template("markdown_ui.html")

    # Pass the SVG code into the template context
    return template.render(marked_js_code=marked_js_code, adeu_svg_code=adeu_svg_code)
