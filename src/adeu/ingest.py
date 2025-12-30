import io
import structlog
from docx import Document

logger = structlog.get_logger(__name__)

def extract_text_from_stream(file_stream: io.BytesIO, filename: str = "document.docx") -> str:
    """
    Extracts text from a file stream using raw run concatenation.
    
    CRITICAL: This must match DocumentMapper._build_map logic exactly.
    We iterate runs and join them. We do not use para.text.
    """
    try:
        # Ensure stream is at start
        file_stream.seek(0)
        
        doc = Document(file_stream)
        full_text = []
        
        # 1. Body Paragraphs
        for para in doc.paragraphs:
            # Replicate Mapper logic: join all runs
            # Note: Mapper skips empty runs, so we should too for consistency,
            # or rely on the fact that joining empty strings is fine.
            # Mapper: if text_len == 0: continue.
            p_text = "".join([r.text for r in para.runs])
            full_text.append(p_text)
                
        # 2. Tables
        for table in doc.tables:
            for row in table.rows:
                row_parts = []
                for cell in row.cells:
                    # Cell paragraphs
                    cell_text = "\n".join(["".join([r.text for r in p.runs]) for p in cell.paragraphs])
                    if cell_text:
                        row_parts.append(cell_text)
                
                if row_parts:
                    full_text.append(" | ".join(row_parts))

        return "\n\n".join(full_text)

    except Exception as e:
        logger.error(f"Text extraction failed: {e}", exc_info=True)
        raise ValueError(f"Could not extract text: {str(e)}") from e