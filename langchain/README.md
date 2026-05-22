# FILE: langchain/README.md
# langchain-adeu

[![PyPI version](https://img.shields.io/pypi/v/langchain-adeu.svg)](https://pypi.org/project/langchain-adeu/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**LangChain tools for [Adeu](https://adeu.ai) — track-changes for DOCX in the LLM era.**

This package wraps the local, offline-capable subset of Adeu's document-editing engine as native [LangChain tools](https://docs.langchain.com/oss/python/langchain/tools), so LangChain and LangGraph agents can read, edit, diff, sanitize, and finalize Microsoft Word documents while preserving the underlying OOXML structure (formatting, comments, tracked changes).

## Installation

```bash
pip install langchain-adeu
```

## Quick Start

> 🚧 Tools API coming in the next step of this build. The skeleton is in place; tool implementations land next.

## What's Included

| Tool | Purpose |
|------|---------|
| `AdeuReadDocx` | Read a DOCX file into LLM-friendly Markdown with inline CriticMarkup for tracked changes and comments |
| `AdeuApplyChanges` | Apply a batch of edits and review actions as native Word Track Changes |
| `AdeuDiffDocx` | Generate a word-level diff between two DOCX files |
| `AdeuAcceptAllChanges` | Accept all tracked changes and produce a finalized document |
| `AdeuSanitizeDocx` | Strip dangerous metadata (author names, RSIDs, custom XML) with a full audit report |

Plus an `AdeuToolkit` class that bundles all five for convenient agent construction.

## What's NOT Included

This package intentionally focuses on **local, cross-platform, offline-capable** workflows. For the following, use the [Adeu MCP server](https://github.com/dealfluence/adeu) directly:

- **Live MS Word interop** (Windows COM) — real-time edits on an open Word document
- **Adeu Cloud features** — email fetching, multi-document semantic validation
- **MCP Apps UI** — interactive Markdown view rendered in the chat client

## License

MIT. See [LICENSE](../LICENSE).