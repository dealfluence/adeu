# Feature Specification: Document Finalization 

## 1. The Problem

At the end of a contract negotiation or drafting phase, a document must be prepared for external distribution or signature. This currently requires a disjointed sequence of operations:
1. Accept all track changes.
2. Scrub comments and metadata.
3. Lock the document to prevent unauthorized edits.
4. Export a fixed-layout PDF for e-signature platforms.

Exposing these as 4 separate MCP tools clutters the LLM's context window and risks execution errors (e.g., the agent might export the PDF *before* accepting the changes). 

## 2. The Solution: The `finalize_document` Tool

We will evolve the existing `sanitize_docx` concept into a unified `finalize_document` tool. This provides a single, atomic operation for agents to transition a document from "Draft" to "Outbound."

By overloading this single tool, the agent can configure the exact final state via flags in a single JSON-RPC call.

### 2.1 Proposed Tool Schema

```python
@tool(
    description=(
        "Prepares a document for external distribution or e-signature. "
        "This tool combines metadata sanitization, document locking (protection), "
        "and PDF exportation into a single step."
    ),
    annotations={"destructiveHint": True}
)
async def finalize_document(
    ctx: Context,
    file_path: str,
    output_path: Optional[str] = None,
    
    # 1. Sanitization Sub-config (Inherited from current sanitize_docx)
    sanitize_mode: Literal["full", "keep-markup", "baseline"] = "full",
    accept_all: bool = True,
    
    # 2. Protection Sub-config
    protection_mode: Optional[Literal["read_only", "encrypt"]] = None,
    password: Optional[str] = None,
    
    # 3. Export Sub-config
    export_pdf: bool = False,
    pdf_output_path: Optional[str] = None
) -> dict:
```

## 3. Dual-Path Execution Strategy

Because Adeu supports both **Live Word COM** (Windows) and **Disk XML** (Headless/Mac/Linux), the finalization steps must support both paradigms without losing parity.

### 3.1 Protection & Locking

**Mode: `read_only`** (Anyone can open it, but cannot edit it without a password)
*   **Live Word:** We natively invoke `doc.Protect(Type=3, Password=password)`. (Type 3 = `wdAllowOnlyReading`).
*   **Disk XML:** We inject the native OOXML protection hash into `word/settings.xml`. Word enforces this natively upon opening.
    ```xml
    <w:documentProtection w:edit="readOnly" w:enforcement="1" w:cryptProviderType="rsaFull" w:cryptAlgorithmClass="hash" .../>
    ```

**Mode: `encrypt`** (Requires password just to open the file)
*   **Live Word:** We natively invoke `doc.Password = password`.
*   **Disk XML:** We use the `msoffcrypto-tool` library to wrap the final `.docx` ZIP payload in an AES-encrypted OLE compound document.

### 3.2 PDF Export

If `export_pdf=True`, the tool will generate a PDF *after* all sanitization and formatting acceptance is complete.

*   **Live Word:** We utilize `doc.SaveAs2(pdf_output_path, FileFormat=17)`. This guarantees 100% perfect visual layout parity, capturing the exact state of the canvas.
*   **Disk Fallback:** 
    1. Try `docx2pdf` (Uses Word invisibly in the background on Mac/Win).
    2. Try `soffice --headless --convert-to pdf` (LibreOffice on Linux/CI).

## 4. Report Generation & Audit

The tool will return the same detailed `SanitizeReport` we currently use, but expanded to include the finalization status:

```text
═══════════════════════════════════════════
Finalization Report: MSA_Draft.docx
═══════════════════════════════════════════

TRACKED CHANGES & COMMENTS
  ✓ 7 tracked changes accepted
  ✓ 3 comments stripped

METADATA (scrubbed)
  ✓ Authors, RSIDs, Timestamps removed

PROTECTION
  ✓ Document locked (Read-Only)

EXPORT
  ✓ PDF generated: MSA_Draft_Final.pdf

═══════════════════════════════════════════
Result: SECURE & READY TO SEND
═══════════════════════════════════════════
```

## 5. CLI Parity

The CLI will be updated to reflect this super-command. We will rename `adeu sanitize` to `adeu finalize` (leaving `sanitize` as a hidden alias for backwards compatibility).

```bash
adeu finalize contract.docx \
    --accept-all \
    --read-only "MySecretPassword" \
    --export-pdf
```

## 6. Implementation Steps

1. Add `msoffcrypto-tool` to `pyproject.toml` dependencies.
2. Update `adeu/sanitize/core.py` to become `adeu/finalize/core.py`, adding the XML protection injection and PDF fallback chain.
3. Update `adeu/mcp_components/tools/sanitize.py` to `finalize.py` and implement the expanded schema.
4. Update `adeu/mcp_components/tools/live_word.py` to intercept `finalize_document` and apply `doc.Protect`, `doc.Password`, and `doc.SaveAs2` natively when the file is active on the Windows canvas.
