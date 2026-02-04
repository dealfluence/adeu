# Adeu: Native Track Changes for AI

**Adeu bridges the gap between LLM text generation and Microsoft Word.**

LLMs speak Markdown; Lawyers speak "Track Changes." Adeu allows AI agents to propose edits to `.docx` files without breaking formatting, numbering, or complex layouts.

It treats the DOCX file as a **Virtual DOM**:
1.  **Ingest:** Extracts a lightweight, token-efficient text representation for the AI.
2.  **Diff:** Calculates changes based on the AI's edits.
3.  **Reconcile:** Surgically injects native XML `w:ins` (insertions) and `w:del` (deletions) back into the original document.

---

## ⚡ Zero-Config Setup

**Prerequisite:** [uv](https://github.com/astral-sh/uv) must be installed.

To instantly add Adeu to **Claude Desktop**:

```bash
uvx adeu init
```

*Restart Claude Desktop to load the new tools.*

---

## Workflows

### 1. For Agents (Claude / MCP)
Adeu runs as a Model Context Protocol (MCP) server. It provides agents with specific tools to read, review, and edit documents safely.

**The "Document Specialist" Prompt:**
Give your agent this context to maximize effectiveness:

> **Role:** Document Specialist
> **Tools:**
> *   `read_docx(clean_view=True)`: Read the final "clean" version of the text to understand context.
> *   `apply_edits_as_markdown`: **Drafting Mode.** Generate a CriticMarkup preview (`{--old--}{++new++}`) to show the user exactly what will change.
> *   `apply_structured_edits`: **Commit Mode.** Apply specific search-and-replace edits to generate native Track Changes in the DOCX.
> *   `manage_review_actions`: **Negotiation.** Reply to comments or Accept/Reject specific changes by ID.

### 2. For Builders (Python SDK)
If you are building a legal-tech application or an automated pipeline, use the `RedlineEngine` directly. It handles the heavy lifting of XML manipulation.

```python
from adeu import RedlineEngine, DocumentEdit
from io import BytesIO

# 1. Load the contract
with open("MSA.docx", "rb") as f:
    stream = BytesIO(f.read())

# 2. Define the edit (e.g., from an LLM response)
# Adeu uses fuzzy matching to locate the target text, even if whitespace varies.
edit = DocumentEdit(
    target_text="State of New York",
    new_text="State of Delaware",
    comment="Standardizing governing law."
)

# 3. Apply changes
engine = RedlineEngine(stream, author="AI Copilot")
engine.apply_edits([edit])

# 4. Save the result
with open("MSA_Redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

### 3. The CLI
Quickly inspect documents or apply batches of edits from your terminal.

```bash
# Extract clean text for RAG or prompting
adeu extract contract.docx -o contract.md

# Generate a visual diff between two versions
adeu diff v1.docx v2.docx

# Preview what an edit list (JSON) would look like
adeu markup contract.docx edits.json --output preview.md

# Apply edits to the DOCX
adeu apply contract.docx edits.json --author "Review Bot"
```

---

## Key Features

### 🛡️ Format Safety
Adeu does not "rewrite" the document. It patches it.
*   **Images & Layouts:** Untouched.
*   **Numbering & Headers:** Preserved.
*   **Complex XML:** It only modifies the text runs targeted by the edit.

### 📝 CriticMarkup Representation
Intermediate representations matter. Adeu uses [CriticMarkup](http://criticmarkup.com/) to visualize changes.

| Markup | Meaning | Example |
| :--- | :--- | :--- |
| `{--text--}` | Deletion | `{--Tenant--}` |
| `{++text++}` | Insertion | `{++Lessee++}` |
| `{>>text<<}` | Comment | `{>>Clarify this term<<}` |

### 🔍 Intelligent Mapping
Word documents are messy. A word like "Contract" might be split into XML runs like `["Con", "tract"]` due to spellcheck or formatting history.
*   **Run Coalescing:** Adeu normalizes these splits so the AI sees "Contract".
*   **Fuzzy Matching:** It handles minor whitespace discrepancies between the LLM's memory and the actual document content.

---

## License

MIT License. Open source and free to use in commercial applications.
