import { afterEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { zipSync } from "fflate";
import { checkToolPath, writeToolDocument } from "./path-policy.js";
import { startTestServer } from "./test-rpc.js";

const shared = fileURLToPath(new URL("../../../../shared/", import.meta.url));
const word = fs.readFileSync(join(shared, "fixtures/base.docx"));
interface PolicyCase {
  id: string;
  operation: "write" | "open";
  inputs: string[];
  target: string;
  kind: "absent" | "directory" | "empty" | "text" | "other_zip" | "word";
  allow: boolean;
  symlink?: string;
  windows_only?: boolean;
  pdf?: boolean;
}
const cases: PolicyCase[] = JSON.parse(
  fs.readFileSync(join(shared, "path_policy/cases.json"), "utf8"),
);

describe("tool path policy", () => {
  let root: string;
  function setup() {
    root = fs.realpathSync.native(fs.mkdtempSync(join(tmpdir(), "adeu-path-policy-")));
    return root;
  }
  afterEach(() => {
    if (root) fs.rmSync(root, { recursive: true, force: true });
  });

  for (const test of cases) {
    it.skipIf(test.windows_only && process.platform !== "win32")(
      test.id,
      async (ctx) => {
        setup();
        const inputs = test.inputs.map((name) => join(root, name));
        for (const input of inputs) {
          fs.mkdirSync(dirname(input), { recursive: true });
          fs.writeFileSync(input, word);
        }
        const target = join(root, test.target);
        const actual = join(root, test.symlink ?? test.target);
        if (test.kind !== "absent") {
          fs.mkdirSync(dirname(actual), { recursive: true });
          switch (test.kind) {
            case "directory":
              fs.mkdirSync(actual);
              break;
            case "text":
              fs.writeFileSync(actual, "Existing contents");
              break;
            case "empty":
              fs.writeFileSync(actual, "");
              break;
            case "word":
              fs.writeFileSync(actual, word);
              break;
            case "other_zip":
              fs.writeFileSync(
                actual,
                zipSync({
                  "[Content_Types].xml": new Uint8Array(),
                  "xl/workbook.xml": new Uint8Array(),
                }),
              );
              break;
            default:
              throw new Error(`Unknown fixture ${test.kind}`);
          }
        }
        if (test.symlink) {
          fs.mkdirSync(dirname(target), { recursive: true });
          try {
            fs.symlinkSync(actual, target, "file");
          } catch (error) {
            if (
              process.platform === "win32" &&
              (error as NodeJS.ErrnoException).code === "EPERM"
            ) {
              ctx.skip("Windows file symlink privilege is unavailable");
            }
            throw error;
          }
        }
        const before = fs.readdirSync(root, { recursive: true });
        const contents =
          test.kind !== "directory" && test.kind !== "absent"
            ? fs.readFileSync(actual)
            : undefined;
        const check = checkToolPath(
          target,
          test.pdf ? "pdf" : test.operation,
          inputs,
        );
        if (test.allow) {
          expect(await check).toBe(actual);
        } else {
          await expect(check).rejects.toThrow(
            /Refused .*\. (Use|Choose|Supply) /,
          );
          if (test.operation === "write" && !test.pdf) {
            await expect(
              writeToolDocument(target, word, inputs),
            ).rejects.toThrow("Refused");
          }
        }
        expect(fs.readdirSync(root, { recursive: true })).toEqual(before);
        if (contents) expect(fs.readFileSync(actual)).toEqual(contents);
      },
    );
  }

  it("writes through the deepest existing parent of a linked folder", async () => {
    setup();
    const source = join(root, "input.docx");
    fs.writeFileSync(source, word);
    const folder = join(root, "real");
    const link = join(root, "alias");
    fs.mkdirSync(folder);
    fs.symlinkSync(
      folder,
      link,
      process.platform === "win32" ? "junction" : "dir",
    );
    const target = join(folder, "child", "final.docx");
    expect(
      (
        await writeToolDocument(join(link, "child", "final.docx"), word, [
          source,
        ])
      ).path,
    ).toBe(target);
    expect(fs.readFileSync(target)).toEqual(word);
  });

  it("replacing an interrupted sibling preserves hard-linked contents", async () => {
    setup();
    const source = join(root, "input.docx");
    const notes = join(root, "notes.txt");
    const target = join(root, "leftover.docx");
    fs.writeFileSync(source, word);
    fs.writeFileSync(notes, "Existing contents");
    fs.linkSync(notes, target);
    await writeToolDocument(target, word, [source]);
    expect(fs.readFileSync(notes, "utf8")).toBe("Existing contents");
    expect(fs.readFileSync(target)).toEqual(word);
  });

  it("routes every registered path parameter through the helper", async () => {
    setup();
    const server = await startTestServer("path-coverage");
    try {
      const source = join(root, "source.docx");
      const output = join(root, "dest.docx");
      fs.writeFileSync(source, word);
      const { result } = await server.rpc("tools/list", {});
      for (const tool of result.tools) {
        const args: Record<string, unknown> = {
          changes: [],
          revised_text: "Text",
        };
        const keys = Object.keys(tool.inputSchema.properties).filter((key) =>
          /(?:^|_)(?:paths?|files?|filenames?|director(?:y|ies)|folders?)(?:_|$)/i.test(
            key.replace(/([a-z])([A-Z])/g, "$1_$2"),
          ),
        );
        for (const key of keys)
          args[key] = key === "output_path" ? output : source;
        for (const key of keys) {
          const result = await server.callTool(tool.name, {
            ...args,
            [key]: "invalid\0filename",
          });
          expect(result.isError, `${tool.name}.${key}`).toBe(true);
          expect(result.content[0].text, `${tool.name}.${key}`).toContain(
            "invalid file path",
          );
        }
      }
    } finally {
      server.stop();
    }
  });

  it("refuses an unsafe diagnostic before saving a text revision", async () => {
    setup();
    const server = await startTestServer("diagnostic-path");
    try {
      const source = join(root, "source.docx");
      const folder = join(root, "results");
      const output = join(folder, "final.docx");
      const diagnostic = join(folder, "final.unverified.docx");
      fs.writeFileSync(source, word);
      fs.mkdirSync(folder);
      fs.writeFileSync(diagnostic, "Existing contents");
      const result = await server.callTool("apply_text_revision", {
        file_path: source,
        output_path: output,
        revised_text: "Text",
      });
      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain(diagnostic);
      expect(fs.readFileSync(diagnostic, "utf8")).toBe("Existing contents");
      expect(fs.existsSync(output)).toBe(false);
    } finally {
      server.stop();
    }
  });
});
