// FILE: node/packages/mcp-server/src/mcp.schema-gaps.test.ts
//
// Guards the MCP boundary's HONESTY: what the server advertises over `tools/list`
// (the schema + documentation an LLM sees) must match what the tools really do.
// Each block below closed a gap where an agent, behaving exactly as documented,
// was either misled or kept from a capability that exists — "under-documented
// power is unused power." These tests now assert the corrected state and fail if
// any gap regresses.
//
// Gaps closed:
//   • process_document_batch — `changes` now publishes a typed item schema so the
//     six DocumentChange variants are discoverable (ADEU_TOOL_ISSUES #1), and the
//     real `match_mode`/`regex` options are documented in both schema and prose
//     (#10). Both are still proven honored at the live boundary.
//   • read_docx — its description carries the build stamp exactly once (UI tools
//     were previously double-wrapped and stamped twice).
//   • diff_docx_files — described as the custom `@@ Word Patch @@` format it
//     actually emits, no longer mislabeled a "unified diff".
//   • finalize_document — discloses that `protection_mode:'encrypt'` is unsupported
//     in the zero-dependency Node build and falls back to a read-only lock.

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject } from "@adeu/core";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

const CHANGE_VARIANTS = [
  "modify",
  "accept",
  "reject",
  "reply",
  "insert_row",
  "delete_row",
] as const;

const BUILD_STAMP_RE = /\[Adeu v[^\]]*\]/g;

