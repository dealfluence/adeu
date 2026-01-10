# Adeu: Automated DOCX Redlining Engine

Adeu is a Python engine for automated document redlining. It solves the "Round-Trip Problem" in Legal Tech: allowing Large Language Models (LLMs) to edit documents while preserving 100% of the original DOCX formatting, styles, and headers.

## 🚀 Key Features

*   **High-Fidelity Retention**: Does not "convert" the DOCX to Markdown and back. Instead, it extracts text for the LLM and "injects" changes back into the original binary file.
*   **Native Redlines**: Generates real Microsoft Word Track Changes (`w:ins`, `w:del`).
*   **Split-Run Handling**: Intelligently handles Word's complex XML structure where a single word like "Contract" might be split into `["Con", "tract"]`.
*   **Robust Diffing**: Calculates semantic diffs between the original text and LLM output to generate precise edits.
*   **Native Comments**: Injects real Microsoft Word comments (Review pane) linked to specific text ranges, avoiding document clutter.

## 🛠️ Installation

Adeu uses `poetry` for dependency management.

```bash
git clone https://github.com/your-org/adeu.git
cd adeu
poetry install
```

## 🖥️ CLI Usage

The built-in CLI handles the full workflow:

### 1. Extract Text
Convert a DOCX to Markdown for manual or LLM editing.

```bash
poetry run python cli.py contracts/my_contract.docx
# Output: contracts/my_contract.md
```

### 2. Apply Redlines
Compare the modified Markdown against the original DOCX and generate a redlined copy.

```bash
poetry run python cli.py contracts/my_contract.docx contracts/my_contract.md
# Output: contracts/my_contract_redlined.docx
```

## 🤖 MCP Server

Adeu includes a Model Context Protocol (MCP) server. This allows AI agents (like Claude Desktop) to directly read, analyze, and redline DOCX iles on your local machine.

### Tools Exposed
* read_docx(path): Reads a document and returns its text content (for the AI to analyze).
* apply_structured_edits(original_path, edits, output_path): The AI generates structured JSON edits, and the server injects them as Track Changes.

## 📦 Library Usage

```python
from pathlib import Path
from io import BytesIO
from adeu.diff import generate_edits_from_text
from adeu.redline.engine import RedlineEngine
from adeu.ingest import extract_text_from_stream

# 1. Load Data
with open("contract.docx", "rb") as f:
    original_bytes = f.read()
    
# 2. Extract Text (for LLM)
text = extract_text_from_stream(BytesIO(original_bytes))

# 3. ... LLM modifies 'text' to 'new_text' ...

# 4. Generate Diff
edits = generate_edits_from_text(text, new_text)

# 5. Apply Edits
engine = RedlineEngine(BytesIO(original_bytes))
engine.apply_edits(edits)

# 6. Save
with open("contract_redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

## 🏗️ Architecture

*   `src/adeu/ingest.py`: Extracts text using `python-docx` raw run concatenation to ensure 1:1 mapping.
*   `src/adeu/redline/mapper.py`: Indexer that maps text offsets back to specific XML `Run` elements.
*   `src/adeu/redline/engine.py`: The core injector that modifies the XML DOM.
*   `src/adeu/redline/comments.py`: Manages the OXML `comments.xml` part and relationships.
*   `src/adeu/diff.py`: Wraps `diff-match-patch` to convert string diffs into `ComplianceEdit` objects.
