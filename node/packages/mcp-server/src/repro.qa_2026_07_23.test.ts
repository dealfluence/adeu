// FILE: node/packages/mcp-server/src/repro.qa_2026_07_23.test.ts
//
// Repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box QA of
// v1.29.0+4bb70f9). Findings covered, exercised end-to-end against the REAL
// compiled MCP server over stdio JSON-RPC (mirrors mcp.bugs.test.ts /
// mcp.schema-gaps.test.ts):
//
//   F10 (medium) — insert_row/delete_row row-op fields (notably `cells`) are
//                  not documented in the tool DESCRIPTION. MCP clients are
//                  known to strip the typed `changes.items` schema down to {}
//                  in transit, so the prose description is the only channel
//                  guaranteed to reach the model.
//   F11 (medium) — stale-ID errors recommend CLI commands (`adeu markup`,
//                  `adeu extract`) that an MCP caller cannot run; they must
//                  advise calling `read_docx` again.
//   F14 (low)    — diff_docx_files on identical files prints only the two
//                  header lines instead of an explicit no-differences line.
//   F16 (low)    — relative-path file-not-found error drops the directory
//                  from the echoed path and never says absolute paths are
//                  required.
//   F17 (low)    — silent overwrites: (a) a repeated default-named run
//                  overwrites <name>_processed.docx without warning;
//                  (b) output_path == input path silently overwrites the
//                  source document.
//   F18 (low)    — accept_all_changes on a change-free document reports
//                  "Accepted all changes" instead of saying it was a no-op.
//   F19 (low)    — a .docx whose bytes are plain text errors with the bare
//                  fflate message "invalid zip data" with no hint that the
//                  file is not a valid .docx.
//
// Every test in this file is written test-first: it fails on current main and
// passes once the finding is fixed.
//
// NOT covered (verified NOT to reproduce at the raw stdio server boundary on
// current main, 2026-07-23):
//   F3  — tools/list advertises process_document_batch with
//         required = ["reasoning","original_docx_path","author_name","changes"],
//         so schema and runtime already agree that author_name is required.
//         The QA report's "only `changes` is required" observation matches the
//         MCP *client's* transformed view of the schema, not the server's.
//   F22 (page sub-item) — the server publishes `page` as
//         {"anyOf":[{"type":"number"},{"type":"string"}]}; the `{}` the QA saw
//         is the same client-side anyOf-stripping.

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  copyFileSync,
  existsSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject, RedlineEngine } from "@adeu/core";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

