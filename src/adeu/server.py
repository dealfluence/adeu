import sys
import logging
import structlog
from pathlib import Path
from io import BytesIO
from typing import List

from mcp.server.fastmcp import FastMCP

# --- LOGGING CONFIGURATION ---
# CRITICAL: Redirect all logs to stderr. 
# Any output to stdout will break the MCP JSON-RPC protocol.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
# -----------------------------

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine
from adeu.models import DocumentEdit

# Initialize the MCP Server
mcp = FastMCP("Adeu Redlining Service")

def _read_file_bytes(path: str) -> BytesIO:
    """Helper to read file into BytesIO."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(p, "rb") as f:
        return BytesIO(f.read())

def _save_stream(stream: BytesIO, path: str):
    """Helper to save BytesIO to file."""
    with open(path, "wb") as f:
        f.write(stream.getvalue())

@mcp.tool()
def read_docx(file_path: str) -> str:
    """
    Reads a local DOCX file and returns its final text content as Markdown (with all tracked changes accepted).
    
    Use this to understand the document content before proposing edits.
    Example: Reading a contract to find the "Governing Law" section before changing it.
    """
    try:
        stream = _read_file_bytes(file_path)
        # Use filename for better logging/context if needed inside ingest
        return extract_text_from_stream(stream, filename=Path(file_path).name)
    except Exception as e:
        return f"Error reading file: {str(e)}"

@mcp.tool()
def apply_structured_edits(
    original_docx_path: str,
    edits: List[DocumentEdit],
    output_path: str,
    author_name: str = "Adeu AI"
) -> str:
    """
    Applies a specific list of structured edits (INSERTION, DELETION, MODIFICATION) to the DOCX file 
    and saves to a NEW output file, leaving the original unchanged.
    
    Returns the count of applied vs. skipped edits.
    Example: Replacing "Seller" with "Vendor" or deleting an obsolete clause.
    Note: output_path must be writable. If it exists, it will be overwritten.
    """
    try:
        # 1. Read Original
        stream = _read_file_bytes(original_docx_path)
        
        # 2. Apply
        engine = RedlineEngine(stream, author=author_name)
        applied, skipped = engine.apply_edits(edits)
        
        # 3. Save
        result_stream = engine.save_to_stream()
        _save_stream(result_stream, output_path)
        
        return f"Applied {applied} edits. Skipped {skipped} edits (targets not found). Saved to: {output_path}"
        
    except Exception as e:
        return f"Error applying edits: {str(e)}"

if __name__ == "__main__":
    # Runs the server over stdio
    mcp.run()