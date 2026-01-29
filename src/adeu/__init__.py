# FILE: src/adeu/__init__.py
from importlib.metadata import version

from adeu.ingest import extract_text_from_stream
from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine

__version__ = version("adeu")

__all__ = [
    "RedlineEngine",
    "DocumentEdit",
    "extract_text_from_stream",
    "apply_edits_to_markdown",
    "__version__",
]
