# Adeu OSS: DOCX Redlining Engine

**Adeu allows AI Agents and LLMs to safely "Track Changes" in Microsoft Word documents.**

Most LLMs output raw text or Markdown. Legal and compliance professionals need `w:ins` (insertions) and `w:del` (deletions) to review changes natively in Word. 

Adeu solves this by treating DOCX as a "Virtual DOM". It presents a clean, readable text representation to the AI, and then **reconciles** the AI's edits back into the original XML structure without breaking formatting, numbering, or images.

## 🚀 New in v0.6.0
*   **CriticMarkup Preview**: New `apply_edits_as_markdown` tool and `markup` CLI command to preview changes as CriticMarkup in Markdown files before applying to DOCX.
*   **Highlight-Only Mode**: Mark target text locations with `{==...==}` without applying changes — perfect for review workflows.
*   **Edit Indexing**: Track edit positions with `[Edit:N]` markers for easy reference back to the original edit list.

## 🚀 New in v0.5.0
*   **Comments & Threads**: Full support for reading and replying to Word comments using **CriticMarkup** syntax (`{==Target==}{>>Comment<<}`).
*   **Negotiation Actions**: Agents can now `ACCEPT`, `REJECT`, or `REPLY` to specific changes and comments.
*   **Safety**: Enhanced protection against corrupting nested revisions or structural boilerplate.

---

## Installation
```bash
pip install adeu
```

---

## Ways to Use Adeu

### 1. As an MCP Server
Connect Adeu directly to your agentic workspace. This allows AI Agent to read contracts, propose redlines, and answer comments natively.

Add this to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "adeu": {
      "command": "uvx",
      "args": ["adeu", "adeu-server"]
    }
  }
}
```

**What the Agent sees:**
The agent receives a text view of the document where comments and changes are clearly marked:
```text
The Vendor shall be liable for {==indirect damages==}{>>[Counsel] We request this be removed.<<}...
```

#### Available MCP Tools

| Tool | Description |
| :--- | :--- |
| `read_docx` | Reads a DOCX file. Supports `clean_view=True` to simulate "Accept All Changes" before reading. |
| `diff_docx_files` | Compares two DOCX files and returns a text-based Unified Diff, ignoring formatting noise. |
| `apply_structured_edits` | **The Core Engine.** Applies a list of "Search & Replace" edits, generating native Track Changes (`w:ins`/`w:del`). |
| `manage_review_actions` | Review workflow. Allows the Agent to `ACCEPT`, `REJECT`, or `REPLY` to specific changes or comments by ID. |
| `accept_all_changes` | Creates a clean version of the document by accepting all revisions and removing comments. |
| `apply_edits_as_markdown` | **New.** Extracts text from a DOCX, applies edits as CriticMarkup, and saves as a `.md` file for preview. |

#### CriticMarkup Preview Example

The `apply_edits_as_markdown` tool lets agents preview changes before applying them to the actual DOCX:
```python
# Input: contract.docx containing "The Tenant shall pay rent monthly."
# Edit: target_text="Tenant", new_text="Lessee", comment="Standardizing terminology"

# Output saved to contract_markup.md:
The {--Tenant--}{++Lessee++}{>>Standardizing terminology<<} shall pay rent monthly.
```

Options:
- `highlight_only=True`: Only mark targets with `{==...==}` without showing changes
- `include_index=True`: Add `[Edit:N]` markers for tracking

### 2. For Python Developers ("Vibe Coding")
Adeu handles the heavy lifting of XML manipulation so you can focus on the logic.
```python
from adeu import RedlineEngine, DocumentEdit
from io import BytesIO

# 1. Load your contract
with open("NDA.docx", "rb") as f:
    stream = BytesIO(f.read())

# 2. Define the change (e.g., from an LLM response)
# Adeu uses "Search & Replace" logic with fuzzy matching
edit = DocumentEdit(
    target_text="State of New York",
    new_text="State of Delaware",
    comment="Standardizing governing law."
)

# 3. Apply the Redline
engine = RedlineEngine(stream, author="AI Associate")
engine.apply_edits([edit])

# 4. Save
with open("NDA_Redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

#### Preview Changes as Markdown

You can also preview changes as CriticMarkup without modifying the DOCX:
```python
from adeu import apply_edits_to_markdown, DocumentEdit

text = "The Tenant shall pay rent monthly."
edits = [
    DocumentEdit(
        target_text="Tenant",
        new_text="Lessee",
        comment="Standardizing terminology"
    )
]

# Full preview with changes
result = apply_edits_to_markdown(text, edits, include_index=True)
# Output: "The {--Tenant--}{++Lessee++}{>>Standardizing terminology [Edit:0]<<} shall pay rent monthly."

# Highlight-only mode (show what will be changed)
preview = apply_edits_to_markdown(text, edits, highlight_only=True)
# Output: "The {==Tenant==}{>>Standardizing terminology<<} shall pay rent monthly."
```

### 3. The CLI
Quickly extract text, apply patches, or preview changes from your terminal.
```bash
# Extract text from a DOCX
adeu extract contract.docx -o contract.md

# Compare two docs and get a summary
adeu diff v1.docx v2.docx

# Apply a structured edit list (JSON) to a doc
adeu apply agreement.docx edits.json --author "Reviewer Bot"

# Preview changes as CriticMarkup Markdown (NEW)
adeu markup contract.docx edits.json -o preview.md

# Highlight-only mode (show targets without changes)
adeu markup contract.docx edits.json --highlight

# Include edit indices for tracking
adeu markup contract.docx edits.json --index
```

#### CLI Commands Reference

| Command | Description |
| :--- | :--- |
| `extract` | Extract text from a DOCX file to stdout or a file. |
| `diff` | Compare two DOCX files and show changes. |
| `apply` | Apply edits from a JSON file to a DOCX with Track Changes. |
| `markup` | **New.** Apply edits to a document and output as CriticMarkup Markdown. |

#### Edits JSON Format

Both `apply` and `markup` commands accept a JSON file with this structure:
```json
[
  {
    "target_text": "Contract Agreement",
    "new_text": "Service Agreement",
    "comment": "Standardizing terminology"
  },
  {
    "target_text": "30 days",
    "new_text": "60 days",
    "comment": "Extended notice period"
  }
]
```

---

## CriticMarkup Syntax

Adeu uses [CriticMarkup](http://criticmarkup.com/) for text-based change representation:

| Markup | Meaning | Example |
| :--- | :--- | :--- |
| `{--text--}` | Deletion | `{--old text--}` |
| `{++text++}` | Insertion | `{++new text++}` |
| `{==text==}` | Highlight | `{==marked text==}` |
| `{>>text<<}` | Comment | `{>>reviewer note<<}` |

Combined example:
```
The {--Tenant--}{++Lessee++}{>>Standardizing terminology [Edit:0]<<} shall pay rent.
```

---

## Why Adeu?

*   **Native Redlines**: Generates real Microsoft Word Track Changes. You can "Accept" or "Reject" them in Word.
*   **Format Safe**: Preserves complex numbering, headers, footers, and images. It only touches the text you change.
*   **Token Efficient**: Converts heavy XML into lightweight Markdown for the LLM context window.
*   **Intelligent Mapping**: Handles the messy internal XML of Word documents (e.g., when "Contract" is split into `["Con", "tract"]` by spellcheck).
*   **Preview First**: Review changes as CriticMarkup Markdown before committing to the DOCX.

## License

MIT License. Open source and free to use in commercial legal tech applications.