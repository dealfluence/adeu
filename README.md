# Adeu OSS: DOCX Redlining Engine

**Adeu allows AI Agents and LLMs to safely "Track Changes" in Microsoft Word documents.**

Most LLMs output raw text or Markdown. Legal and compliance professionals need `w:ins` (insertions) and `w:del` (deletions) to review changes natively in Word. 

Adeu solves this by treating DOCX as a "Virtual DOM". It presents a clean, readable text representation to the AI, and then **reconciles** the AI's edits back into the original XML structure without breaking formatting, numbering, or images.

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
| `read_docx` | Reads a DOCX file. supports `clean_view=True` to simulate "Accept All Changes" before reading. |
| `diff_docx_files` | Compares two DOCX files and returns a text-based Unified Diff, ignoring formatting noise. |
| `apply_structured_edits` | **The Core Engine.** Applies a list of "Search & Replace" edits, generating native Track Changes (`w:ins`/`w:del`). |
| `manage_review_actions` | Review workflow. Allows the Agent to `ACCEPT`, `REJECT`, or `REPLY` to specific changes or comments by ID. |
| `accept_all_changes` | Creates a clean version of the document by accepting all revisions and removing comments. |

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

### 3. The CLI
Quickly extract text or apply patches from your terminal.

```bash
# Compare two docs and get a summary
adeu diff v1.docx v2.docx

# Apply a structured edit list (JSON) to a doc
adeu apply agreement.docx edits.json --author "Reviewer Bot"
```

---

## Why Adeu?

*   **Native Redlines**: Generates real Microsoft Word Track Changes. You can "Accept" or "Reject" them in Word.
*   **Format Safe**: Preserves complex numbering, headers, footers, and images. It only touches the text you change.
*   **Token Efficient**: Converts heavy XML into lightweight Markdown for the LLM context window.
*   **Intelligent Mapping**: Handles the messy internal XML of Word documents (e.g., when "Contract" is split into `["Con", "tract"]` by spellcheck).

## License

MIT License. Open source and free to use in commercial legal tech applications.
