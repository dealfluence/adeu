import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import { spawnSync } from "node:child_process";
import { delimiter, dirname, join, sep } from "node:path";
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
interface ConfinementCase {
  id: string;
  roots: string[];
  paths: string[];
  existing?: string[];
  link?: [string, string];
  allow: boolean | "case_alias" | "unicode_alias";
}
interface StartupCase {
  id: string;
  entries: string[] | null;
  link?: [string, string];
  resolved?: string[];
  error?: "empty" | "relative" | "missing" | "file";
}
const {
  safe_targets,
  confinement,
  startup,
}: {
  safe_targets: PolicyCase[];
  confinement: ConfinementCase[];
  startup: StartupCase[];
} = JSON.parse(fs.readFileSync(join(shared, "path_policy/cases.json"), "utf8"));

describe("tool path policy", () => {
  let root: string;
  function setup() {
    root = fs.realpathSync.native(
      fs.mkdtempSync(join(tmpdir(), "adeu-path-policy-")),
    );
    return root;
  }
  function linkFolder([alias, target]: [string, string]) {
    const destination = join(root, target);
    fs.mkdirSync(destination, { recursive: true });
    fs.symlinkSync(
      destination,
      join(root, alias),
      process.platform === "win32" ? "junction" : "dir",
    );
  }
  afterEach(() => {
    if (root) fs.rmSync(root, { recursive: true, force: true });
  });

  it.each(startup)("startup: $id", async (test) => {
    setup();
    fs.mkdirSync(join(root, "allowed"));
    fs.mkdirSync(join(root, "second"));
    fs.writeFileSync(join(root, "file.docx"), word);
    if (test.link) linkFolder(test.link);
    const env = { ...process.env };
    const expandEntry = (s: string) =>
      s.startsWith("$root/") ? root + sep + s.slice(6).split("/").join(sep) : s;
    const entries = test.entries?.map(expandEntry);
    if (entries) env.ADEU_ALLOWED_DIRS = entries.join(delimiter);
    else delete env.ADEU_ALLOWED_DIRS;
    const child = spawnSync(
      process.execPath,
      [fileURLToPath(new URL("../dist/index.js", import.meta.url))],
      {
        env,
        input: "",
        encoding: "utf8",
        timeout: 15000,
      },
    );
    if (child.error) throw child.error;
    if (test.error) {
      const entry = entries!.at(-1);
      const messages = {
        empty: "ADEU_ALLOWED_DIRS is set but contains no folders.",
        relative: `ADEU_ALLOWED_DIRS entry '${entry}' is relative. Use an absolute folder path.`,
        missing: `ADEU_ALLOWED_DIRS entry '${entry}' does not exist.`,
        file: `ADEU_ALLOWED_DIRS entry '${entry}' is not a folder.`,
      };
      expect(child.status).toBe(1);
      expect(child.stderr.trim()).toBe(messages[test.error]);
      expect(child.stdout).toBe("");
    } else {
      expect(child.status).toBe(0);
      expect(child.stderr).toContain("running on stdio");
      if (entries) {
        const roots =
          test.resolved?.map(expandEntry) ??
          entries.filter((entry) => entry.trim());
        const paths = roots.join(delimiter);
        expect(child.stderr.split(/\r?\n/)).toContain(
          `ADEU_ALLOWED_DIRS allowed folders: ${paths}`,
        );
        if (test.link) {
          const source = join(roots[0], "inside.docx");
          fs.writeFileSync(source, word);
          const server = await startTestServer("resolved-root", env);
          try {
            const inside = await server.callTool("read_docx", {
              file_path: source,
            });
            expect(inside.isError, inside.content[0].text).toBeFalsy();
            const outside = await server.callTool("read_docx", {
              file_path: join(root, "file.docx"),
            });
            expect(outside.content[0].text).toBe(
              `File access refused. Allowed folders: ${paths}`,
            );
          } finally {
            server.stop();
          }
        }
      } else {
        expect(child.stderr).not.toContain("ADEU_ALLOWED_DIRS");
      }
    }
  });

  it.each(confinement)("folder confinement: $id", async (test) => {
    setup();
    for (const name of ["allowed", "second", "outside", "allowed2", "Café"])
      fs.mkdirSync(join(root, name));
    if (test.link) linkFolder(test.link);
    for (const name of test.existing ?? [])
      fs.writeFileSync(join(root, name), word);
    const folders = test.roots.map((name) => join(root, name));
    const listing = folders.join(delimiter);
    // join() collapses '..' before the filesystem can resolve an earlier link.
    const paths = test.paths.map(
      (name) => root + sep + name.split("/").join(sep),
    );
    const source =
      paths.length === 2 ? paths[0] : join(folders[0], "source.docx");
    const output = paths.at(-1)!;
    fs.writeFileSync(source, word);
    let expected = test.allow;
    if (typeof expected !== "boolean") {
      const variant = fs.statSync(dirname(output), {
        bigint: true,
        throwIfNoEntry: false,
      });
      const folder = fs.statSync(folders[0], { bigint: true });
      expected = variant?.dev === folder.dev && variant.ino === folder.ino;
    }
    const server = await startTestServer("confinement", {
      ...process.env,
      ADEU_ALLOWED_DIRS: listing,
    });
    try {
      const result =
        test.id === "outside_exists" || test.id === "outside_missing"
          ? await server.callTool("read_docx", { file_path: output })
          : await server.callTool("accept_all_changes", {
              docx_path: source,
              output_path: output,
            });
      if (expected) {
        expect(result.isError, result.content[0].text).toBeFalsy();
      } else {
        expect(result.isError).toBe(true);
        expect(result.content[0].text).toBe(
          `File access refused. Allowed folders: ${listing}`,
        );
      }
    } finally {
      server.stop();
    }
  });

  it("checks inferred outputs and the diagnostic before loading a source", async () => {
    setup();
    const allowed = join(root, "allowed");
    fs.mkdirSync(allowed);
    const source = join(allowed, "source.docx");
    fs.writeFileSync(source, "Invalid source; this must not be loaded");
    const server = await startTestServer("derived-confinement", {
      ...process.env,
      ADEU_ALLOWED_DIRS: allowed,
    });
    try {
      for (const [tool, input, suffix] of [
        ["process_document_batch", "original_docx_path", "_processed"],
        ["accept_all_changes", "docx_path", "_clean"],
        ["finalize_document", "file_path", "_final"],
        ["apply_text_revision", "file_path", "_redlined"],
        ["apply_text_revision", "file_path", "_redlined.unverified"],
      ]) {
        const target = join(allowed, `source${suffix}.docx`);
        fs.symlinkSync(
          root,
          target,
          process.platform === "win32" ? "junction" : "dir",
        );
        const result = await server.callTool(tool, {
          [input]: source,
          changes: [],
          revised_text: "text",
        });
        expect(result.content[0].text, `${tool} ${suffix}`).toBe(
          `File access refused. Allowed folders: ${allowed}`,
        );
        fs.unlinkSync(target);
      }
    } finally {
      server.stop();
    }
  });

  it("suggests siblings only in the resolved folder of a missing document", async () => {
    setup();
    const allowed = join(root, "allowed");
    const child = join(allowed, "child");
    fs.mkdirSync(child, { recursive: true });
    fs.writeFileSync(join(allowed, "inside.docx"), word);
    fs.writeFileSync(join(root, "outside.docx"), word);
    fs.symlinkSync(
      root,
      join(allowed, "outside-link.docx"),
      process.platform === "win32" ? "junction" : "dir",
    );
    const alias = join(root, "link");
    fs.symlinkSync(
      child,
      alias,
      process.platform === "win32" ? "junction" : "dir",
    );
    const server = await startTestServer("confined-missing", {
      ...process.env,
      ADEU_ALLOWED_DIRS: allowed,
    });
    try {
      const result = await server.callTool("read_docx", {
        file_path: [alias, "..", "missing.docx"].join(sep),
      });
      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain(
        "available files: [inside.docx]",
      );
      expect(result.content[0].text).not.toContain("outside.docx");
      expect(result.content[0].text).not.toContain("outside-link.docx");
    } finally {
      server.stop();
    }
  });

  it("keeps the startup snapshot after the environment changes", async () => {
    setup();
    const allowed = join(root, "allowed");
    fs.mkdirSync(allowed);
    vi.stubEnv("ADEU_ALLOWED_DIRS", allowed);
    try {
      vi.resetModules();
      const policy = await import("./path-policy.js");
      vi.stubEnv("ADEU_ALLOWED_DIRS", root);
      await expect(
        policy.checkToolPath(join(root, "missing.docx"), "source"),
      ).rejects.toThrow(`File access refused. Allowed folders: ${allowed}`);
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });

  for (const test of safe_targets) {
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

  it.each([false, true])(
    "routes every registered path parameter through the helper (confined=%s)",
    async (confined) => {
      setup();
      const allowed = join(root, "allowed");
      fs.mkdirSync(allowed);
      const env = { ...process.env };
      if (confined) env.ADEU_ALLOWED_DIRS = allowed;
      const server = await startTestServer("path-coverage", env);
      try {
        const source = join(allowed, "source.docx");
        const output = join(allowed, "dest.docx");
        const outside = join(root, "outside.docx");
        fs.writeFileSync(outside, word);
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
              [key]: confined ? outside : "invalid\0filename",
            });
            expect(result.isError, `${tool.name}.${key}`).toBe(true);
            if (confined) {
              expect(result.content[0].text, `${tool.name}.${key}`).toBe(
                `File access refused. Allowed folders: ${allowed}`,
              );
            } else {
              expect(result.content[0].text, `${tool.name}.${key}`).toContain(
                "invalid file path",
              );
            }
          }
        }
      } finally {
        server.stop();
      }
    },
  );

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
