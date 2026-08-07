// FILE: node/packages/mcp-server/src/repro_qa_round3_2026_07_24.test.ts
/**
 * MCP-server-layer repro tests for the Adeu MCP QA Report — Round 3
 * (2026-07-24, black-box QA of v1.30.0+1fd5285):
 *
 *   3.3 (S3)  read_docx's file-not-found error dumps the ENTIRE directory
 *             listing (~300 filenames, thousands of tokens in the QA run)
 *             into one error string instead of a capped nearest-match
 *             suggestion list, and the trailing "Provide an absolute path"
 *             hint fires even when the caller's path WAS absolute.
 *   3.4 (S3)  Purely informational notes ("… the action itself succeeded")
 *             are rendered under a header literally called "Skipped
 *             Details" — rename to "Notes".
 *   3.10 (S3) A regex alternation returns the SAME paragraph once per
 *             branch hit (3 entries, mutually inconsistent occurrence
 *             counts) instead of one deduped entry with all hits
 *             highlighted.
 *   3.12 (S3) Search snippets slice at line boundaries, so a match whose
 *             line ends inside a multi-line {>>…<<} bubble renders an
 *             unterminated annotation and the read-before-act loop cannot
 *             harvest ids/pairings from search results.
 *
 * (Engine-level round-3 findings live in
 * node/packages/core/src/repro_qa_round3_2026_07_24.test.ts; the Python
 * mirrors live in python/tests/test_repro_qa_round3_2026_07_24.py.)
 *
 * Every test is written test-first: it fails on current main and passes
 * once the finding is fixed.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import {
  existsSync,
  mkdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { formatBatchResult } from "./index.js";
import { build_search_response } from "./response-builders.js";

// ---------------------------------------------------------------------------
// 3.3: ENOENT error — capped listing, no bogus absolute-path hint
// ---------------------------------------------------------------------------

describe("QA round 3, 3.3: read_docx file-not-found error", () => {
  let serverProc: ChildProcess;
  let crowdedDir: string;

  beforeAll(async () => {
    // A directory crowded with .docx files, like the QA workspace (~300).
    crowdedDir = join(tmpdir(), `adeu_qa3_dirdump_${process.pid}`);
    mkdirSync(crowdedDir, { recursive: true });
    for (let i = 0; i < 40; i++) {
      writeFileSync(join(crowdedDir, `fixture_${String(i).padStart(2, "0")}.docx`), "");
    }

    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error("MCP server not built. Run 'npm run build' before tests.");
    }
    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    rmSync(crowdedDir, { recursive: true, force: true });
  });

  function sendRpc(method: string, params: any, id: number = 1): Promise<any> {
    return new Promise((resolvePromise, reject) => {
      const timeout = setTimeout(() => reject(new Error("RPC Timeout")), 5000);
      const listener = (data: Buffer) => {
        const lines = data.toString().trim().split("\n");
        for (const line of lines) {
          if (!line.startsWith("{")) continue;
          try {
            const res = JSON.parse(line);
            if (res.id === id) {
              clearTimeout(timeout);
              serverProc.stdout?.removeListener("data", listener);
              resolvePromise(res);
            }
          } catch {
            // Ignore incomplete chunks
          }
        }
      };
      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  it("caps the available-files listing instead of dumping the directory", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: join(crowdedDir, "DEF_DOES_NOT_EXIST.docx"),
        },
      },
      301,
    );

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    expect(text).toContain("file not found:");

    // The QA run got ~300 filenames (thousands of tokens) in one error
    // string. A capped nearest-match list (~10 suggestions) is enough for
    // one-turn self-correction; the count includes the echoed request path.
    const listed = (text.match(/\.docx/g) || []).length;
    expect(
      listed,
      `the error dumps the whole directory (${listed} .docx mentions) ` +
        `instead of a capped suggestion list (QA round 3, finding 3.3):\n${text}`,
    ).toBeLessThanOrEqual(12);
  });

  it("does not tell an absolute-path caller to provide an absolute path", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: join(crowdedDir, "DEF_DOES_NOT_EXIST.docx"),
        },
      },
      302,
    );

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    expect(
      text,
      "the relative-path hint fires even though the caller already provided " +
        "an absolute path (QA round 3, finding 3.3)",
    ).not.toContain("Provide an absolute path");
  });
});

// ---------------------------------------------------------------------------
// 3.4: informational notes must not be headed "Skipped Details"
// ---------------------------------------------------------------------------

describe("QA round 3, 3.4: informational notes header", () => {
  it("does not file success-notes under 'Skipped Details'", () => {
    const stats = {
      actions_applied: 1,
      actions_skipped: 0,
      actions_already_resolved: 1,
      edits_applied: 0,
      edits_skipped: 0,
      edits: [],
      skipped_details: [
        "- Note: Action 2 ('reject' on Chg:2) had no additional effect — " +
          "the change was already resolved together with its replacement " +
          "pair by an earlier action in this batch. Counted as " +
          "already_resolved, not applied.",
      ],
    };

    const res = formatBatchResult(stats, "dummy_processed.docx");
    expect(res).toContain("had no additional effect"); // the note itself stays
    expect(
      res,
      'purely informational notes ("the action itself succeeded" / ' +
        '"already_resolved") are filed under a header called "Skipped ' +
        'Details" — misleading; rename to "Notes" (QA round 3, finding 3.4)',
    ).not.toContain("Skipped Details");
  });
});

// ---------------------------------------------------------------------------
// 3.10: duplicate/overlapping matches for one paragraph
// ---------------------------------------------------------------------------

describe("QA round 3, 3.10: search dedupe per paragraph", () => {
  it("renders one entry for a paragraph with several alternation hits", () => {
    const body =
      "# Section One\n\n" +
      "This deal between Dealfluence and the Com: holder grants limited " +
      "rights to the platform.\n\n" +
      "Unrelated closing paragraph.";

    const res = build_search_response(
      body,
      "Com:|Dealfluence|limited rights",
      true,
      true,
      undefined,
      "dummy.docx",
    );
    const text: string = res.content[0].text;
    const entries = (text.match(/### Match/g) || []).length;
    expect(
      entries,
      "one paragraph with 3 alternation-branch hits rendered " +
        `${entries} separate match entries (each with different bold ` +
        "placement and occurrence counts) — dedupe matches per paragraph " +
        "and highlight all hits in one entry (QA round 3, finding 3.10)",
    ).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// 3.12: snippets truncate mid-annotation
// ---------------------------------------------------------------------------

describe("QA round 3, 3.12: search snippet annotation truncation", () => {
  it("never leaves a {>>…<<} annotation unterminated in a snippet", () => {
    const body =
      "# Section One\n\n" +
      "Invoiced charges are due net {--ten--}{++twenty++}{>>[Chg:1 delete] " +
      "Adeu AI (pairs with Chg:2)\n" +
      "[Chg:2 insert] Adeu AI (pairs with Chg:1)\n" +
      "[Com:0] Reviewer @ 2026-01-22T14:13:00Z: chained change<<} days " +
      "from the invoice date.\n\n" +
      "Unrelated closing paragraph.";

    const res = build_search_response(
      body,
      "Invoiced charges",
      false,
      true,
      undefined,
      "dummy.docx",
    );
    const text: string = res.content[0].text;
    const opened = (text.match(/\{>>/g) || []).length;
    const closed = (text.match(/<<\}/g) || []).length;
    expect(
      opened,
      `search snippet truncates mid-annotation (${opened} '{>>' opened, ` +
        `${closed} '<<}' closed) — the agent cannot harvest ids/pairings ` +
        `from search results (QA round 3, finding 3.12):\n${text}`,
    ).toBe(closed);
  });
});