describe("QA 2026-07-23 — MCP server repro tests (real server over stdio)", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let allTools: any[] = [];

  // Per-finding fixture paths (each test owns its files).
  let f11TrackedPath: string; // one tracked change ("lazy dog" -> "sleepy cat")
  let f14OrigPath: string; // plain doc
  let f14CopyPath: string; // byte-identical copy of f14OrigPath
  let f17aInputPath: string; // plain doc, default-named output run twice
  let f17bInputPath: string; // plain doc, output_path == input path
  let f18PlainPath: string; // plain doc with ZERO tracked changes
  let f19FakePath: string; // .docx-named file containing plain text bytes

  const getTool = (name: string) => allTools.find((t) => t.name === name);

  // --- Line-buffered JSON-RPC plumbing over stdio (as in mcp.schema-gaps) ---
  const pending = new Map<number, (msg: any) => void>();
  let rpcId = 700;
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

  // Build a docx from paragraph strings by cloning the empty shared fixture
  // and clearing its body (same technique as mcp.schema-gaps.test.ts).
  async function buildDoc(paragraphs: string[]): Promise<Buffer> {
    const initialPath = resolve(
      __dirname,
      "../../../../shared/fixtures/initial.docx",
    );
    const doc = await DocumentObject.load(readFileSync(initialPath));
    const body = doc.element;
    while (body.firstChild) body.removeChild(body.firstChild);
    const xmlDoc = body.ownerDocument!;
    for (const text of paragraphs) {
      const p = xmlDoc.createElement("w:p");
      const r = xmlDoc.createElement("w:r");
      const t = xmlDoc.createElement("w:t");
      t.textContent = text;
      t.setAttribute("xml:space", "preserve");
      r.appendChild(t);
      p.appendChild(r);
      body.appendChild(p);
    }
    return doc.save();
  }

  const PLAIN_PARAGRAPHS = [
    "The quick brown fox jumps over the lazy dog.",
    "Second clause stays.",
  ];

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_qa20260723_"));

    // F11: a document with exactly one tracked change by "Reviewer".
    const plainBuf = await buildDoc(PLAIN_PARAGRAPHS);
    {
      const doc = await DocumentObject.load(plainBuf);
      const engine = new RedlineEngine(doc, "Reviewer");
      engine.process_batch([
        { type: "modify", target_text: "lazy dog", new_text: "sleepy cat" },
      ]);
      f11TrackedPath = join(workDir, "f11_tracked.docx");
      writeFileSync(f11TrackedPath, await doc.save());
    }

    // F14: two byte-identical documents.
    f14OrigPath = join(workDir, "f14_orig.docx");
    f14CopyPath = join(workDir, "f14_copy.docx");
    writeFileSync(f14OrigPath, plainBuf);
    copyFileSync(f14OrigPath, f14CopyPath);

    // F17a / F17b: independent plain inputs.
    f17aInputPath = join(workDir, "f17a_input.docx");
    writeFileSync(f17aInputPath, plainBuf);
    f17bInputPath = join(workDir, "f17b_input.docx");
    writeFileSync(f17bInputPath, plainBuf);

    // F18: plain document with zero tracked changes.
    f18PlainPath = join(workDir, "f18_plain.docx");
    writeFileSync(f18PlainPath, plainBuf);

    // F19: a .docx-named file whose bytes are plain text (not a zip).
    f19FakePath = join(workDir, "f19_fake.docx");
    writeFileSync(
      f19FakePath,
      "This is definitely not a zip archive, just plain text bytes.\n",
    );

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

    await rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "qa-2026-07-23-repro", version: "0.0.0" },
    });
    notify("notifications/initialized", {});

    const list = await rpc("tools/list", {});
    allTools = list.result.tools ?? [];
  }, 30000);

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  // ======================================================================
  // F10 — row-op fields must be documented in the tool DESCRIPTION
  // ======================================================================
  it("F10: process_document_batch description documents the row-op fields (insert_row/delete_row + cells)", () => {
    const pdbTool = getTool("process_document_batch");
    expect(pdbTool, "process_document_batch must be advertised").toBeDefined();
    const description: string = pdbTool.description ?? "";

    // The op names themselves are already mentioned…
    expect(description).toContain("insert_row");
    expect(description).toContain("delete_row");
    // …but the working interface (`cells`: array of cell values; the row is
    // located via `target_text`) is discoverable only from the typed
    // changes.items schema, which MCP clients demonstrably strip to `{}` in
    // transit. The description must name the `cells` field so the row ops
    // stay usable through any client. FAILS on current main: the prose says
    // only "'insert_row' / 'delete_row': Table edits. Disk mode only".
    expect(
      description,
      "tool description must document the insert_row `cells` field",
    ).toContain("cells");
  });

  // ======================================================================
  // F11 — MCP error strings must not recommend CLI commands
  // ======================================================================
  it("F11: stale-ID error advises calling read_docx again, not running CLI commands", async () => {
    const res = await rpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        reasoning: "test",
        original_docx_path: f11TrackedPath,
        author_name: "QA Agent",
        changes: [{ type: "accept", target_id: "Chg:99" }],
      },
    });

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    // Sanity: we hit the not-found path for a stale/nonexistent change id.
    expect(text).toContain("no tracked change with that id exists");

    // FAILS on current main: the recovery hint reads
    // "Run `adeu markup <file> -i` or `adeu extract <file>` to list the
    // current change (Chg:) and comment (Com:) ids." — an MCP caller cannot
    // run the CLI; the correct advice is to call read_docx again.
    expect(text).not.toContain("adeu markup");
    expect(text).not.toContain("adeu extract");
    expect(text).toMatch(/read_docx/);
  }, 20000);

  // ======================================================================
  // F14 — identical files must yield an explicit no-differences statement
  // ======================================================================
  it("F14: diff_docx_files on identical files states there are no differences", async () => {
    const res = await rpc("tools/call", {
      name: "diff_docx_files",
      arguments: {
        reasoning: "test",
        original_path: f14OrigPath,
        modified_path: f14CopyPath,
        compare_clean: true,
      },
    });

    expect(res.result.isError).toBeFalsy();
    const text: string = res.result.content[0].text;
    // Sanity: the files really are identical — no Word Patch hunks.
    expect(text).not.toContain("@@ Word Patch @@");

    // FAILS on current main: the response is ONLY the two header lines
    // ("--- f14_orig.docx\n+++ f14_copy.docx\n"), leaving the caller to
    // infer that nothing differs.
    expect(text).toMatch(/no (textual )?differences/i);
  }, 20000);

  // ======================================================================
  // F16 — relative-path error must echo the path as given + require absolutes
  // ======================================================================
  it("F16: relative-path file-not-found error echoes the full path as given", async () => {
    const res = await rpc("tools/call", {
      name: "read_docx",
      arguments: {
        reasoning: "test",
        file_path: "qa_sandbox/alice_copy.docx",
      },
    });

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    // FAILS on current main: the error says "file not found: alice_copy.docx;"
    // — the qa_sandbox/ directory component is dropped, so the caller cannot
    // see which path was actually tried.
    expect(text).toContain("qa_sandbox/alice_copy.docx");
  }, 20000);

  it("F16: relative-path file-not-found error says absolute paths are required", async () => {
    const res = await rpc("tools/call", {
      name: "read_docx",
      arguments: {
        reasoning: "test",
        file_path: "qa_sandbox/alice_copy.docx",
      },
    });

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    // FAILS on current main: nothing tells the caller that file_path must be
    // an absolute path (the schema says so, but the error must too — the
    // caller who got here clearly missed it).
    expect(text).toMatch(/absolute path/i);
  }, 20000);

  // ======================================================================
  // F17 — overwrites must be called out in the batch response
  // ======================================================================
  it("F17a: rerunning a default-named batch warns that the existing output was overwritten", async () => {
    const callBatch = () =>
      rpc("tools/call", {
        name: "process_document_batch",
        arguments: {
          reasoning: "test",
          original_docx_path: f17aInputPath,
          author_name: "QA Agent",
          changes: [
            {
              type: "modify",
              target_text: "Second clause",
              new_text: "Third clause",
            },
          ],
        },
      });

    // First run creates f17a_input_processed.docx — no warning expected.
    const first = await callBatch();
    expect(first.result.isError).toBeFalsy();
    expect(first.result.content[0].text).toContain("Batch complete.");
    expect(existsSync(join(workDir, "f17a_input_processed.docx"))).toBe(true);

    // Second run silently clobbers it. FAILS on current main: the response is
    // an identical "Batch complete. Saved to: …" with no overwrite mention.
    const second = await callBatch();
    expect(second.result.isError).toBeFalsy();
    expect(second.result.content[0].text).toMatch(/overwrit|replaced existing/i);
  }, 20000);

  it("F17b: output_path equal to the input path warns that the source was overwritten in place", async () => {
    const res = await rpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        reasoning: "test",
        original_docx_path: f17bInputPath,
        author_name: "QA Agent",
        changes: [
          {
            type: "modify",
            target_text: "Second clause",
            new_text: "Third clause",
          },
        ],
        output_path: f17bInputPath,
      },
    });

    expect(res.result.isError).toBeFalsy();
    const text: string = res.result.content[0].text;
    expect(text).toContain("Batch complete.");
    // FAILS on current main: the source document is silently overwritten in
    // place with no acknowledgement anywhere in the response.
    expect(text).toMatch(/overwrit|replaced existing/i);
  }, 20000);

  // ======================================================================
  // F18 — accept_all_changes must report a no-op as a no-op
  // ======================================================================
  it("F18: accept_all_changes on a change-free document says there were no changes", async () => {
    // Sanity: the fixture really has zero tracked changes and zero comments.
    const read = await rpc("tools/call", {
      name: "read_docx",
      arguments: { reasoning: "test", file_path: f18PlainPath },
    });
    expect(read.result.isError).toBeFalsy();
    const docText: string = read.result.content[0].text;
    expect(docText).not.toContain("{++");
    expect(docText).not.toContain("{--");

    const res = await rpc("tools/call", {
      name: "accept_all_changes",
      arguments: {
        reasoning: "test",
        docx_path: f18PlainPath,
        output_path: join(workDir, "f18_plain_clean.docx"),
      },
    });

    expect(res.result.isError).toBeFalsy();
    const text: string = res.result.content[0].text;
    // FAILS on current main: the response is unconditionally
    // "Accepted all changes. Saved to: …" even though nothing was accepted.
    expect(text).toMatch(/no (pending |tracked )?changes/i);
  }, 20000);

  // ======================================================================
  // F19 — non-zip bytes must be diagnosed as "not a valid .docx"
  // ======================================================================
  it("F19: reading a .docx whose bytes are plain text hints the file is not a valid .docx", async () => {
    const res = await rpc("tools/call", {
      name: "read_docx",
      arguments: { reasoning: "test", file_path: f19FakePath },
    });

    expect(res.result.isError).toBe(true);
    const text: string = res.result.content[0].text;
    // FAILS on current main: the raw fflate message leaks through as
    // "Error executing tool read_docx: invalid zip data", which teaches the
    // caller nothing about the actual problem (the file is not a Word
    // document at all).
    expect(text).toMatch(/not a valid \.?docx|not a word document/i);
  }, 20000);
});
