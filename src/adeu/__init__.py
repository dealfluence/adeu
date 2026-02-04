from importlib.metadata import PackageNotFoundError, version

from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine

try:
    __version__ = version("adeu")
except PackageNotFoundError:
    # Package is not installed (e.g., running from source/dev)
    __version__ = "0.0.0-dev"

__all__ = [
    "RedlineEngine",
    "DocumentEdit",
    "extract_text_from_stream",
    "apply_edits_to_markdown",
    "__version__",
]
