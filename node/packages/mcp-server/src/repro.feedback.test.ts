// FILE: node/packages/mcp-server/src/repro.feedback.test.ts
//
// Reproduction for field observation Issue 3 — "File-Path Ambiguity and Save
// Shuffling". Exercised end-to-end against the REAL compiled MCP server over
// stdio JSON-RPC (mirrors mcp.bugs.test.ts).
//
//   * process_document_batch defaults its output to ${base}_processed.docx
//   * accept_all_changes      defaults its output to ${base}_clean.docx
//
// Because the two tools choose different default stems, the "active" document
// state fragments across multiple files and the agent loses track of which file
// holds the current state. The Node server ALSO lacks the idempotency guard the
// Python tool has, so re-running a batch compounds the suffix
// (contract_processed_processed.docx).

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join, basename, dirname } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  readdirSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { DocumentObject } from "@adeu/core";

describe("Field feedback repro — Issue 3 file-path ambiguity (real MCP server)", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let contractPath: string;

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_repro_paths_"));
    contractPath = join(workDir, "contract.docx");

    // Build a tiny contract with known text from the shared fixture.
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/initial.docx",
    );
    const doc = await DocumentObject.load(readFileSync(fixturePath));
    const body = doc.element;
    while (body.firstChild) body.removeChild(body.firstChild);
    const x = body.ownerDocument!;
    const p = x.createElement("w:p");
    const r = x.createElement("w:r");
    const t = x.createElement("w:t");
    t.textContent = "The Provider shall deliver the goods.";
    t.setAttribute("xml:space", "preserve");
    r.appendChild(t);
    p.appendChild(r);
    body.appendChild(p);
    writeFileSync(contractPath, await doc.save());

    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error("MCP server not built. Run 'npm run build' before tests.");
    }
    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (workDir && existsSync(workDir)) rmSync(workDir, { recursive: true, force: true });
  });

  function sendRpc(method: string, params: any, id: number): Promise<any> {
    return new Promise((res, rej) => {
      const timeout = setTimeout(() => rej(new Error("RPC Timeout")), 8000);
      const listener = (data: Buffer) => {
        const lines = data.toString().trim().split("\n");
        for (const line of lines) {
          if (!line.startsWith("{")) continue;
          try {
            const parsed = JSON.parse(line);
            if (parsed.id === id) {
              clearTimeout(timeout);
              serverProc.stdout?.removeListener("data", listener);
              res(parsed);
            }
          } catch {
            /* ignore partial chunks */
          }
        }
      };
      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  const callTool = (name: string, args: any, id: number) =>
    sendRpc("tools/call", { name, arguments: args }, id);

  /** Pull just the file path out of a "...Saved to: <path>\n<rest>" message. */
  const savedFile = (msg: string) =>
    basename(msg.split("Saved to:")[1].split("\n")[0].trim());

  it("redline + accept defaults land in DIFFERENT files (_processed vs _clean), fragmenting state", async () => {
    // 1. Redline with NO output_path -> contract_processed.docx
    const r1 = await callTool(
      "process_document_batch",
      {
        original_docx_path: contractPath,
        author_name: "Agent",
        changes: [{ type: "modify", target_text: "Provider", new_text: "Supplier" }],
      },
      201,
    );
    const msg1 = r1.result.content[0].text as string;
    expect(msg1).toContain("Batch complete. Saved to:");
    expect(savedFile(msg1)).toBe("contract_processed.docx");
    expect(existsSync(join(workDir, "contract_processed.docx"))).toBe(true);

    // 2. Accept all on the redlined file with NO output_path -> *_clean.docx,
    //    NOT back onto contract.docx / contract_processed.docx.
    const r2 = await callTool(
      "accept_all_changes",
      { docx_path: join(workDir, "contract_processed.docx") },
      202,
    );
    const msg2 = r2.result.content[0].text as string;
    expect(msg2).toContain("Accepted all changes. Saved to:");
    expect(savedFile(msg2)).toBe("contract_processed_clean.docx");

    // The agent now has THREE files and no single source of truth:
    const files = readdirSync(workDir).sort();
    expect(files).toEqual([
      "contract.docx",
      "contract_processed.docx",
      "contract_processed_clean.docx",
    ]);
  });

  it("Issue 3 (BUG, RED until fixed): re-batching a *_processed.docx must reuse the path, not compound the suffix", async () => {
    // DESIRED behaviour (already implemented in the Python tool, document.py:220):
    // when the input stem already ends in _processed/_redlined, write back to the
    // SAME file. The Node tool has no such guard, so it splinters state into
    // contract_processed_processed.docx. This test is RED on current Node and
    // turns GREEN once the idempotency guard is ported.
    const r = await callTool(
      "process_document_batch",
      {
        original_docx_path: join(workDir, "contract_processed.docx"),
        author_name: "Agent",
        changes: [{ type: "modify", target_text: "goods", new_text: "products" }],
      },
      203,
    );
    const msg = r.result.content[0].text as string;
    expect(savedFile(msg)).toBe("contract_processed.docx");
    expect(existsSync(join(workDir, "contract_processed_processed.docx"))).toBe(false);
  });

  it("Issue 1 (end-to-end via shipped server): accept + modify the same foreign insertion in one batch succeeds", async () => {
    // Proves the engine fix is present in the COMPILED bundle, not just src.
    // Build a doc where "Supplier's Counsel" has tracked-inserted "24 months".
    const foreign = join(workDir, "lease.docx");
    {
      const doc = await DocumentObject.load(
        readFileSync(resolve(__dirname, "../../../../shared/fixtures/initial.docx")),
      );
      const body = doc.element;
      while (body.firstChild) body.removeChild(body.firstChild);
      const x = body.ownerDocument!;
      const p = x.createElement("w:p");
      const r0 = x.createElement("w:r");
      const t0 = x.createElement("w:t");
      t0.textContent = "The term is ";
      t0.setAttribute("xml:space", "preserve");
      r0.appendChild(t0);
      p.appendChild(r0);
      const ins = x.createElement("w:ins");
      ins.setAttribute("w:id", "5");
      ins.setAttribute("w:author", "Supplier's Counsel");
      ins.setAttribute("w:date", "2024-01-01T00:00:00Z");
      const r = x.createElement("w:r");
      const t = x.createElement("w:t");
      t.textContent = "24 months";
      r.appendChild(t);
      ins.appendChild(r);
      p.appendChild(ins);
      body.appendChild(p);
      writeFileSync(foreign, await doc.save());
    }

    const res = await callTool(
      "process_document_batch",
      {
        original_docx_path: foreign,
        author_name: "Acme's Counsel",
        output_path: join(workDir, "lease_out.docx"),
        changes: [
          { type: "accept", target_id: "Chg:5" },
          { type: "modify", target_text: "24 months", new_text: "36 months" },
        ],
      },
      204,
    );
    const msg = res.result.content[0].text as string;
    expect(msg).not.toContain("active insertion from another author");
    expect(msg).toContain("Batch complete");
    expect(msg).toContain("Edits: 1 applied, 0 skipped");
  });
});
