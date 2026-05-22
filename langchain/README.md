# langchain-adeu

[![PyPI version](https://img.shields.io/pypi/v/langchain-adeu.svg)](https://pypi.org/project/langchain-adeu/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**LangChain integration for [Adeu](https://adeu.ai) — Track Changes for Microsoft Word (.docx) in the LLM Era.**

This package wraps the local, cross-platform, and offline-capable subset of Adeu's document-editing engine as native LangChain tools. It enables LangChain and LangGraph agents to read, edit, diff, sanitize, and finalize Microsoft Word documents while preserving the underlying formatting, layout, custom styles, and XML structures.

---

## Installation

Install the package via `pip` or `uv`:

### Using pip
```bash
pip install langchain-adeu
```

### Using uv
```bash
uv add langchain-adeu
```

---

## Quick Start

Instantiate the `AdeuToolkit` and register its tools with a tool-calling chat model. This short example initializes an agent capable of managing docx operations.

```python
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_adeu import AdeuToolkit

# Load a tool-calling model
model = ChatOpenAI(model="anthropic:claude-sonnet-4-6")

# Initialize the toolkit
toolkit = AdeuToolkit()
tools = toolkit.get_tools()

# Create the agent
agent = create_agent(model=model, tools=tools)
```

---

## Worked Example: Multi-Tool Review & Redline Flow

Below is a complete, runnable workflow illustrating how an agent can read an existing draft, apply tracked changes, generate a visual diff, and sanitize metadata before sending it to a counterparty.

```python
from langchain_adeu import AdeuToolkit

# 1. Instantiate the toolkit
tools_map = {t.name: t for t in AdeuToolkit().get_tools()}

read_tool = tools_map["adeu_read_docx"]
apply_tool = tools_map["adeu_apply_changes"]
diff_tool = tools_map["adeu_diff_docx"]
sanitize_tool = tools_map["adeu_sanitize_docx"]

input_path = "MSA_draft.docx"
redline_path = "MSA_redlined.docx"
clean_path = "MSA_final.docx"

# 2. Read the document to extract text with active tracked changes & comments
# clean_view=False ensures the LLM sees inline CriticMarkup (e.g. {++inserted++})
read_result = read_tool.invoke({
    "file_path": input_path,
    "clean_view": False,
    "mode": "full",
    "page": 1
})
print("--- Document Contents ---\n", read_result)

# 3. Apply a batch of edits (tracked modifications + a comment reply)
apply_result = apply_tool.invoke({
    "file_path": input_path,
    "author_name": "AI Reviewer",
    "output_path": redline_path,
    "changes": [
        {
            "type": "modify",
            "target_text": "Governing Law shall be the State of New York.",
            "new_text": "Governing Law shall be the State of Delaware.",
            "comment": "Updating jurisdiction to corporate standard."
        },
        {
            "type": "reply",
            "target_id": "Com:1",
            "text": "Agreed. Applied jurisdiction change."
        }
    ]
})
print("\n--- Changes Applied ---\n", apply_result)

# 4. Generate a word-level diff to verify edits
diff_result = diff_tool.invoke({
    "original_path": input_path,
    "modified_path": redline_path,
    "compare_clean": True
})
print("\n--- Word-Level Diff ---\n", diff_result)

# 5. Sanitize document properties and remove author history for final delivery
# keep_markup=True preserves unresolved track changes while stripping metadata
sanitize_result = sanitize_tool.invoke({
    "file_path": redline_path,
    "output_path": clean_path,
    "keep_markup": True,
    "author": "Anonymous Advisor"
})
print("\n--- Sanitization Report ---\n", sanitize_result)
```

---

## Per-Tool Reference

| Tool Name | Purpose / When to Use | Key Input Parameters | Response Format / Output Shape |
| :--- | :--- | :--- | :--- |
| `adeu_read_docx` | Reads a `.docx` file into Markdown. Use `clean_view=False` to audit active track-changes. | `file_path` (str), `clean_view` (bool), `mode` (Literal), `page` (int) | `content_and_artifact` (Returns projected Markdown text + structured metadata artifact) |
| `adeu_apply_changes` | Commits a transactional batch of edits as native track-changes and comment threads. | `file_path` (str), `author_name` (str), `changes` (list[dict]), `output_path` (str) | `content_and_artifact` (Returns completion text + structured change stats) |
| `adeu_diff_docx` | Generates a word-level patch showing insertions and deletions between two files. | `original_path` (str), `modified_path` (str), `compare_clean` (bool) | `content` (Returns free-form `@@ Word Patch @@` visual text) |
| `adeu_accept_all_changes` | Resolves and bakes all tracked changes and format modifications into plain text. | `file_path` (str), `output_path` (str) | `content_and_artifact` (Returns completion text + artifact mapping paths) |
| `adeu_sanitize_docx` | Cleans document properties (author names, RSIDs, Custom XML, DMS traces). | `file_path` (str), `output_path` (str), `keep_markup` (bool), `accept_all` (bool) | `content_and_artifact` (Returns human-readable report text + structured cleanup stats) |

---

## What's NOT Included

This package intentionally focuses on **local, cross-platform, offline-capable** workflows. For the following, use the [Adeu MCP server](https://github.com/dealfluence/adeu) directly:

- **Live MS Word Interop** (Windows COM) — real-time edits on an active Microsoft Word canvas.
- **Adeu Cloud Features** — email fetching, multi-document asynchronous semantic validation.
- **MCP Apps UI** — interactive Markdown preview rendering inside custom client interfaces.

---

## Development & Testing

We use `uv` for dependency management and workspace isolation.

### Installation
Sync development and testing dependencies locally:
```bash
make install
```

### Running Tests
To run unit tests (isolated, socket-disabled):
```bash
make test
```

To run integration tests (requires real fixture `.docx` documents):
```bash
make integration_test
```

### Code Formatting & Linting
We enforce Ruff for formatting and linting:
```bash
make format
make lint
```

---

## License

MIT. See [LICENSE](../LICENSE).
