// FILE: node/packages/mcp-server/src/mcp.bugs.test.ts
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { DocumentObject, RedlineEngine } from "@adeu/core";

describe("Resolved Bugs MCP Server Verification", () => {
  let serverProc: ChildProcess;
  let cleanDocPath: string;
  let dirtyDocPath: string;

  beforeAll(async () => {
    // 1. Grab the shared golden fixture from the monorepo root
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    cleanDocPath = join(tmpdir(), `adeu_clean_${Date.now()}.docx`);
    dirtyDocPath = join(tmpdir(), `adeu_dirty_${Date.now()}.docx`);

    // Save a clean copy
    const fixtureBuf = readFileSync(fixturePath);
    writeFileSync(cleanDocPath, fixtureBuf);

    // Load it via the public API, dirty it, and save a dirty copy
    const doc = await DocumentObject.load(fixtureBuf);
    const engine = new RedlineEngine(doc, "Reviewer");

    // "document" is original base text, so we won't trigger the cross-author nested redline constraint
    engine.process_batch([
      {
        type: "modify",
        target_text: "document",
        new_text: "dirty modified document",
      },
    ]);
    writeFileSync(dirtyDocPath, await doc.save());

    // 2. Boot the compiled MCP server
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before tests.",
      );
    }

    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (existsSync(cleanDocPath)) unlinkSync(cleanDocPath);
    if (existsSync(dirtyDocPath)) unlinkSync(dirtyDocPath);
  });

  // Helper to interact with the stdio JSON-RPC server
  function sendRpc(method: string, params: any, id: number = 1): Promise<any> {
    return new Promise((resolve, reject) => {
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
              resolve(res);
            }
          } catch (e) {
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

  it("BUG-5: Rejects empty changes array early without writing files", async () => {
    const outPath = join(tmpdir(), `adeu_out_${Date.now()}.docx`);
    if (existsSync(outPath)) unlinkSync(outPath);

    const res = await sendRpc(
      "tools/call",
      {
        name: "process_document_batch",
        arguments: {
          original_docx_path: cleanDocPath,
          author_name: "Agent",
          changes: [],
          output_path: outPath,
        },
      },
      101,
    );

    expect(res.result.content[0].text).toBe("Error: No changes provided.");
    expect(existsSync(outPath)).toBe(false); // Proves no-op
  });

  it("BUG-9: diff_docx_files tool respects compare_clean parameter", async () => {
    // 1. compare_clean = true (Default) -> Should output clean text comparison
    const resClean = await sendRpc(
      "tools/call",
      {
        name: "diff_docx_files",
        arguments: {
          original_path: cleanDocPath,
          modified_path: dirtyDocPath,
          compare_clean: true,
        },
      },
      102,
    );

    const cleanText = resClean.result.content[0].text;
    expect(cleanText).toContain("dirty modified");
    expect(cleanText).not.toContain("{++"); // No CriticMarkup

    // 2. compare_clean = false -> Should output raw CriticMarkup comparison
    const resRaw = await sendRpc(
      "tools/call",
      {
        name: "diff_docx_files",
        arguments: {
          original_path: cleanDocPath,
          modified_path: dirtyDocPath,
          compare_clean: false,
        },
      },
      103,
    );

    const rawText = resRaw.result.content[0].text;
    expect(rawText).toContain("{++dirty modified ++}");
  });
  it("BUG-10: Traps ENOENT and returns clean File Not Found errors", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: { file_path: join(tmpdir(), "DEF_DOES_NOT_EXIST.docx") },
      },
      104,
    );

    expect(res.result.isError).toBe(true);
    expect(res.result.content[0].text).toContain(
      "Error executing tool read_docx: File not found:",
    );
    expect(res.result.content[0].text).not.toContain("ENOENT"); // Raw node error must not leak
  });
});
