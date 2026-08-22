import { split_structural_appendix } from "@adeu/core";

export const MARKDOWN_UI_URI = "ui://adeu/markdown-ui";

export const MCP_ID_DISCOVERY_HINT =
  "Call `read_docx` with `mode='changes'` on the document again to list the current change (Chg:) and comment (Com:) ids — ids shift between document states.";

export function split_projection(text: string): [string, string] {
  const [body, appendix] = split_structural_appendix(text);
  return [appendix ? body.replace(/\n\n---$/, "") : body, appendix];
}

export function handleServerCliArgs(
  argv: string[],
  packageVersion: string,
): string | null {
  if (argv.includes("--version") || argv.includes("-v")) {
    return `adeu-mcp-server ${packageVersion}`;
  }
  if (argv.includes("--help") || argv.includes("-h")) {
    return [
      "Usage: adeu-mcp-server [options]",
      "",
      "Adeu MCP server (stdio transport, zero-dependency Node engine).",
      "Started by MCP hosts such as Claude Desktop; it reads JSON-RPC on stdin.",
      "",
      "Options:",
      "  -h, --help     Show this help and exit",
      "  -v, --version  Print the server version and exit",
      "",
      "Docs: https://github.com/dealfluence/adeu",
    ].join("\n");
  }
  return null;
}
