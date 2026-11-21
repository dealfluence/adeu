import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, realpathSync, rmdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { extractTextFromBuffer } from "@adeu/core";
import { expect, it } from "vitest";
import { startTestServer } from "./test-rpc.js";

it.skipIf(process.platform !== "win32")(
  "reports the canonical output path when a batch writes through a Windows short directory name",
  async (ctx) => {
    const directory = realpathSync.native(
      mkdtempSync(join(tmpdir(), "adeu-long-directory-name-")),
    );
    const output = join(directory, "result.docx");
    ctx.onTestFinished(() => {
      rmSync(output, { force: true });
      rmdirSync(directory);
    });

    const shortDirectory = execFileSync(
      "cmd.exe",
      ["/d", "/u", "/c", "for %I in (.) do @echo %~sI"],
      { cwd: directory, encoding: "utf16le" },
    ).trim();
    if (basename(shortDirectory).toLowerCase() === basename(directory).toLowerCase()) {
      ctx.skip("The temporary directory has no Windows short name");
    }
    expect(realpathSync.native(shortDirectory)).toBe(directory);

    const server = await startTestServer("windows-short-path");
    ctx.onTestFinished(() => server.stop());
    const input = await server.buildDoc(["Payment is due within thirty days."]);
    const result = await server.callTool("process_document_batch", {
      reasoning: "Verify the saved path",
      original_docx_path: input,
      author_name: "Reviewer",
      changes: [{ type: "modify", target_text: "thirty", new_text: "sixty" }],
      output_path: join(shortDirectory, "result.docx"),
    });

    expect(result.isError).toBeFalsy();
    expect(result.content[0].text).toContain(`Saved to: ${output}\n`);
    expect(await extractTextFromBuffer(readFileSync(output))).toContain("{++sixty++}");
  },
);
