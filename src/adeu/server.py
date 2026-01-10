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
from adeu.diff import generate_edits_from_text
from adeu.redline.engine import RedlineEngine
from adeu.models import DocumentEdit, EditOperationType

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
def diff_docx_files(original_path: str, modified_path: str) -> str:
    """
    Compares two DOCX files and returns a Semantic Unified Diff.
    
    Returns a standard-looking Diff format but optimized for prose (word-level granularity).
    Use this to understand specific changes without loading the full document text.
    """
    try:
        # 1. Ingest both files to text
        stream_orig = _read_file_bytes(original_path)
        text_orig = extract_text_from_stream(stream_orig, filename=Path(original_path).name)
        
        stream_mod = _read_file_bytes(modified_path)
        text_mod = extract_text_from_stream(stream_mod, filename=Path(modified_path).name)
        
        # 2. Generate Edits
        edits = generate_edits_from_text(text_orig, text_mod)
        
        if not edits:
            return "No text differences found between the documents."
            
        # 3. Format Output as Unified Diff Hunks
        # We use the computed indices to grab context from the original text.
        output = [f"--- {Path(original_path).name}", f"+++ {Path(modified_path).name}", ""]
        
        CONTEXT_SIZE = 40  # Characters of context
        
        for edit in edits:
            # Calculate context based on the match index found by the diff engine
            start_idx = getattr(edit, "_match_start_index", 0) or 0
            
            # Pre-context
            pre_start = max(0, start_idx - CONTEXT_SIZE)
            pre_context = text_orig[pre_start:start_idx]
            if pre_start > 0:
                pre_context = "..." + pre_context
                
            # Post-context calculation
            target_len = len(edit.target_text) if edit.target_text else 0
            
            # For Insertion, text is added AT the index. For Del/Mod, text is removed FROM the index.
            if edit.operation == EditOperationType.INSERTION:
                post_start = start_idx
            else:
                post_start = start_idx + target_len
                
            post_end = min(len(text_orig), post_start + CONTEXT_SIZE)
            post_context = text_orig[post_start:post_end]
            if post_end < len(text_orig):
                post_context = post_context + "..."
                
            # Sanitize newlines for display
            pre_context = pre_context.replace("\n", " ").replace("\r", "")
            post_context = post_context.replace("\n", " ").replace("\r", "")
            
            output.append("@@ Word Patch @@")
            output.append(f" {pre_context}")
            if edit.operation != EditOperationType.INSERTION:
                output.append(f"- {edit.target_text}")
            if edit.operation != EditOperationType.DELETION:
                output.append(f"+ {edit.new_text}")
            output.append(f" {post_context}")
            output.append("") # Spacer
            
        return "\n".join(output)

    except Exception as e:
        return f"Error computing diff: {str(e)}"

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