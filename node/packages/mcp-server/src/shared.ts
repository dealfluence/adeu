// FILE: node/packages/mcp-server/src/shared.ts
import { split_structural_appendix } from "@adeu/core";

export const MARKDOWN_UI_URI = "ui://adeu/markdown-ui";

export const MCP_ID_DISCOVERY_HINT =
  "Call `read_docx` with `mode='changes'` on the document again to list the current change (Chg:) and comment (Com:) ids — ids shift between document states.";

/**
 * The projection split this server layer works from — body without the
 * structural appendix OR the rule that introduces it.
 *
 * Python's doc cache projects with `include_appendix=False`
 * (doc_cache.py:159-164), so its body simply ends at the last body line. Node
 * projects WITH the appendix (one pass also feeds mode='appendix') and splits
 * it off afterwards — but the appendix block opens with a `"\n\n---"` rule
 * (domain.ts:369-372) that lands on the BODY side of the split:
 * `split_structural_appendix` rstrips whitespace only, identically in both
 * engines (pagination.ts:77, pagination.py:163 — both return 556 chars for
 * unicode.docx). Dropping that one separator makes Node's body the exact
 * string Python serves, paginates and measures: 551 chars, no dangling rule
 * at the end of `page='all'`.
 *
 * The separator is dropped once and only at the very end, so a document whose
 * own last block is a horizontal rule keeps it — as it does in Python's
 * appendix-free projection.
 */
export function split_projection(text: string): [string, string] {
  const [body, appendix] = split_structural_appendix(text);
  return [appendix ? body.replace(/\n\n---$/, "") : body, appendix];
}

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
