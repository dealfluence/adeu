from importlib.metadata import version

from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit
from adeu.redline.engine import RedlineEngine

__version__ = version("adeu")

__all__ = ["RedlineEngine", "DocumentEdit", "extract_text_from_stream", "__version__"]
