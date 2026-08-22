import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { DocumentObject, RedlineEngine } from "@adeu/core";
import { startTestServer, type TestServer } from "./test-rpc.js";

describe("QA Regression Test - Finding 1: finalize_document crash on missing sanitize_mode with tracked changes", () => {
  let server: TestServer;
  let trackedDocPath: string;
  let outputDocPath: string;

  beforeAll(async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    trackedDocPath = join(tmpdir(), `adeu_regression_tracked_${Date.now()}.docx`);
    outputDocPath = join(tmpdir(), `adeu_regression_output_${Date.now()}.docx`);

    const fixtureBuf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(fixtureBuf);
    const engine = new RedlineEngine(doc, "Reviewer");

    engine.process_batch([
      {
        type: "modify",
        target_text: "document",
        new_text: "modified tracked document",
      },
    ]);
    writeFileSync(trackedDocPath, await doc.save());

    server = await startTestServer("agy-bug");
  });

  afterAll(() => {
    server?.stop();
    if (existsSync(trackedDocPath)) unlinkSync(trackedDocPath);
    if (existsSync(outputDocPath)) unlinkSync(outputDocPath);
  });

  it("should return a clean block report instead of crashing when sanitize_mode is omitted with tracked changes", async () => {
    const res = await server.rpc(
      "tools/call",
      {
        name: "finalize_document",
        arguments: {
          file_path: trackedDocPath,
          output_path: outputDocPath,
          reasoning: "Test finalizing with tracked changes but no sanitize_mode",
        },
      },
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
