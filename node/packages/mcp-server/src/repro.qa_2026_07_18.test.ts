// FILE: node/packages/mcp-server/src/repro.qa_2026_07_18.test.ts
//
// QA 2026-07-18 H1 — the diff tool used to extract WITH the read-only
// structural appendix, so two documents whose only difference changes a
// defined-term usage count produced phantom "used N times" hunks that no
// apply could ever consume. Exercised end-to-end against the REAL compiled
// MCP server over stdio JSON-RPC (mirrors mcp.bugs.test.ts).

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { DocumentObject } from "@adeu/core";

describe("QA 2026-07-18 H1 — diff tool must not leak the appendix (real MCP server)", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let origPath: string;
  let modPath: string;

  async function buildDefinedTermsDoc(extraUses: boolean): Promise<Buffer> {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/initial.docx",
    );
    const doc = await DocumentObject.load(readFileSync(fixturePath));
    const body = doc.element;
    while (body.firstChild) body.removeChild(body.firstChild);
    const x = body.ownerDocument!;
    const addP = (text: string) => {
      const p = x.createElement("w:p");
      const r = x.createElement("w:r");
      const t = x.createElement("w:t");
      t.textContent = text;
      t.setAttribute("xml:space", "preserve");
      r.appendChild(t);
      p.appendChild(r);
      body.appendChild(p);
    };
    addP('"Agreement" means this service agreement between the parties.');
    addP("The Agreement enters into force upon signature.");
    if (extraUses) {
      addP("Termination of the Agreement requires notice.");
      addP("Amendments to the Agreement must be written.");
    }
    return doc.save();
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_repro_h1_"));
    origPath = join(workDir, "orig.docx");
    modPath = join(workDir, "mod.docx");
    writeFileSync(origPath, await buildDefinedTermsDoc(false));
    writeFileSync(modPath, await buildDefinedTermsDoc(true));

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
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
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

  it("diff_docx_files reports the body change but no appendix hunks", async () => {
    const r = await sendRpc(
      "tools/call",
      {
        name: "diff_docx_files",
        arguments: {
          reasoning: "test",
          original_path: origPath,
          modified_path: modPath,
          compare_clean: true,
        },
      },
      501,
    );
    const text = r.result.content[0].text as string;
    // The real body change is present…
    expect(text).toContain("Termination");
    // …but nothing from the generated read-only appendix (QA H1).
    expect(text).not.toContain("— used ");
    expect(text).not.toContain("Document Structure");
  }, 20000);
});
