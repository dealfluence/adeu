// FILE: node/packages/mcp-server/src/repro.qa_report_v8.test.ts
/**
 * MCP-server repro tests for the 2026-07-19 black-box QA and UX report on
 * 1.26.0 (adeu 1.26.0+0741eaf). Mirrors python/tests/test_repro_qa_report_v8.py
 * for the findings that live on the server surface:
 *
 *   F-06  `--help` / `--version` used to start the stdio server instead of
 *         printing and exiting — both the Python `adeu-server` and this
 *         Node bin share the defect
 *   F-10  search snippet highlighting collides with the document's own
 *         style markers (`**The **Supplier** _shall provide**_`)
 *
 * Every test fails against the commit preceding its fix.
 */

import { describe, it, expect } from "vitest";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { build_search_response, emphasizedSnippet } from "./response-builders.js";
import { handleServerCliArgs } from "./shared.js";

// ---------------------------------------------------------------------------
// F-06: --help/--version print and exit, never start the server
// ---------------------------------------------------------------------------

describe("QA v8 F-06: server --help/--version", () => {
  it("returns version text without serving", () => {
    expect(handleServerCliArgs(["--version"], "9.9.9")).toBe(
      "adeu-mcp-server 9.9.9",
    );
    expect(handleServerCliArgs(["-v"], "9.9.9")).toContain("9.9.9");
  });

  it("returns help text without serving", () => {
    const help = handleServerCliArgs(["--help"], "9.9.9");
    expect(help).toBeTruthy();
    expect(help!.toLowerCase()).toContain("usage");
    expect(help).toContain("--version");
  });

  it("returns null for a normal server start", () => {
    expect(handleServerCliArgs([], "9.9.9")).toBeNull();
  });

  it("built bin exits immediately on --version instead of serving", async () => {
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before tests.",
      );
    }

    const result = await new Promise<{ code: number | null; out: string }>(
      (resolvePromise, rejectPromise) => {
        const proc = spawn("node", [serverPath, "--version"], {
          stdio: ["pipe", "pipe", "pipe"],
        });
        let out = "";
        proc.stdout.on("data", (d) => (out += d.toString()));
        const timer = setTimeout(() => {
          proc.kill();
          rejectPromise(
            new Error("--version started the stdio server instead of exiting"),
          );
        }, 15_000);
        proc.on("exit", (code) => {
          clearTimeout(timer);
          resolvePromise({ code, out });
        });
        proc.on("error", (err) => {
          clearTimeout(timer);
          rejectPromise(err);
        });
      },
    );

    expect(result.code).toBe(0);
    expect(result.out).toContain("adeu-mcp-server");
  }, 30_000);
});

// ---------------------------------------------------------------------------
// F-10: search highlighting must not collide with existing style markers
// ---------------------------------------------------------------------------

describe("QA v8 F-10: search snippet marker collisions", () => {
  // The QA report's fixture projection: bold + italic runs around the match.
  const BODY =
    "# Master Services Agreement\n\n" +
    "**The Supplier** _shall provide_ the Services with reasonable skill and care.\n\n" +
    "Filler paragraph so the snippet has surrounding context.";

  it("strips existing markers before emphasizing the match", () => {
    const res = build_search_response(
      BODY,
      "Supplier.*provide",
      true,
      true,
      undefined,
      "doc.docx",
    );
    const md = res.structuredContent!.markdown as string;

    expect(md).not.toContain("**The **");
    expect(md).not.toContain("**_");
    expect(md).not.toContain("_**");
    expect(md).toContain("**Supplier shall provide**");
  });

  it("keeps plain-document snippets unchanged", () => {
    const res = build_search_response(
      "The quick brown fox jumps over the lazy dog.",
      "brown fox",
      false,
      true,
      undefined,
      "doc.docx",
    );
    const md = res.structuredContent!.markdown as string;
    expect(md).toContain("**brown fox**");
  });

  it("emphasizedSnippet handles a marker split across the match boundary", () => {
    // Match ends right before the closing italic marker: the suffix starts
    // with "_" whose word-edge context lives in the match fragment.
    const out = emphasizedSnippet(
      "**The Supplier** _",
      "shall provide",
      "_ the Services.",
    );
    expect(out).toBe("The Supplier **shall provide** the Services.");
  });
});
