# Adeu: AI Redlining Engine

**Adeu allows AI Agents and LLMs to "Track Changes" in Microsoft Word documents.**

Most LLMs output raw text or Markdown. Professionals need `w:ins` (insertions) and `w:del` (deletions) to review changes inside Word. `adeu` lib shows a Word document in an LLM and human understandable textual format and reflects changes made to it to the actual word document.

It creates a "Virtual DOM" of your document, letting AI apply surgical edits without breaking your formatting, numbering, or headers.

---

## 📦 Installation

Adeu is available on PyPI.

```bash
pip install adeu
```

---

## 🚀 Ways to Use Adeu

### 1. As MCP Server (No Code Required)
If you use an agentic system such as Claude Desktop, you can connect Adeu directly. This lets you handle contracts in Claude and say: *"Change the Governing Law to Delaware and generate me the redline."*

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
*(Requires [uv](https://docs.astral.sh/uv/) installed on your machine)*

### 2. For "Vibe Coding" & Python Scripts
Building your own Agentic AI tool in Cursor, Replit, or Windsurf: Adeu is the engine that handles the document manipulation for you.

```python
from adeu import RedlineEngine, DocumentEdit
from io import BytesIO

# 1. Load your contract
with open("NDA.docx", "rb") as f:
    doc_stream = BytesIO(f.read())

# 2. Define the change (Usually this comes from your LLM response)
edit = DocumentEdit(
    target_text="State of New York",
    new_text="State of Delaware",
    comment="Changed governing law to neutral jurisdiction."
)

# 3. Apply the Redline
engine = RedlineEngine(doc_stream, author="AI Associate")
engine.apply_edits([edit])

# 4. Save
with open("NDA_Redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

### 3. The CLI
Quickly extract text or apply patches from your terminal.

```bash
# Compare two docs and see a summary
adeu diff v1.docx v2.docx

# Apply a JSON list of edits to a doc
adeu apply agreement.docx edits.json
```

---

## ⚖️ Why Adeu?

*   **Native Redlines**: Generates real Microsoft Word Track Changes. You can "Accept" or "Reject" them in Word.
*   **Format Safe**: Adeu preserves your complex numbering, headers, footers, and images. It only touches the text you change.
*   **Native Comments**: Supports adding comments (`Review Pane`) linked to specific text ranges.
*   **Intelligent Mapping**: Handles the messy internal XML of Word documents (e.g., when "Contract" is split into `["Con", "tract"]` by spellcheck).

## 🛡️ License

MIT License. Open source and free to use in commercial legal tech applications.
