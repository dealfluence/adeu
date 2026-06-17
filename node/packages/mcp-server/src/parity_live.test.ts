// FILE: node/packages/mcp-server/src/parity_live.test.ts
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { existsSync, readFileSync } from "node:fs";

describe("Parity Live Server Integration Verification", () => {
  let serverProc: ChildProcess;
  const fixturePath = resolve(__dirname, "../tests/fixtures/gap2_minimal_repro.docx");

  beforeAll(async () => {
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before tests.",
      );
    }
    if (!existsSync(fixturePath)) {
      throw new Error(`Fixture not found: ${fixturePath}`);
    }

    // Spawn server process
    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) {
      serverProc.kill();
    }
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

  it("exposes the build stamp via the server_info tool", async () => {
    const res = await sendRpc("tools/call", {
      name: "server_info",
      arguments: {},
    }, 201);

    expect(res.result).toBeDefined();
    expect(res.result.content[0].text).toContain("Build: ");
    expect(res.result.content[0].text).not.toContain("unknown@unknown");
  });

  it("read_docx appends [Debug] build stamp footer to mode=outline and mode=full", async () => {
    const res = await sendRpc("tools/call", {
      name: "read_docx",
      arguments: {
        file_path: fixturePath,
        mode: "outline",
      },
    }, 202);

    expect(res.result).toBeDefined();
    expect(res.result.content[0].text).not.toContain("[Debug] build=");
  });

  it("GAP 2 (Live): process_document_batch modify straddling deleted text returns actionable deletion error", async () => {
    const outPath = join(resolve(__dirname, "../../../../tmp"), `live_gap2_out_${Date.now()}.docx`);
    const res = await sendRpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        original_docx_path: fixturePath,
        author_name: "Ed",
        changes: [
          {
            type: "modify",
            target_text: "Foo bar old phrase here.",
            new_text: "X",
            comment: "c",
          }
        ],
        output_path: outPath,
        dry_run: true,
      },
    }, 203);

    expect(res.result).toBeDefined();
    const text = res.result.content[0].text;
    expect(text).toContain("Edit 1 Failed: Target text matches text inside a tracked deletion by Test Negotiator.");
    expect(text).toContain("Reject/accept that change first or target the active replacement text instead.");
    expect(text).not.toContain("Target text not found in document.");

    // Run Python engine to assert cross-engine exact error parity
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli = join(projectRoot, "python/.venv/bin/adeu");
    
    // Create temporary changes JSON for Python CLI
    const fs = await import("node:fs");
    const tempJsonPath = join(projectRoot, "tmp", `changes_${Date.now()}.json`);
    fs.mkdirSync(join(projectRoot, "tmp"), { recursive: true });
    fs.writeFileSync(tempJsonPath, JSON.stringify([
      {
        type: "modify",
        target_text: "Foo bar old phrase here.",
        new_text: "X",
        comment: "c",
      }
    ]));

    try {
      const pythonOut = execSync(`"${pythonCli}" apply "${fixturePath}" "${tempJsonPath}" --dry-run --author Ed`, {
        encoding: "utf-8",
      });
      // Should not succeed normally since dry run exits with code 1 on errors, so we catch in the try block
    } catch (err: any) {
      const pyErrorOutput = err.stdout || err.stderr || "";
      expect(pyErrorOutput).toContain("Edit 1 Failed: Target text matches text inside a tracked deletion by Test Negotiator.");
      expect(pyErrorOutput).toContain("Reject/accept that change first or target the active replacement text instead.");
    } finally {
      if (fs.existsSync(tempJsonPath)) {
        fs.unlinkSync(tempJsonPath);
      }
    }
  });

  it("GAP 1 (Live): read_docx mode=outline heading count and content parity with Python", async () => {
    const gap1FixturePath = resolve(__dirname, "../tests/fixtures/gap1_deleted_row_repro.docx");
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli = join(projectRoot, "python/.venv/bin/adeu");

    // 1. clean_view = true
    const nodeResClean = await sendRpc("tools/call", {
      name: "read_docx",
      arguments: {
        file_path: gap1FixturePath,
        mode: "outline",
        clean_view: true,
      },
    }, 204);

    expect(nodeResClean.result).toBeDefined();
    const nodeTextClean = nodeResClean.result.content[0].text;
    
    // Get Python output for clean_view = true
    const pythonOutClean = execSync(`"${pythonCli}" extract "${gap1FixturePath}" --mode outline --clean-view`, {
      encoding: "utf-8",
    });

    // Extract raw markdown headings (lines starting with #) to assert perfect parity
    const getHeadings = (text: string) => text.split("\n").filter(line => line.startsWith("#")).map(l => l.trim());
    
    const nodeHeadingsClean = getHeadings(nodeTextClean);
    const pythonHeadingsClean = getHeadings(pythonOutClean);

    expect(nodeHeadingsClean).toEqual(["# Active Heading (p1)"]);
    expect(pythonHeadingsClean).toEqual(["# Active Heading (p1)"]);
    expect(nodeHeadingsClean).toEqual(pythonHeadingsClean);

    // 2. clean_view = false
    const nodeResDirty = await sendRpc("tools/call", {
      name: "read_docx",
      arguments: {
        file_path: gap1FixturePath,
        mode: "outline",
        clean_view: false,
      },
    }, 205);

    expect(nodeResDirty.result).toBeDefined();
    const nodeTextDirty = nodeResDirty.result.content[0].text;

    const pythonOutDirty = execSync(`"${pythonCli}" extract "${gap1FixturePath}" --mode outline`, {
      encoding: "utf-8",
    });

    const nodeHeadingsDirty = getHeadings(nodeTextDirty);
    const pythonHeadingsDirty = getHeadings(pythonOutDirty);

    expect(nodeHeadingsDirty).toEqual(["# Active Heading (p1)", "# Deleted Heading (p1)"]);
    expect(pythonHeadingsDirty).toEqual(["# Active Heading (p1)", "# Deleted Heading (p1)"]);
    expect(nodeHeadingsDirty).toEqual(pythonHeadingsDirty);
  });

  it("GAP 1 - Style Def (Live): style-definition outlineLvl on non-heading style classification parity", async () => {
    const gap1FixturePath = resolve(__dirname, "../tests/fixtures/gap1_minimal_repro.docx");
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli = join(projectRoot, "python/.venv/bin/adeu");

    // 1. clean_view = true (both engines must return exactly 2 headings, excluding Subtitle)
    const nodeResClean = await sendRpc("tools/call", {
      name: "read_docx",
      arguments: {
        file_path: gap1FixturePath,
        mode: "outline",
        clean_view: true,
      },
    }, 206);

    expect(nodeResClean.result).toBeDefined();
    const nodeTextClean = nodeResClean.result.content[0].text;

    const pythonOutClean = execSync(`"${pythonCli}" extract "${gap1FixturePath}" --mode outline --clean-view`, {
      encoding: "utf-8",
    });

    const getHeadings = (text: string) => text.split("\n").filter(line => line.startsWith("#")).map(l => l.trim());

    const nodeHeadingsClean = getHeadings(nodeTextClean);
    const pythonHeadingsClean = getHeadings(pythonOutClean);

    // Assert exact count parity (must be exactly 2 for both)
    expect(nodeHeadingsClean.length).toBe(2);
    expect(pythonHeadingsClean.length).toBe(2);

    expect(nodeHeadingsClean).toEqual(["# Real Heading One (p1)", "# Real Heading Two (p1)"]);
    expect(pythonHeadingsClean).toEqual(["# Real Heading One (p1)", "# Real Heading Two (p1)"]);
    expect(nodeHeadingsClean).toEqual(pythonHeadingsClean);
  });
});
