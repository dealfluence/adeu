import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";

describe("QA Regression Test - Finding 1: Platform Serialization Bug Rejects Raw JSON Changes", () => {
  let serverProc: ChildProcess;
  let docPath: string;
  let outputDocPath: string;

  beforeAll(async () => {
    // 1. Grab the shared golden fixture from the monorepo root
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    docPath = join(tmpdir(), `adeu_platform_ser_doc_${Date.now()}.docx`);
    outputDocPath = join(tmpdir(), `adeu_platform_ser_out_${Date.now()}.docx`);

    const fixtureBuf = readFileSync(fixturePath);
    writeFileSync(docPath, fixtureBuf);

    // 2. Boot the compiled MCP server
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before running tests.",
      );
    }

    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (existsSync(docPath)) unlinkSync(docPath);
    if (existsSync(outputDocPath)) unlinkSync(outputDocPath);
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

  it("should handle numeric reference IDs in changes gracefully and return a clear workaround instruction instead of throwing an RPC schema validation error", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "process_document_batch",
        arguments: {
          reasoning: "Test numeric reference ID serialization",
          original_docx_path: docPath,
          output_path: outputDocPath,
          author_name: "QA Tester",
          changes: [
            1928014526 // A serialized object converted to a numeric ID by the platform MCP client
          ],
        },
      },
      401,
    );

    // The RPC protocol call should succeed (no Zod schema parsing error at the RPC layer)
    expect(res.error).toBeUndefined();
    expect(res.result).toBeDefined();

    // The tool should return a domain error
    expect(res.result.isError).toBe(true);

    const errorText = res.result.content[0].text;
    
    // It should explain the numeric reference ID platform constraint and suggest the stringified workaround
    expect(errorText).toContain("numeric reference ID");
    expect(errorText).toContain("platform serialization constraint");
    expect(errorText).toContain("JSON-stringified string");
  });
});
