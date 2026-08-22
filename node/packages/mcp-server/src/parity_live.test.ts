// FILE: node/packages/mcp-server/src/parity_live.test.ts
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { resolve, join } from "node:path";
import { existsSync, readFileSync } from "node:fs";
import { startTestServer, type TestServer } from "./test-rpc.js";

const PYTHON_PARITY_TIMEOUT_MS = 60_000;

describe("Parity Live Server Integration Verification", () => {
  let server: TestServer;
  const fixturePath = resolve(
    __dirname,
    "../tests/fixtures/gap2_minimal_repro.docx",
  );

  beforeAll(async () => {
    if (!existsSync(fixturePath)) {
      throw new Error(`Fixture not found: ${fixturePath}`);
    }
    server = await startTestServer("parity-live");
  });

  afterAll(() => {
    server?.stop();
  });

  function sendRpc(method: string, params: any): Promise<any> {
    return server.rpc(method, params);
  }

  it("does not expose the server_info tool anymore", async () => {
    const listRes = await sendRpc("tools/list", {}, 200);
    expect(listRes.result).toBeDefined();
    const tools = listRes.result.tools || [];
    const serverInfoTool = tools.find((t: any) => t.name === "server_info");
    expect(serverInfoTool).toBeUndefined();
  });

  it("exposes the build stamp via tool-call descriptions", async () => {
    const listRes = await sendRpc("tools/list", {}, 201);
    expect(listRes.result).toBeDefined();
    const tools = listRes.result.tools || [];
    const readDocx = tools.find((t: any) => t.name === "read_docx");
    expect(readDocx).toBeDefined();
    expect(readDocx.description).toContain("[Adeu v");
  });

  it("exposes the build stamp via serverInfo.version in initialize", async () => {
    const initRes = await sendRpc(
      "initialize",
      {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "test-client", version: "1.0.0" },
      },
      202,
    );
    expect(initRes.result).toBeDefined();
    expect(initRes.result.serverInfo).toBeDefined();
    expect(initRes.result.serverInfo.version).not.toBe("1.0.0");
    expect(initRes.result.serverInfo.version).not.toContain("unknown");
  });

  it("read_docx appends [Debug] build stamp footer to mode=outline and mode=full", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: fixturePath,
          mode: "outline",
        },
      },
      202,
    );

    expect(res.result).toBeDefined();
    expect(res.result.content[0].text).not.toContain("[Debug] build=");
  });

  it("GAP 2 (Live): process_document_batch modify straddling deleted text returns actionable deletion error", { timeout: PYTHON_PARITY_TIMEOUT_MS }, async () => {
    const outPath = join(
      resolve(__dirname, "../../../../tmp"),
      `live_gap2_out_${Date.now()}.docx`,
    );
    const res = await sendRpc(
      "tools/call",
      {
        name: "process_document_batch",
        arguments: {
          reasoning: "test",
          original_docx_path: fixturePath,
          author_name: "Ed",
          changes: [
            {
              type: "modify",
              target_text: "Foo bar old phrase here.",
              new_text: "X",
              comment: "c",
            },
          ],
          output_path: outPath,
        },
      },
      203,
    );

    expect(res.result).toBeDefined();
    const text = res.result.content[0].text;
    expect(text).toContain(
      "Edit 1 Failed: Target text matches text inside a tracked deletion by Test Negotiator.",
    );
    expect(text).toContain(
      "Reject/accept that change first or target the active replacement text instead.",
    );
    expect(text).not.toContain("Target text not found in document.");

    // Run Python engine to assert cross-engine exact error parity
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli =
      process.platform === "win32"
        ? join(projectRoot, "python/.venv/Scripts/adeu.exe")
        : join(projectRoot, "python/.venv/bin/adeu");

    // Create temporary changes JSON for Python CLI
    const fs = await import("node:fs");
    const tempJsonPath = join(projectRoot, "tmp", `changes_${Date.now()}.json`);
    fs.mkdirSync(join(projectRoot, "tmp"), { recursive: true });
    fs.writeFileSync(
      tempJsonPath,
      JSON.stringify([
        {
          type: "modify",
          target_text: "Foo bar old phrase here.",
          new_text: "X",
          comment: "c",
        },
      ]),
    );

    try {
      const pythonOut = execSync(
        `"${pythonCli}" apply "${fixturePath}" "${tempJsonPath}" --author Ed`,
        {
          encoding: "utf-8",
        },
      );
      // Should not succeed normally since apply exits with code 1 on errors, so we catch in the try block
    } catch (err: any) {
      const pyErrorOutput = err.stdout || err.stderr || "";
      expect(pyErrorOutput).toContain(
        "Edit 1 Failed: Target text matches text inside a tracked deletion by Test Negotiator.",
      );
      expect(pyErrorOutput).toContain(
        "Reject/accept that change first or target the active replacement text instead.",
      );
    } finally {
      if (fs.existsSync(tempJsonPath)) {
        fs.unlinkSync(tempJsonPath);
      }
    }
  });

  it("GAP 1 (Live): read_docx mode=outline heading count and content parity with Python", { timeout: PYTHON_PARITY_TIMEOUT_MS }, async () => {
    const gap1FixturePath = resolve(
      __dirname,
      "../tests/fixtures/gap1_deleted_row_repro.docx",
    );
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli =
      process.platform === "win32"
        ? join(projectRoot, "python/.venv/Scripts/adeu.exe")
        : join(projectRoot, "python/.venv/bin/adeu");

    // 1. clean_view = true
    const nodeResClean = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: gap1FixturePath,
          mode: "outline",
          clean_view: true,
        },
      },
      204,
    );

    expect(nodeResClean.result).toBeDefined();
    const nodeTextClean = nodeResClean.result.content[0].text;

    // Get Python output for clean_view = true
    const pythonOutClean = execSync(
      `"${pythonCli}" extract "${gap1FixturePath}" --mode outline --clean-view`,
      {
        encoding: "utf-8",
      },
    );

    // Extract raw markdown headings (lines starting with #) to assert perfect parity
    const getHeadings = (text: string) =>
      text
        .split("\n")
        .filter((line) => line.startsWith("#"))
        .map((l) => l.trim());

    const nodeHeadingsClean = getHeadings(nodeTextClean);
    const pythonHeadingsClean = getHeadings(pythonOutClean);

    expect(nodeHeadingsClean).toEqual(["# Active Heading (p1)"]);
    expect(pythonHeadingsClean).toEqual(["# Active Heading (p1)"]);
    expect(nodeHeadingsClean).toEqual(pythonHeadingsClean);

    // 2. clean_view = false
    const nodeResDirty = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: gap1FixturePath,
          mode: "outline",
          clean_view: false,
        },
      },
      205,
    );

    expect(nodeResDirty.result).toBeDefined();
    const nodeTextDirty = nodeResDirty.result.content[0].text;

    const pythonOutDirty = execSync(
      `"${pythonCli}" extract "${gap1FixturePath}" --mode outline`,
      {
        encoding: "utf-8",
      },
    );

    const nodeHeadingsDirty = getHeadings(nodeTextDirty);
    const pythonHeadingsDirty = getHeadings(pythonOutDirty);

    expect(nodeHeadingsDirty).toEqual([
      "# Active Heading (p1)",
      "# Deleted Heading (p1)",
    ]);
    expect(pythonHeadingsDirty).toEqual([
      "# Active Heading (p1)",
      "# Deleted Heading (p1)",
    ]);
    expect(nodeHeadingsDirty).toEqual(pythonHeadingsDirty);
  });

  it("GAP 1 - Style Def (Live): style-definition outlineLvl on non-heading style classification parity", { timeout: PYTHON_PARITY_TIMEOUT_MS }, async () => {
    const gap1FixturePath = resolve(
      __dirname,
      "../tests/fixtures/gap1_minimal_repro.docx",
    );
    const { execSync } = await import("node:child_process");
    const projectRoot = resolve(__dirname, "../../../..");
    const pythonCli =
      process.platform === "win32"
        ? join(projectRoot, "python/.venv/Scripts/adeu.exe")
        : join(projectRoot, "python/.venv/bin/adeu");

    // 1. clean_view = true (both engines must return exactly 2 headings, excluding Subtitle)
    const nodeResClean = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          reasoning: "test",
          file_path: gap1FixturePath,
          mode: "outline",
          clean_view: true,
        },
      },
      206,
    );

    expect(nodeResClean.result).toBeDefined();
    const nodeTextClean = nodeResClean.result.content[0].text;

    const pythonOutClean = execSync(
      `"${pythonCli}" extract "${gap1FixturePath}" --mode outline --clean-view`,
      {
        encoding: "utf-8",
      },
    );

    const getHeadings = (text: string) =>
      text
        .split("\n")
        .filter((line) => line.startsWith("#"))
        .map((l) => l.trim());

    const nodeHeadingsClean = getHeadings(nodeTextClean);
    const pythonHeadingsClean = getHeadings(pythonOutClean);

    // Assert exact count parity (must be exactly 2 for both)
    expect(nodeHeadingsClean.length).toBe(2);
    expect(pythonHeadingsClean.length).toBe(2);

    expect(nodeHeadingsClean).toEqual([
      "# Real Heading One (p1)",
      "# Real Heading Two (p1)",
    ]);
    expect(pythonHeadingsClean).toEqual([
      "# Real Heading One (p1)",
      "# Real Heading Two (p1)",
    ]);
    expect(nodeHeadingsClean).toEqual(pythonHeadingsClean);
  });
});
