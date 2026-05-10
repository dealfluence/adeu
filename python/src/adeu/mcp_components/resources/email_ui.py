# FILE: src/adeu/mcp_components/resources/email_ui.py
from pathlib import Path

import jinja2
from fastmcp.resources import resource

from adeu.mcp_components.shared import EMAIL_UI_URI

ADEU_DIR = Path(__file__).resolve().parent.parent.parent

templates_dir = ADEU_DIR / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(templates_dir),
    variable_start_string="[[",
    variable_end_string="]]",
)


def _get_adeu_svg_content() -> str:
    """Reads the adeu.svg file from the assets directory."""
    asset_path = ADEU_DIR / "assets" / "adeu.svg"
    if asset_path.exists():
        with open(asset_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


@resource(
    EMAIL_UI_URI,
    annotations={"readOnlyHint": True},
    mime_type="text/html;profile=mcp-app",
)
def email_ui_app() -> str:
    """Interactive HTML App for rendering Email tool results."""
    adeu_svg_code = _get_adeu_svg_content()
    template = jinja_env.get_template("email_ui.html")

    return template.render(adeu_svg_code=adeu_svg_code)
