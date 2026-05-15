// FILE: node/packages/mcp-server/src/mcp.cloud.test.ts
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  rmSync,
} from "node:fs";

describe("Cloud Auth & Email Tools MCP Verification", () => {
  let serverProc: ChildProcess;
  let testHomeDir: string;
  let adeuConfigDir: string;
  let credPath: string;

  beforeAll(async () => {
    // 1. Create a sandboxed home directory so we don't nuke the dev's actual login
    testHomeDir = join(tmpdir(), `adeu_test_home_${Date.now()}`);
    adeuConfigDir = join(testHomeDir, ".adeu");
    credPath = join(adeuConfigDir, "credentials.json");
    mkdirSync(testHomeDir, { recursive: true });

    // 2. Boot the compiled MCP server with the sandboxed HOME environment
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before tests.",
      );
    }

    serverProc = spawn("node", [serverPath], {
      env: {
        ...process.env,
        HOME: testHomeDir, // Mock homedir() for macOS/Linux
        USERPROFILE: testHomeDir, // Mock homedir() for Windows
      },
    });
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (existsSync(testHomeDir)) {
      rmSync(testHomeDir, { recursive: true, force: true });
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

  it("CLOUD-1: Enforces authentication trap for search_and_fetch_emails", async () => {
    // Attempt to search emails without a credentials.json file in the sandbox
    const res = await sendRpc(
      "tools/call",
      {
        name: "search_and_fetch_emails",
        arguments: { subject: "Invoice" },
      },
      201,
    );

    expect(res.result.isError).toBe(true);
    expect(res.result.content[0].text).toContain("Authentication Required");
    expect(res.result.content[0].text).toContain("login_to_adeu_cloud");
  });

  it("CLOUD-2: Validates create_email_draft missing required arguments", async () => {
    // To bypass the auth trap for this test, we inject a dummy credential file
    mkdirSync(adeuConfigDir, { recursive: true });
    writeFileSync(credPath, JSON.stringify({ api_key: "dummy_test_key" }));

    const res = await sendRpc(
      "tools/call",
      {
        name: "create_email_draft",
        arguments: {
          body_markdown: "Hello World",
          // Missing reply_to_email_id AND subject/to_recipients
        },
      },
      202,
    );

    expect(res.result.isError).toBe(true);
    expect(res.result.content[0].text).toContain(
      "You must provide either 'reply_to_email_id' OR both 'subject' and 'to_recipients'",
    );
  });

  it("CLOUD-3: logout_of_adeu_cloud successfully deletes the credentials file", async () => {
    // Ensure the dummy file from the previous test exists
    expect(existsSync(credPath)).toBe(true);

    const res = await sendRpc(
      "tools/call",
      {
        name: "logout_of_adeu_cloud",
        arguments: {},
      },
      203,
    );

    expect(res.result.isError).toBeFalsy();
    expect(res.result.content[0].text).toContain("Successfully logged out");

    // Verify the file was physically deleted from the sandboxed .adeu directory
    expect(existsSync(credPath)).toBe(false);
  });
});
