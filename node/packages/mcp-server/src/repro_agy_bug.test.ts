import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { DocumentObject, RedlineEngine } from "@adeu/core";

describe("QA Regression Test - Finding 1: finalize_document crash on missing sanitize_mode with tracked changes", () => {
  let serverProc: ChildProcess;
  let trackedDocPath: string;
  let outputDocPath: string;

  beforeAll(async () => {
    // 1. Grab the shared golden fixture from the monorepo root
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    trackedDocPath = join(tmpdir(), `adeu_regression_tracked_${Date.now()}.docx`);
    outputDocPath = join(tmpdir(), `adeu_regression_output_${Date.now()}.docx`);

    // Load fixture and dirty it to create unresolved tracked changes
    const fixtureBuf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(fixtureBuf);
    const engine = new RedlineEngine(doc, "Reviewer");

    // Modify a piece of text to generate a tracked change
    engine.process_batch([
      {
        type: "modify",
        target_text: "document",
        new_text: "modified tracked document",
      },
    ]);
    writeFileSync(trackedDocPath, await doc.save());

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
    if (existsSync(trackedDocPath)) unlinkSync(trackedDocPath);
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

  it("should return a clean block report instead of crashing when sanitize_mode is omitted with tracked changes", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "finalize_document",
        arguments: {
          reasoning: "Finalize document containing unresolved tracked changes without sanitize_mode",
          file_path: trackedDocPath,
          output_path: outputDocPath,
        },
      },
      201,
    );

    // Assert that the tool does not crash or return a TypeError
    expect(res.error).toBeUndefined();
    expect(res.result).toBeDefined();
    
    // It should NOT be an error/crash response.
    // (A blocked finalization is a clean business logic result, not a fatal RPC / NodeJS error)
    expect(res.result.isError).toBeUndefined();
    
    const responseText = res.result.content[0].text;
    expect(responseText).not.toContain("TypeError");
    expect(responseText).not.toContain("must be of type string");
    
    // It must contain the blocked report indicating unresolved tracked changes
    expect(responseText.toLowerCase()).toContain("blocked");
    expect(responseText).toContain("unresolved tracked changes");
    
    // Proves that no file was written
    expect(existsSync(outputDocPath)).toBe(false);
  });
});