describe("MCP tools — advertised schema/docs match real capability", () => {
  let serverProc: ChildProcess;
  let allTools: any[] = [];
  const tempPaths: string[] = [];

  // Fixtures
  let pdbFixture: string; // repeated phrase + currency, for match_mode/regex proofs
  let diffOrig: string;
  let diffMod: string;
  let finalizeInput: string;
  let multiPageFixture: string;

  const getTool = (name: string) => allTools.find((t) => t.name === name);

  // --- Robust line-buffered JSON-RPC plumbing over stdio ---
  const pending = new Map<number, (msg: any) => void>();
  let rpcId = 100;
  let stdoutBuffer = "";

  function rpc(method: string, params: any): Promise<any> {
    const id = ++rpcId;
    return new Promise((resolveRpc, rejectRpc) => {
      const timeout = setTimeout(
        () => rejectRpc(new Error(`RPC timeout for ${method}`)),
        15000,
      );
      pending.set(id, (msg) => {
        clearTimeout(timeout);
        resolveRpc(msg);
      });
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  function notify(method: string, params: any): void {
    serverProc.stdin?.write(
      JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n",
    );
  }

  // Build a docx from a list of paragraph strings (cloning the empty fixture and
  // clearing its body — `@adeu/core` does not export its test-utils). Tracks the
  // path for cleanup.
  async function buildDoc(paragraphs: string[]): Promise<string> {
    const initialPath = resolve(
      __dirname,
      "../../../../shared/fixtures/initial.docx",
    );
    const doc = await DocumentObject.load(readFileSync(initialPath));
    const body = doc.element;
    while (body.firstChild) body.removeChild(body.firstChild);

    for (const text of paragraphs) {
      const xmlDoc = body.ownerDocument!;
      const p = xmlDoc.createElement("w:p");
      const r = xmlDoc.createElement("w:r");
      const t = xmlDoc.createElement("w:t");
      t.textContent = text;
      if (/\s/.test(text)) t.setAttribute("xml:space", "preserve");
      r.appendChild(t);
      p.appendChild(r);
      body.appendChild(p);
    }

    const outPath = join(
      tmpdir(),
      `adeu_schemagap_${Date.now()}_${tempPaths.length}.docx`,
    );
    writeFileSync(outPath, await doc.save());
    tempPaths.push(outPath);
    return outPath;
  }

  function tempOut(label: string): string {
    const p = join(
      tmpdir(),
      `adeu_schemagap_out_${label}_${Date.now()}_${tempPaths.length}.docx`,
    );
    tempPaths.push(p);
    return p;
  }

  beforeAll(async () => {
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before tests.",
      );
    }

    serverProc = spawn("node", [serverPath]);
    serverProc.stdout?.on("data", (data: Buffer) => {
      stdoutBuffer += data.toString();
      let idx: number;
      while ((idx = stdoutBuffer.indexOf("\n")) !== -1) {
        const line = stdoutBuffer.slice(0, idx).trim();
        stdoutBuffer = stdoutBuffer.slice(idx + 1);
        if (!line.startsWith("{")) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            const cb = pending.get(msg.id)!;
            pending.delete(msg.id);
            cb(msg);
          }
        } catch {
          // ignore non-JSON / partial lines
        }
      }
    });

    // Proper MCP handshake, then snapshot the advertised tool list.
    await rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "schema-gap-test", version: "0.0.0" },
    });
    notify("notifications/initialized", {});

    const list = await rpc("tools/list", {});
    allTools = list.result.tools ?? [];

    pdbFixture = await buildDoc([
      "The Confidential Information shall remain protected.",
      "Some unrelated clause about delivery schedules.",
      "The Confidential Information shall not be disclosed.",
      "Setup fee is $500 due on signing.",
    ]);
    diffOrig = await buildDoc(["The quick brown fox.", "Second clause stays."]);
    diffMod = await buildDoc([
      "The slow green turtle.",
      "Second clause stays.",
    ]);
    finalizeInput = await buildDoc(["Some content to finalize."]);

    const filler =
      "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur.";
    const multiParas: string[] = [];
    for (let i = 0; i < 200; i++) {
      multiParas.push(`Paragraph ${i + 1}: ${filler}`);
    }
    multiPageFixture = await buildDoc(multiParas);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    for (const p of tempPaths) {
      if (existsSync(p)) {
        try {
          unlinkSync(p);
        } catch {
          // best-effort cleanup
        }
      }
    }
  });

  // ======================================================================
  // process_document_batch — ADEU_TOOL_ISSUES #1: `changes` items are typed
  // ======================================================================
  describe("process_document_batch #1: `changes` publishes a typed item schema", () => {
    it("exposes a `changes.items` schema enumerating all six change variants", () => {
      const pdbTool = getTool("process_document_batch");
      expect(
        pdbTool,
        "process_document_batch must be advertised",
      ).toBeDefined();

      const changesProp = pdbTool.inputSchema?.properties?.changes;
      expect(changesProp?.type).toBe("array");

      const items = changesProp.items;
      expect(
        items,
        "changes.items must describe the change shape",
      ).toBeTruthy();

      // Every DocumentChange discriminator is now machine-discoverable.
      const itemsJson = JSON.stringify(items);
      for (const variant of CHANGE_VARIANTS) {
        expect(
          itemsJson,
          `variant '${variant}' should be discoverable`,
        ).toContain(variant);
      }
    });

    it("exposes the per-variant fields (target_text / new_text / target_id / text), not just prose", () => {
      const itemsJson = JSON.stringify(
        getTool("process_document_batch").inputSchema.properties.changes.items,
      );
      for (const field of ["target_text", "new_text", "target_id", "text"]) {
        expect(itemsJson).toContain(field);
      }
    });
  });

  // ======================================================================
  // process_document_batch — ADEU_TOOL_ISSUES #10: match_mode / regex surfaced
  // ======================================================================
  describe("process_document_batch #10: match_mode / regex are documented and honored", () => {
    it("documents `match_mode` and `regex` in both the schema and the description", () => {
      const pdbTool = getTool("process_document_batch");
      const schemaJson = JSON.stringify(pdbTool.inputSchema).toLowerCase();
      const description: string = (pdbTool.description ?? "").toLowerCase();

      // Discoverable from the schema (the typed change item)...
      expect(schemaJson).toContain("match_mode");
      expect(schemaJson).toContain("regex");
      // ...and called out in the prose guidance too.
      expect(description).toContain("match_mode");
      expect(description).toContain("regex");
    });

    // B5: the description used to promise "nothing is applied" on any
    // failure, which the salvage default makes a lie. The replacement prose
    // has to fit the SAME ~2048-char client truncation budget, or the
    // guidance it replaces is invisible anyway.
    it("documents the salvage default and still fits the 2048-char client budget", () => {
      const description: string = getTool("process_document_batch").description;
      expect(description).toContain("PARTIAL: applied K of N");
      expect(description).toContain("partial=false");
      expect(description).not.toContain("rejects the whole batch transactionally");
      expect(
        description.length,
        `process_document_batch description is ${description.length} chars — ` +
          "clients truncate at ~2048 and the tail never reaches the model",
      ).toBeLessThan(2048);
    });

    it("honors match_mode:'all' at the live MCP boundary — edits every occurrence (2)", async () => {
      const outPath = join(tmpdir(), `adeu_schema_gap_all_${Date.now()}.docx`);
      tempPaths.push(outPath);
      const res = await rpc("tools/call", {
        name: "process_document_batch",
        arguments: {
          reasoning: "test",
          original_docx_path: pdbFixture,
          author_name: "Schema Gap Test",
          changes: [
            {
              type: "modify",
              target_text: "The Confidential Information",
              new_text: "The Proprietary Data",
              match_mode: "all",
            },
          ],
          output_path: outPath,
        },
      });

      const text: string = res.result.content[0].text;
      expect(res.result.isError).toBeFalsy();
      expect(text).toMatch(/2 occurrences/);
      expect(text).toContain("`all`");
    });

    it("honors regex:true (with a capture group) at the live MCP boundary", async () => {
      const outPath = join(tmpdir(), `adeu_schema_gap_regex_${Date.now()}.docx`);
      tempPaths.push(outPath);
      const res = await rpc("tools/call", {
        name: "process_document_batch",
        arguments: {
          reasoning: "test",
          original_docx_path: pdbFixture,
          author_name: "Schema Gap Test",
          changes: [
            {
              type: "modify",
              // A regex, not a literal — `target_text` never appears verbatim in
              // the doc, so a successful edit proves regex mode was honored.
              target_text: "\\$(\\d+)",
              new_text: "USD $1",
              regex: true,
            },
          ],
          output_path: outPath,
        },
      });

      const text: string = res.result.content[0].text;
      expect(res.result.isError).toBeFalsy();
      expect(text).toContain("{++USD ++}500"); // capture group $1 substituted
    });
  });

  // ======================================================================
  // read_docx — build stamp & discoverable page ranges
  // ======================================================================
  describe("read_docx: build stamp & discoverable page ranges", () => {
    it("stamps the build tag once — UI tools are no longer double-wrapped", () => {
      const readDocx = getTool("read_docx");
      expect(readDocx, "read_docx must be advertised").toBeDefined();

      const stamps = readDocx.description.match(BUILD_STAMP_RE) ?? [];
      expect(stamps.length).toBe(1);

      // Parity with a plain (non-UI) tool, which was always stamped once.
      const pdbStamps =
        getTool("process_document_batch").description.match(BUILD_STAMP_RE) ??
        [];
      expect(pdbStamps.length).toBe(1);
    });

    it("advertises page range support ('2-6') in tool description and stays under 2048 chars", () => {
      const readDocx = getTool("read_docx");
      expect(readDocx, "read_docx must be advertised").toBeDefined();
      expect(readDocx.description).toContain("'2-6'");
      expect(readDocx.description.length).toBeLessThan(2048);
    });

    it("returns two page banners when called with page: '2-3' at live MCP boundary", async () => {
      const res = await rpc("tools/call", {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: multiPageFixture,
          page: "2-3",
        },
      });

      const text: string = res.result.content[0].text;
      expect(res.result.isError).toBeFalsy();
      expect(text).toContain("Page 2 of");
      expect(text).toContain("Page 3 of");
      expect(text).not.toContain("Page 1 of");
    });
  });

  // ======================================================================
  // diff_docx_files — described as the Word Patch format it actually emits
  // ======================================================================
  describe("diff_docx_files: described as the Word Patch format it emits", () => {
    it("describes its output as a Word Patch, not a 'unified diff'", () => {
      const desc = getTool("diff_docx_files").description.toLowerCase();
      expect(desc).not.toContain("unified diff");
      expect(desc).toContain("word patch");
    });

    it("emits the custom `@@ Word Patch @@` format at runtime (matching its description)", async () => {
      const res = await rpc("tools/call", {
        name: "diff_docx_files",
        arguments: {
          reasoning: "test",
          original_path: diffOrig,
          modified_path: diffMod,
          compare_clean: true,
        },
      });

      const text: string = res.result.content[0].text;
      expect(res.result.isError).toBeFalsy();
      expect(text).toContain("@@ Word Patch @@");
      // It is NOT a standard line-based unified diff (no `@@ -l,s +l,s @@` header).
      expect(text).not.toMatch(/@@ -\d+(,\d+)? \+\d+(,\d+)? @@/);
    });
  });

  // ======================================================================
  // finalize_document — encrypt fallback is disclosed
  // ======================================================================
  describe("finalize_document: discloses the encrypt → read-only fallback", () => {
    it("advertises `encrypt` honestly — dropped from the enum, or its fallback disclosed", () => {
      const finalizeTool = getTool("finalize_document");
      const enumVals: string[] =
        finalizeTool.inputSchema.properties.protection_mode.enum;
      const desc: string = (finalizeTool.description ?? "").toLowerCase();

      const honest =
        !enumVals.includes("encrypt") ||
        (desc.includes("encrypt") &&
          /read-only|falls back|fallback|unsupported/.test(desc));
      expect(
        honest,
        "encrypt must be dropped from the Node enum or its read-only fallback disclosed",
      ).toBe(true);
    });

    it("downgrades encrypt to a read-only lock at runtime, with a warning (matching the disclosure)", async () => {
      const res = await rpc("tools/call", {
        name: "finalize_document",
        arguments: {
          reasoning: "test",
          file_path: finalizeInput,
          output_path: tempOut("finalize_encrypt"),
          protection_mode: "encrypt",
        },
      });

      const text: string = res.result.content[0].text;
      expect(res.result.isError).toBeFalsy();
      expect(text).toContain("Encryption mode");
      expect(text.toLowerCase()).toContain("unsupported");
      expect(text).toMatch(/read-only/i);
    });
  });

  // ======================================================================
  // read_docx — mode='changes', changes_author, changes_offset
  // ======================================================================
  describe("read_docx: mode='changes', changes_author, changes_offset", () => {
    it("advertises 'changes' in mode enum, and advertises changes_author and changes_offset (defaulting to 0)", () => {
      const readDocx = getTool("read_docx");
      expect(readDocx).toBeDefined();

      const modeProp = readDocx.inputSchema.properties.mode;
      expect(modeProp.enum).toContain("changes");

      const props = readDocx.inputSchema.properties;
      expect(props.changes_author).toBeDefined();
      expect(props.changes_offset).toBeDefined();
      expect(props.changes_offset.default).toBe(0);
    });

    it("mentions mode='changes' in read_docx description for discoverability", () => {
      const readDocx = getTool("read_docx");
      expect(readDocx.description).toContain("mode='changes'");
    });

    it("returns tracked changes ledger for mode='changes' on fixture with tracked changes", async () => {
      const trackedFixture = resolve(
        __dirname,
        "../../../../shared/conformance/fixtures/multi_author.docx",
      );
      const res = await rpc("tools/call", {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: trackedFixture,
          mode: "changes",
        },
      });

      expect(res.result.isError).toBeFalsy();
      const text: string = res.result.content[0].text;
      expect(text).toMatch(/^> \*\*File Path:\*\*/);
      expect(text).toContain("> **Changes ledger** —");
      expect(text).toContain("Chg:");
    });

    it("refuses mode='changes' with clean_view:true", async () => {
      const res = await rpc("tools/call", {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: pdbFixture,
          mode: "changes",
          clean_view: true,
        },
      });

      expect(res.result.isError).toBe(true);
      const text: string = res.result.content[0].text;
      expect(text).toContain("--clean-view cannot be used with mode='changes'.");
    });

    it("supports page ranges with mode='changes' without error", async () => {
      const denseFixture = resolve(
        __dirname,
        "../../../../shared/conformance/fixtures/dense_175.docx",
      );
      const res = await rpc("tools/call", {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: denseFixture,
          mode: "changes",
          page: "2-3",
        },
      });

      expect(res.result.isError).toBeFalsy();
      const text: string = res.result.content[0].text;
      expect(text).toContain("> **Changes ledger** —");
    });

    it("rejects non-integer changes_offset", async () => {
      const trackedFixture = resolve(
        __dirname,
        "../../../../shared/conformance/fixtures/multi_author.docx",
      );
      const res = await rpc("tools/call", {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: trackedFixture,
          mode: "changes",
          changes_offset: 1.5,
        },
      });

      const isValidationError =
        res.error?.code === -32602 || res.result?.isError === true;
      expect(isValidationError).toBe(true);
    });
  });
});
