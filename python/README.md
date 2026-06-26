# Adeu: Python Toolchain & MCP Server

This directory contains the core Python implementation of Adeu. It provides the core Redline Engine, the developer SDK, the command-line interface (CLI), and the FastMCP server backend. 

Adeu acts as a "Virtual DOM" for Microsoft Word. It translates complex DOCX XML into token-efficient CriticMarkup for LLMs, validates structural edits, and patches the XML safely to preserve document formatting, metadata, and styles. On Windows, it also interfaces directly with live Microsoft Word instances via COM.

## Local Development Setup

Adeu is managed using [`uv`](https://docs.astral.sh/uv/) and packaged via `hatchling`. It requires Python 3.12 or higher.

```bash
# Clone and enter the directory
cd python

# Install dependencies and sync the virtual environment
uv sync

# Run the test suite
uv run pytest
```

## The Command Line Interface (CLI)

The `adeu` CLI provides a powerful suite of tools for interacting with documents locally.

### Extraction & Reading
Extract text as CriticMarkup. Use `--clean-view` to simulate "Accept All Changes".
```bash
# Extract full text
uvx adeu extract contract.docx -o output.md

# Extract only the structural heading outline
uvx adeu extract contract.docx --mode outline

# Windows Only: Extract text from the actively open Word document
uvx adeu extract --live
```

### Diffing
Generate a word-level patch diff between two document versions.
```bash
# Compare two DOCX files
uvx adeu diff original.docx modified.docx

# Output raw JSON edits for programmatic use
uvx adeu diff original.docx modified.docx --json
```

### Applying Edits
Apply a JSON array of `DocumentChange` objects (or a modified markdown file) back to the DOCX.
```bash
# Apply a JSON batch of edits to a file
uvx adeu apply original.docx edits.json --author "AI Reviewer" -o redlined.docx

# Simulate the changes without modifying the file to get a preview report
uvx adeu apply original.docx edits.json --dry-run

# Windows Only: Apply edits directly to the live, open Word canvas
uvx adeu apply edits.json --live
```

### Sanitization
Strip sensitive metadata, hidden text, and author names before external distribution.
```bash
# Full scrub (fails if unresolved track changes exist unless --accept-all is passed)
uvx adeu sanitize contract.docx --accept-all -o clean.docx

# Keep your redlines/comments, but anonymize the author and strip metadata
uvx adeu sanitize redline.docx --keep-markup --author "My Firm"
```

## The Python SDK

The SDK allows you to embed Adeu's Redline Engine directly into your own Python applications.

### Applying Tracked Changes
The engine processes a flat list of `DocumentChange` objects (`ModifyText`, `AcceptChange`, `RejectChange`, `ReplyComment`, `InsertTableRow`, `DeleteTableRow`).

```python
from io import BytesIO
from adeu import RedlineEngine, ModifyText, AcceptChange

# 1. Load the document stream
with open("contract.docx", "rb") as f:
    stream = BytesIO(f.read())

# 2. Define your edits
changes = [
    ModifyText(
        target_text="State of New York",
        new_text="State of Delaware",
        comment="Standardized jurisdiction.",
        match_mode="all"
    ),
    AcceptChange(target_id="Chg:12")
]

# 3. Initialize the engine and apply
engine = RedlineEngine(stream, author="AI Copilot")
stats = engine.process_batch(changes)

# 4. Save the result
with open("contract_redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

### Extracting Text
Read a document into CriticMarkup representation.

```python
from io import BytesIO
from adeu import extract_text_from_stream

with open("contract.docx", "rb") as f:
    stream = BytesIO(f.read())

# Extract raw text with {++ ++} and {-- --} tags intact
markdown_text = extract_text_from_stream(stream)

# Extract clean text simulating "Accept All Changes"
clean_text = extract_text_from_stream(stream, clean_view=True)
```

### Sanitizing Documents
Run the metadata scrubber programmatically.

```python
from adeu.sanitize import sanitize_docx

result = sanitize_docx(
    input_path="draft.docx",
    output_path="final.docx",
    keep_markup=True,
    author="Legal Team"
)

print(result.report_text)
```

## The MCP Server

The Python backend exposes Adeu's capabilities to AI agents via the Model Context Protocol (MCP), powered by FastMCP.

### Running the Server
You can boot the server over stdio for agent consumption:
```bash
uvx --from adeu adeu-server
```

### Claude Desktop Integration
Adeu provides an initialization command to automatically inject the MCP server into your local Claude Desktop configuration.

```bash
# Installs to Claude Desktop using the global uvx path
uvx adeu init

# Local Developer Mode: Configures Claude to run the server from your current source tree
uv run adeu init --local
```

### Live Word Interop (Windows COM)
When the MCP server runs on Windows (`sys.platform == 'win32'`), it automatically enables the `live_word.py` tools. These tools utilize `pywin32` to hijack the active Microsoft Word COM object. 

If an agent leaves the `file_path` argument empty when calling `read_docx` or `process_document_batch`, the server will automatically target the document that the user currently has open on their screen.

## Testing & Architectural Constraints

When developing inside the `python/` directory, please note the following invariants:

* **Surgical Mode**: The `RedlineEngine` never performs global document normalization on load or save. This strict behavior prevents the silent destruction of unrelated metadata (like `<w:proofErr>`) and minimizes XML diff noise.
* **COM Teardown**: In `live_word.py` and its associated tests, we intentionally omit `pythoncom.CoUninitialize()` and `app.Quit()` during teardown. FastMCP and `pytest` hold proxies unpredictably: forcing teardown causes fatal RPC Access Violations (`0x800706be`). We let the OS handle the apartment lifecycle.
* **Testing Asserts**: Native `python-docx` `Paragraph.text` properties silently ignore text inside `<w:ins>` tags. When writing tests to verify redlines, strictly use `extract_text_from_stream(clean_view=True)` to accurately evaluate the accepted text state.