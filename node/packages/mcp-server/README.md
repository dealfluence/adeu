
# @adeu/mcp-server

[![GitHub Repo stars](https://img.shields.io/github/stars/dealfluence/adeu?style=social)](https://github.com/dealfluence/adeu)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**Native Microsoft Word Track Changes for AI Agents**

`@adeu/mcp-server` is a standalone Node.js [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that acts as a "Virtual DOM" for Microsoft Word documents. It provides AI agents (like Claude) with tools to safely read, edit, and sanitize `.docx` files without destroying underlying formatting or complex XML.

This package provides the exact same engine as the Python `adeu` CLI, but executes entirely via Node.js—making it ideal for environments where Python is unavailable or undesired.

## Usage with MCP Clients

You can add this server directly to your MCP client (like Claude Desktop, Cursor, or Windsurf) using `npx`. 

Add the following to your MCP configuration file (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "adeu-node": {
      "command": "npx",
      "args": ["-y", "@adeu/mcp-server"]
    }
  }
}
```

## Exposed Tools

Once connected, your AI agent will have access to the following tools:

- `read_docx`: Reads a DOCX file and returns LLM-friendly text with inline CriticMarkup (`{++inserted++}`, `{--deleted--}`) for Tracked Changes and Comments. Supports pagination, structural outlining, and semantic appendix extraction.
- `process_document_batch`: Applies a batch of search-and-replace text modifications, table edits, and comment replies to a document. Translates the LLM's edits into perfectly formatted native Word Track Changes.
- `accept_all_changes`: Accepts all tracked changes and removes all comments to produce a finalized clean document.
- `diff_docx_files`: Compares two DOCX files and returns a unified sub-word diff of their text content.
- `finalize_document`: Prepares a document for signature by applying native OOXML read-only locking and deep metadata sanitization.

## Documentation & Support
For full architectural details, prompt recommendations, and the project constitution, please visit the [main Adeu repository](https://github.com/dealfluence/adeu) or our [website](https://adeu.ai).

---

## Development Runbook Note: Workspace Linkage Trap

> [!WARNING]
> **Node MCP: a committed source fix is NOT a running fix.**
> `dist/` is gitignored and the server runs the compiled bundle. After ANY change to `node/packages/core` or `mcp-server`:
> 
> 1. **Relink dependencies:** `cd node && npm install` (relinks `@adeu/core` in the workspace; a copied — not symlinked — core is the #1 cause of "fix didn't take").
> 2. **Rebuild the workspace:** `npm run build` (core builds before mcp-server; confirm `core/dist` timestamp is updated).
> 3. **Verify:** Check that the build verification sentinel check succeeds (automatically runs postbuild, or can be run manually via `npm run build:verify` under `packages/mcp-server`).
> 4. **Restart the MCP server:** Restart the MCP server AND reconnect in the client to flush any cached process states.
> 5. **Confirm the build stamp:** Verify that the build stamp in the live server (via `server_info` or startup logs) matches your built git HEAD.