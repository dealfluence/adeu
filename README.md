# Adeu: Native Track Changes for AI

[![PyPI version](https://img.shields.io/pypi/v/adeu.svg)](https://pypi.org/project/adeu/)
[![Python versions](https://img.shields.io/pypi/pyversions/adeu.svg)](https://pypi.org/project/adeu/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/dealfluence/adeu/actions/workflows/ci.yml/badge.svg)](https://github.com/dealfluence/adeu/actions/workflows/ci.yml)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

**LLMs speak Markdown; Lawyers speak "Track Changes."** 

Adeu is a Model Context Protocol (MCP) server and Python SDK that acts as a **"Virtual DOM" for Microsoft Word**. It provides a two-way abstraction layer that lets AI agents freely edit document text without destroying the underlying formatting or complex DOCX XML.

While standard libraries like `python-docx` excel at generating documents from scratch, they fail at non-destructive redlining. Adeu solves this by translating `.docx` files into a token-efficient Markdown representation. This frees AI agents to focus entirely on document semantics instead of wasting tokens wrestling with OpenXML.

Adeu acts as an **intelligent proxy**, processing AI edits as safe, atomic transactions:

1. **Extract:** Translates the document (from disk or live Word) into LLM-friendly **CriticMarkup** with a **Semantic Appendix** of defined terms, cross-references, and likely typos. The agent starts with semantic structure, not raw data.
2. **Validate:** Acts as a strict safety gate. It protects the document's integrity by automatically blocking ambiguous text matches or invalid structural changes before they touch the file.
3. **Commit:** Translates the AI's text edits into native Word Track Changes. Adeu handles the complex XML under the hood, ensuring existing layouts, fonts, and margin comments are perfectly preserved.

Maintained by [Adeu](https://adeu.ai).

---

## Setup

**Prerequisite:** Adeu uses [uv](https://docs.astral.sh/uv/) for fast, isolated execution. The easiest way to install it is via pip:

```bash
pip install uv
```

<details>
<summary><b>Alternative OS-Specific Installers</b></summary>
<br>

**macOS**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
</details>

### Claude Desktop Integration

To instantly add Adeu to **Claude Desktop**, run:

```bash
uvx adeu init
```

> [!IMPORTANT]
> This command **automatically detects and updates** your `claude_desktop_config.json`.
> **Restart Claude Desktop** afterward to load the new tools.

### Verify It's Working

Once Claude Desktop has restarted, you can confirm Adeu is connected by typing the following message directly into Claude:

> **"Can you read a DOCX file using the Adeu tool?"**

If everything is set up correctly, Claude will confirm it has access to the Adeu tools and describe what it can do. If it doesn't mention Adeu or says it doesn't have file tools, double-check that you restarted Claude Desktop after running `uvx adeu init`.

<details>
<summary><b>Manual / Other MCP Client Configuration</b></summary>
<br>
If you are using another MCP client (like Cursor, Windsurf, or a custom app), add the following to your MCP configuration file.

Because Adeu requires Python 3.12+, `uvx` will automatically handle downloading the correct Python version and running the server:

```json
{
  "mcpServers": {
    "adeu": {
      "command": "uvx",
      "args": ["--from", "adeu", "adeu-server"]
    }
  }
}
```

</details>

---

## Workflows

### 1. For Agents (Claude / MCP)

Adeu runs as a Model Context Protocol (MCP) server. It provides agents with specific tools to read, review, and edit documents safely.

> **MCP Apps UI:** The `read_docx` tool supports the latest **MCP Apps UI** protocol. When an agent reads a document, Adeu dynamically renders a custom, interactive Markdown UI view directly inside your Claude chat window—allowing you to visually review the extracted text and formatting alongside the AI's reasoning!

**Recommended Agent Prompt:**
While Adeu's tools automatically describe their own schemas to the LLM, you can guarantee the best behavioral results by adding this context to Claude's **Project Instructions** or your agent's System Prompt:

> **Role:** Document Specialist
> **Tools:**
>
> - `read_docx(clean_view=True)`: Read the final "clean" version of the text to understand context.
> - `process_document_batch`: **Commit & Negotiate Mode.** Apply a unified list of changes. Use `type: "modify"` for specific search-and-replace text edits, and `type: "accept"`, `"reject"`, or `"reply"` to manage existing Track Changes and Comments by ID.
> - `sanitize_docx`: **Pre-Send Scrub.** Strip dangerous metadata, author names, and internal tracking IDs before sharing. Can preserve existing markup (`keep_markup=True`) or generate a clean delta against a baseline.

#### Live MS Word Integration
If you are running on Windows with Microsoft Word installed, Adeu can act as a real-time copilot, editing the active document right in front of you.
- `read_active_word_document`: Extracts text, tracked changes, and comments directly from the live, open Word window.
- `process_active_word_batch`: Translates the LLM's edits into native COM macros, watching Word type, delete, and add comments on the canvas automatically.

### 2. For Builders (Python SDK)

If you are building a legal-tech application or an automated pipeline, use the `RedlineEngine` directly. It handles the heavy lifting of XML manipulation.

```python
from adeu import RedlineEngine, ModifyText
from io import BytesIO

# 1. Load the contract
with open("MSA.docx", "rb") as f:
    stream = BytesIO(f.read())

# 2. Define the edit (e.g., from an LLM response)
# Adeu uses fuzzy matching to locate the target text, even if whitespace varies.
edit = ModifyText(
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

# Scrub author metadata and internal trackers, but keep the visual redlines for the counterparty
adeu sanitize redline.docx -o clean.docx --keep-markup --author "My Firm" --report
```

---

## Key Features

### Format Safety

Adeu does not "rewrite" the document. It patches it.

- **Images & Layouts:** Untouched.
- **Numbering & Headers:** Preserved.
- **Tables & Lists:** Complex gridspans and multi-level legal numbering are explicitly protected.
- **Complex XML:** It only modifies the text runs targeted by the edit.

### CriticMarkup Representation

Intermediate representations matter. Adeu uses [CriticMarkup](http://criticmarkup.com/) to visualize changes.

| Markup       | Meaning   | Example                   |
| :----------- | :-------- | :------------------------ |
| `{--text--}` | Deletion  | `{--Tenant--}`            |
| `{++text++}` | Insertion | `{++Lessee++}`            |
| `{>>text<<}` | Comment   | `{>>Clarify this term<<}` |

### Semantic Appendix

Contracts are full of landmines that an LLM will miss on a first pass: defined terms used inconsistently, broken cross-references, and OCR-style typos in messy documents. Adeu pre-computes these on extract and hands the agent a structured appendix alongside the text.

### Intelligent Mapping

Word documents are messy. A word like "Contract" might be split into XML runs like `["Con", "tract"]` due to spellcheck or formatting history.

- **Run Coalescing:** Adeu normalizes these splits so the AI sees "Contract".
- **Fuzzy Matching:** It handles minor whitespace discrepancies between the LLM's memory and the actual document content.

### Metadata Sanitization

Existing metadata scrubbers break redlines or silently strip data. Adeu's `sanitize` command surgically removes dangerous trackers (rsids, templates, internal paths, timestamps) and orphaned content while preserving valid track changes. Crucially, it generates a transparent audit report proving exactly what was stripped and what will be visible to the recipient.

---

## Adeu Cloud

By default, the core Adeu redlining engine and local file tools are fully open-source and execute entirely on your machine. **Adeu never phones home with your local documents** (though your chosen LLM provider will naturally process the text the agent reads).

However, you can explicitly opt-in to connect your MCP server to **Adeu Cloud** to unlock:

- **End-to-End Workflows (Email):** Because contracts travel via email, Adeu Cloud allows agents to securely fetch email threads, extract counterparty DOCX attachments for review, and draft replies with your newly sanitized redlines attached.
- **Advanced Document Validation:** Run complex, multi-document semantic validation tasks asynchronously. By securely routing these massive contexts to Adeu Cloud for processing, you prevent your local AI agent from exhausting its context window or hitting rate limits.

[Learn more about Adeu Cloud](https://adeu.ai).

---

## Contributing

We welcome contributions from the community! Whether it's fixing bugs, adding capabilities, or improving documentation, please see our [Contributing Guide](CONTRIBUTING.md) for instructions on setting up the local `uv` environment, running tests, and understanding the project's strict XML safety guidelines.

---

## License

MIT License. Open source and free to use in commercial applications.
