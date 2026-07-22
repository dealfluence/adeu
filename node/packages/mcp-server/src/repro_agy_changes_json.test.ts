import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";

// There is exactly ONE changes parameter. A second `changes_json` spelling
// forced the model to choose between two ways to say the same thing, and the
// two engines had drifted into opposite rules for which one wins. `changes`
// stays a REQUIRED, typed array: per-item stringification is repaired, and a
// wholly stringified payload is rejected with an error the caller can act on
// rather than being silently treated as an empty batch.
describe("QA Regression Test - process_document_batch changes payload shapes", () => {
  let serverProc: ChildProcess;
  let docPath: string;
  let outputDocPath: string;

  beforeAll(async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    docPath = join(tmpdir(), `adeu_scratch_doc_${Date.now()}.docx`);
    outputDocPath = join(tmpdir(), `adeu_scratch_output_${Date.now()}.docx`);

    const fixtureBuf = readFileSync(fixturePath);
    writeFileSync(docPath, fixtureBuf);

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

  it("repairs per-item stringified changes (the Gemini double-serialize quirk)", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "process_document_batch",
        arguments: {
          reasoning: "Test per-item stringified changes",
          original_docx_path: docPath,
          output_path: outputDocPath,
          author_name: "QA Tester",
          changes: [
            JSON.stringify({
              type: "modify",
              target_text: "document",
              new_text: "modified tracked document",
            }),
          ],
        },
      },
      301,
    );

    expect(res.error).toBeUndefined();
    expect(res.result).toBeDefined();
    expect(res.result.isError).toBeUndefined();
    expect(res.result.content[0].text).toContain("applied");
  });

  it("rejects a wholly stringified payload loudly instead of as an empty batch", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "process_document_batch",
        arguments: {
          reasoning: "Test wholly stringified payload",
          original_docx_path: docPath,
          output_path: outputDocPath,
          author_name: "QA Tester",
          changes: JSON.stringify([
            { type: "modify", target_text: "a", new_text: "b" },
          ]),
        },
      },
      302,
    );

    // Must NOT report success over an unchanged document.
    expect(res.result.isError).toBe(true);
    expect(res.result.content[0].text).toContain("expected array");
    expect(res.result.content[0].text).toContain("changes");
  });

  it("advertises changes as a required, typed array", async () => {
    const res = await sendRpc("tools/list", {}, 303);
    const tool = res.result.tools.find(
      (t: any) => t.name === "process_document_batch",
    );
    expect(tool.inputSchema.required).toContain("changes");
    expect(tool.inputSchema.properties.changes.type).toBe("array");
    expect(tool.inputSchema.properties.changes.items).toBeTruthy();
    expect(Object.keys(tool.inputSchema.properties)).not.toContain(
      "changes_json",
    );
  });
});
