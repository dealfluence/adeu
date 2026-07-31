import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { ProtocolAdapter } from "./protocol-adapter.js";
import { spawn, ChildProcess } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

describe("ProtocolAdapter", () => {
  it("exports ProtocolAdapter and attachProtocolAdapter", () => {
    expect(ProtocolAdapter).toBeDefined();
  });
});

describe("MCP Server 2026-07-28 Protocol Integration", () => {
  let serverProc: ChildProcess;
  let rpcId = 1;

  function sendRpc(method: string, params: any, id = rpcId++): Promise<any> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error(`RPC Timeout for ${method}`)), 5000);
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
          } catch {}
        }
      };
      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  }

  beforeAll(() => {
    const serverPath = resolve(__dirname, "../dist/index.js");
    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
  });

  it("responds to server/discover with supported versions and resultType: complete", async () => {
    const res = await sendRpc("server/discover", {});
    expect(res.result).toBeDefined();
    expect(res.result.resultType).toBe("complete");
    expect(res.result.protocolVersions).toContain("2026-07-28");
    expect(res.result.serverInfo.name).toBe("adeu-redlining-service");
  });

  it("returns resultType: complete and CacheableResult fields on tools/list", async () => {
    await sendRpc("initialize", {
      protocolVersion: "2026-07-28",
      capabilities: {},
      clientInfo: { name: "test", version: "1.0.0" },
    });
    const res = await sendRpc("tools/list", {});
    expect(res.result.resultType).toBe("complete");
    expect(res.result.ttlMs).toBeGreaterThan(0);
    expect(res.result.cacheScope).toBe("private");
    expect(res.result.tools.length).toBeGreaterThan(0);
    
    // Verify tools are sorted deterministically (alphabetically)
    const names = res.result.tools.map((t: any) => t.name);
    const sorted = [...names].sort();
    expect(names).toEqual(sorted);
  });

  it("rejects request with unsupported protocol version in _meta with error -32022", async () => {
    const res = await sendRpc("tools/list", {
      _meta: {
        "io.modelcontextprotocol/protocolVersion": "1999-01-01",
        "io.modelcontextprotocol/clientCapabilities": {},
      },
    });
    expect(res.error).toBeDefined();
    expect(res.error.code).toBe(-32022);
    expect(res.error.message).toContain("Unsupported protocol version");
  });

  it("rejects 2026-07-28 request missing clientCapabilities in _meta with error -32602", async () => {
    const res = await sendRpc("tools/list", {
      _meta: {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      },
    });
    expect(res.error).toBeDefined();
    expect(res.error.code).toBe(-32602);
    expect(res.error.message).toContain("Missing required _meta parameter");
  });

  it("preserves legacy initialize handshake for older clients", async () => {
    const res = await sendRpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "legacy-client", version: "1.0.0" },
    });
    expect(res.result).toBeDefined();
    expect(res.result.protocolVersion).toBeDefined();
    expect(res.result.resultType).toBe("complete");
  });
});
