// FILE: node/packages/mcp-server/src/shared.ts
export const MARKDOWN_UI_URI = "ui://adeu/markdown-ui";

/**
 * Minimal CLI handling BEFORE the stdio server starts: `--help` and
 * `--version` must print and exit like every other executable instead of
 * silently starting the transport (QA 2026-07-19 v8 F-06). Returns the text
 * to print (caller exits without serving), or null to proceed with server
 * startup. Unknown arguments are tolerated — MCP hosts append their own
 * flags. Lives here (not index.ts) so tests can import it without booting
 * the server.
 */
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
