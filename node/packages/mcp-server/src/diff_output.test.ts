import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { DocumentObject, extractTextFromBuffer, RedlineEngine } from "@adeu/core";
import { startTestServer, TestServer } from "./test-rpc.js";

describe("diff_docx_files one-step output_path redline generation", () => {
  let srv: TestServer;

  beforeAll(async () => {
    srv = await startTestServer("diff-output");
  });

  afterAll(() => {
    srv.stop();
  });

  it("saves a redlined DOCX when output_path is passed to diff_docx_files", async () => {
    const v1Path = await srv.buildDoc([
      "1. Governing Law. This Agreement is governed by Delaware law.",
      "2. Payment Terms. Invoices are payable within thirty (30) days.",
    ]);
    const v2Path = await srv.buildDoc([
      "1. Governing Law. This Agreement is governed by New York law.",
      "2. Payment Terms. Invoices are payable within sixty (60) days.",
      "3. Confidentiality. Both parties shall keep terms confidential.",
    ]);
    const outPath = srv.tempOut("redlined");

    const diffRes = await srv.callTool("diff_docx_files", {
      original_path: v1Path,
      modified_path: v2Path,
      output_path: outPath,
      author_name: "Deal Team",
    });

    expect(diffRes.isError).toBeFalsy();
    const text = diffRes.content?.[0]?.text ?? "";
    expect(text).toContain("Redline saved to:");
    expect(existsSync(outPath)).toBe(true);

    const outBytes = readFileSync(outPath);
    const modBytes = readFileSync(v2Path);
    const origBytes = readFileSync(v1Path);

    expect(await extractTextFromBuffer(outBytes, true)).toBe(
      await extractTextFromBuffer(modBytes, true),
    );

    const rejDoc = await DocumentObject.load(outBytes);
    const engRej = new RedlineEngine(rejDoc);
    engRej.reject_all_revisions();
    const rejBytes = await rejDoc.save();
    expect(await extractTextFromBuffer(rejBytes, true)).toBe(
      await extractTextFromBuffer(origBytes, true),
    );
  });

  it.each(["original", "modified"])(
    "refuses an output_path that is the %s document and leaves it untouched",
    async (which) => {
      const v1Path = await srv.buildDoc(["Invoices are payable within thirty (30) days."]);
      const v2Path = await srv.buildDoc(["Invoices are payable within sixty (60) days."]);
      const target = which === "original" ? v1Path : v2Path;
      const before = readFileSync(target);

      const res = await srv.callTool("diff_docx_files", {
        original_path: v1Path,
        modified_path: v2Path,
        output_path: target,
      });

      expect(res.isError).toBe(true);
      expect(res.content?.[0]?.text ?? "").toContain(`same file as the ${which} document`);
      expect(readFileSync(target).equals(before)).toBe(true);
    },
  );

  it("discloses that an existing output file was replaced", async () => {
    const v1Path = await srv.buildDoc(["Invoices are payable within thirty (30) days."]);
    const v2Path = await srv.buildDoc(["Invoices are payable within sixty (60) days."]);
    const outPath = srv.tempOut("existing");
    const args = { original_path: v1Path, modified_path: v2Path, output_path: outPath };
    await srv.callTool("diff_docx_files", args);

    const res = await srv.callTool("diff_docx_files", args);
    expect(res.isError).toBeFalsy();
    expect(res.content?.[0]?.text ?? "").toContain(`replaced existing file ${outPath}`);
  });
});
