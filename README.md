# Adeu: Track Changes for the LLM era

[![GitHub Repo stars](https://img.shields.io/github/stars/dealfluence/adeu?style=social)](https://github.com/dealfluence/adeu)
[![PyPI version](https://img.shields.io/pypi/v/adeu.svg)](https://pypi.org/project/adeu/)
[![npm version](https://img.shields.io/npm/v/@adeu/core.svg)](https://www.npmjs.com/package/@adeu/core)
[![Downloads](https://img.shields.io/pepy/dt/adeu)](https://pepy.tech/project/adeu)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)
[![Smithery](https://img.shields.io/badge/Smithery-Available-blue.svg)](https://smithery.ai/servers/adeu/adeu)
[![CI](https://github.com/dealfluence/adeu/actions/workflows/ci.yml/badge.svg)](https://github.com/dealfluence/adeu/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**LLMs speak Markdown; reviewers speak "Track Changes."**

Adeu is a **docx ↔ LLM translator**: a Model Context Protocol (MCP) server (Python and Node.js implementations) and accompanying SDKs that act as a **Virtual DOM for Microsoft Word**. It provides a two-way abstraction layer that lets AI agents freely edit document text without destroying the underlying formatting or complex DOCX XML.

While standard libraries like `python-docx` excel at generating documents from scratch, they fail at non-destructive redlining. Adeu solves this by translating `.docx` files into a token-efficient Markdown representation. This frees AI agents to focus entirely on document semantics instead of wasting tokens wrestling with OpenXML.

Adeu acts as an **intelligent proxy**, processing AI edits as safe, atomic transactions:

1. **Read:** Translates the document (from disk or live Word) into LLM-friendly **[CriticMarkup](https://fletcher.github.io/MultiMarkdown-6/syntax/critic.html)** with a **Semantic Appendix** of defined terms, cross-references, and likely typos. The agent starts with semantic structure, not raw data.
2. **Validate:** Acts as a strict safety gate. It protects the document's integrity by automatically blocking ambiguous text matches or invalid structural changes before they touch the file.
3. **Apply:** Translates the AI's text edits into native Word Track Changes. Adeu handles the complex XML under the hood, ensuring existing layouts, fonts, and margin comments are perfectly preserved.

Built and maintained by the team at [Adeu](https://adeu.ai).

---

## Installation

Adeu can be installed directly into AI assistants as an MCP server, used as a Claude Code plugin or Agent Skill, CLI tool, or used locally as a developer toolchain.

### Claude Code (Plugin)
Adeu ships as a [Claude Code plugin](https://docs.claude.com/en/docs/claude-code/plugins) with a built-in agent skill that teaches Claude how to use the engine effectively. Inside Claude Code:

```
/plugin marketplace add dealfluence/adeu
/plugin install adeu-redlining@adeu-skills
```

For best results, also connect either the Node MCP server (`npx -y @adeu/mcp-server`) or the Python MCP server (`uvx --from adeu adeu-server`). The plugin works without an MCP server too — it falls back to driving the `uvx adeu` CLI via Bash.

### Other Skills-Compatible Agents (Cursor, Windsurf, VS Code Copilot, etc.)
Adeu's redlining skill follows the open [Agent Skills specification](https://agentskills.io) and works with any compatible agent:

```bash
npx skills add dealfluence/adeu
```

The skill installs to your agent's skills directory and activates automatically when you ask Claude to redline, edit, or review a `.docx` file.

### Claude Desktop
You can install Adeu directly into Claude Desktop using the official extension package:
1. Download the latest `Adeu.mcpb` file from the [GitHub Releases](https://github.com/dealfluence/adeu/releases) page.
2. Open Claude Desktop and navigate to **Settings > Extensions**.
3. Click **Advanced settings** and find the Extension Developer section.
4. Click **Install Extension...**, select the downloaded `.mcpb` file, and follow the prompts.

### Gemini CLI
Adeu is available as a native [Gemini CLI extension](https://geminicli.com/extensions/). To install:
```bash
gemini extensions install https://github.com/dealfluence/adeu
```

### Other MCP Clients (Cursor, Windsurf, etc.)
For IDEs or clients that configure MCP servers via JSON, you can use either the Node.js or Python backend:

**Node.js**
```json
{
  "mcpServers": {
    "adeu": {
      "command": "npx",
      "args": ["-y", "@adeu/mcp-server"]
    }
  }
}
```

**Python (Required for Live MS Word integration on Windows)**
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

### Smithery
To install Adeu using the Smithery package manager:
```bash
npx -y @smithery/cli install adeu --client claude
```

---

## Agent Workflows

Adeu provides agents with specific tools to read, review, and edit documents safely.

> **MCP Apps UI:** The `read_docx` tool supports the MCP Apps UI protocol. When an agent reads a document, Adeu dynamically renders a custom, interactive Markdown view directly inside the chat window.

**Recommended Agent Prompt:**
You can guarantee the best behavioral results by adding this context to your agent's system prompt or project instructions:

> **Role:** Document Specialist
> **Tools:**
>
> - `read_docx(clean_view=True)`: Read the final "clean" version of the text to understand context. Use `search_query` and `page` filters to locate specific clauses without reading the whole document.
> - `process_document_batch`: **Commit & Negotiate Mode.** Apply a unified list of changes. Use `type: "modify"` for specific search-and-replace text edits (supports `match_mode="all"` and `regex=True` for bulk updates), and `type: "accept"`, `"reject"`, or `"reply"` to manage existing Track Changes and Comments by ID.
> - `finalize_document`: **Pre-Send Scrub.** Strip dangerous metadata, author names, and internal tracking IDs, lock the document (`protection_mode="read_only"`), and prepare it for distribution.

### Live MS Word Integration
If you are running on Windows with Microsoft Word installed, Adeu can act as a real-time copilot, editing the active document right in front of you. This requires running the Python MCP server backend (see Developer Tools below).

---

## Developer Tools (Python & TypeScript)

If you are building a legal-tech application, an automated pipeline, or want to use the local CLI, use our SDKs.

### The Python CLI
The Python toolchain is managed via [uv](https://docs.astral.sh/uv/).

```bash
pip install uv
uv tool install adeu

# Extract clean text for RAG or prompting
adeu extract contract.docx -o contract.md

# Generate a visual diff between two versions
adeu diff v1.docx v2.docx

# Apply edits to the DOCX
adeu apply contract.docx edits.json --author "Review Bot"

# Scrub author metadata and internal trackers
adeu sanitize redline.docx -o clean.docx --keep-markup --author "My Firm" --report
```

What the text projection preserves exactly, what it normalizes (lists,
styles, synthetic pages), and what stays read-only is specified in
[docs/FIDELITY.md](docs/FIDELITY.md).

### The Python SDK
```python
from adeu import RedlineEngine, ModifyText
from io import BytesIO

with open("MSA.docx", "rb") as f:
    stream = BytesIO(f.read())

edit = ModifyText(
    target_text="State of New York",
    new_text="State of Delaware",
    comment="Standardizing governing law."
)

engine = RedlineEngine(stream, author="AI Copilot")
engine.apply_edits([edit])

with open("MSA_Redlined.docx", "wb") as f:
    f.write(engine.save_to_stream().getvalue())
```

### The TypeScript SDK
The entire core parsing and diffing engine is also available in pure TypeScript.

```typescript
import { readFileSync, writeFileSync } from "fs";
import { DocumentObject, RedlineEngine } from "@adeu/core";

const buffer = readFileSync("MSA.docx");
const doc = await DocumentObject.load(buffer);

const engine = new RedlineEngine(doc, "AI Copilot");
engine.process_batch([{
  type: "modify",
  target_text: "State of New York",
  new_text: "State of Delaware",
  comment: "Standardizing governing law."
}]);

const outBuffer = await doc.save();
writeFileSync("MSA_Redlined.docx", outBuffer);
```

See the [@adeu/core documentation](https://github.com/dealfluence/adeu/tree/main/node/packages/core#readme) for full installation and usage details.

### n8n Community Node
Adeu ships as an [n8n](https://n8n.io) community node (`n8n-nodes-adeu`) for teams who prefer visual workflow automation over code. It exposes the full engine (extract Markdown, apply tracked changes with optional dry-run preview, generate diffs, and finalize documents) as drop-in nodes that work in both deterministic pipelines and AI Agent tool calls.

```bash
# In n8n: Settings → Community Nodes → Install: n8n-nodes-adeu
```

See the [n8n-nodes-adeu README](https://github.com/dealfluence/adeu/blob/main/node/packages/n8n-nodes-adeu/README.md) for installation, `$fromAI` recipes, and example workflows.

---

## LangChain Integration

`langchain-adeu` is an official integration package that exposes Adeu's local, offline-capable document manipulation tools directly to the LangChain ecosystem.

```bash
pip install langchain-adeu
```

Bundle its capabilities as tools in your agent workflow:
```python
from langchain_adeu import AdeuToolkit

# Instantiate and retrieve all document tools
tools = AdeuToolkit().get_tools()
```

Refer to the [LangChain Workspace Guide](langchain/README.md) for full development instructions and detailed parameters.

---

## Ecosystem & Integrations

Adeu is designed as a Virtual DOM for DOCX. Because we keep the core strictly focused on OpenXML safety, we maintain a dedicated [`ecosystem/`](ecosystem/) directory for third-party integrations.

The ecosystem folder hosts policies and guidelines for third-party contributions such as legal validation workflows, CLM sync scripts, and specialized multi-agent architectures.

**Are you a vendor or builder?** We welcome PRs to the ecosystem folder! Please see our [Vendor & Integration Policy](ecosystem/VENDOR_POLICY.md) to get started.

---

## Adeu Cloud

By default, the core Adeu redlining engine and local file tools are fully open-source and execute entirely on your machine. **Adeu never phones home with your local documents** (though your chosen LLM provider will naturally process the text the agent reads).

However, for teams requiring end-to-end workflows, you can connect to **Adeu Cloud** to unlock:

- **Email Processing & Fetching:** We offer an extended MCP server with secure email thread fetching, document extraction, and automated drafting capabilities to handle contracts directly from your inbox.

[Learn more about Adeu Cloud](https://adeu.ai).

---

## Contributing

We welcome contributions from the community! Whether it's fixing bugs, adding capabilities, or improving documentation, please see our [Contributing Guide](CONTRIBUTING.md) for instructions on setting up the local `uv` environment, running tests, and understanding the project's strict XML safety guidelines.

---

## License

MIT License. Open source and free to use in commercial applications.

