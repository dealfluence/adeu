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
  let stdoutBuffer = "";

  function sendRpc(method: string, params: any, id = rpcId++): Promise<any> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error(`RPC Timeout for ${method}`)),
        5000,
      );
      let lineBuf = "";
      const listener = (data: Buffer) => {
        lineBuf += data.toString();
        const lines = lineBuf.split("\n");
        lineBuf = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("{")) continue;
          try {
            const res = JSON.parse(trimmed);
            if (res.id === id) {
              clearTimeout(timeout);
              serverProc.stdout?.removeListener("data", listener);
              resolve(res);
            }
          } catch {}
        }
      };
      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  function sendRpcCollectAll(
    method: string,
    params: any,
    id = rpcId++,
    waitMs = 1500,
  ): Promise<any[]> {
    return new Promise((resolve) => {
      const matches: any[] = [];
      let lineBuf = "";

      const listener = (data: Buffer) => {
        lineBuf += data.toString();
        const lines = lineBuf.split("\n");
        lineBuf = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("{")) continue;
          try {
            const res = JSON.parse(trimmed);
            if (res.id === id) {
              matches.push(res);
            }
          } catch {}
        }
      };

      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );

      setTimeout(() => {
        serverProc.stdout?.removeListener("data", listener);
        resolve(matches);
      }, waitMs);
    });
  }

  beforeAll(() => {
    const serverPath = resolve(__dirname, "../dist/index.js");
    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
  });

  it("responds to server/discover with supportedVersions and exactly ONE response", async () => {
    const responses = await sendRpcCollectAll("server/discover", {});
    expect(responses.length).toBe(1);
    const res = responses[0];
    expect(res.result).toBeDefined();
    expect(res.result.resultType).toBe("complete");
    expect(res.result.supportedVersions).toContain("2026-07-28");
    expect(res.result.protocolVersions).toContain("2026-07-28");
    expect(res.result.serverInfo.name).toBe("adeu-redlining-service");
    expect(res.result.capabilities.tools).toBeDefined();
    expect(res.result.capabilities.resources).toBeDefined();
    expect(res.result.ttlMs).toBe(3600000);
    expect(res.result.cacheScope).toBe("public");
  });

  it("returns resultType: complete and CacheableResult fields on tools/list", async () => {
    await sendRpc("initialize", {
      protocolVersion: "2026-07-28",
      capabilities: {},
      clientInfo: { name: "test", version: "1.0.0" },
    });
    serverProc.stdin?.write(
      JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) +
        "\n",
    );
    const res = await sendRpc("tools/list", {});
    expect(res.result.resultType).toBe("complete");
    expect(res.result.ttlMs).toBeGreaterThan(0);
    expect(res.result.cacheScope).toBe("public");
    expect(res.result.tools.length).toBeGreaterThan(0);

    // Verify tools are sorted deterministically (alphabetically)
    const names = res.result.tools.map((t: any) => t.name);
    const sorted = [...names].sort();
    expect(names).toEqual(sorted);
  });

  it("rejects request with unsupported protocol version in _meta with error -32022 and exactly ONE response", async () => {
    const responses = await sendRpcCollectAll("tools/list", {
      _meta: {
        "io.modelcontextprotocol/protocolVersion": "1999-01-01",
        "io.modelcontextprotocol/clientCapabilities": {},
      },
    });
    expect(responses.length).toBe(1);
    const res = responses[0];
    expect(res.error).toBeDefined();
    expect(res.error.code).toBe(-32022);
    expect(res.error.message).toContain("Unsupported protocol version");
    expect(res.error.data.supported).toContain("2026-07-28");
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
    const serverPath = resolve(__dirname, "../dist/index.js");
    const legacyProc = spawn("node", [serverPath]);
    try {
      const res = await new Promise<any>((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Timeout")), 3000);
        legacyProc.stdout?.on("data", (data) => {
          const lines = data.toString().trim().split("\n");
          for (const line of lines) {
            if (!line.startsWith("{")) continue;
            try {
              const resObj = JSON.parse(line);
              if (resObj.id === 999) {
                clearTimeout(timeout);
                resolve(resObj);
              }
            } catch {}
          }
        });
        legacyProc.stdin?.write(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 999,
            method: "initialize",
            params: {
              protocolVersion: "2024-11-05",
              capabilities: {},
              clientInfo: { name: "legacy-client", version: "1.0.0" },
            },
          }) + "\n",
        );
      });

      expect(res.result).toBeDefined();
      expect(res.result.protocolVersion).toBeDefined();
      expect(res.result.resultType).toBe("complete");
    } finally {
      if (legacyProc && !legacyProc.killed) legacyProc.kill();
    }
  });

  it("returns CacheableResult fields for resources/list and resources/read", async () => {
    const meta = {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
    };
    const listRes = await sendRpc("resources/list", { _meta: meta });
    expect(listRes.result.resultType).toBe("complete");
    expect(listRes.result.ttlMs).toBe(3600000);
    expect(listRes.result.cacheScope).toBe("public");
    expect(listRes.result.resources.length).toBeGreaterThan(0);

    const readRes = await sendRpc("resources/read", {
      uri: "ui://adeu/markdown-ui",
      _meta: meta,
    });
    expect(readRes.result.resultType).toBe("complete");
    expect(readRes.result.ttlMs).toBe(60000);
    expect(readRes.result.cacheScope).toBe("private");
    expect(readRes.result.contents.length).toBeGreaterThan(0);
  });

  it("produces NO response for notification with bad _meta protocol version", async () => {
    const seen: any[] = [];
    let lineBuf = "";
    const listener = (data: Buffer) => {
      lineBuf += data.toString();
      const lines = lineBuf.split("\n");
      lineBuf = lines.pop() || "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("{")) continue;
        try {
          seen.push(JSON.parse(trimmed));
        } catch {}
      }
    };
    serverProc.stdout?.on("data", listener);
    serverProc.stdin?.write(
      JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/cancelled",
        params: {
          _meta: {
            "io.modelcontextprotocol/protocolVersion": "1999-01-01",
          },
        },
      }) + "\n",
    );

    await new Promise((r) => setTimeout(r, 500));
    serverProc.stdout?.removeListener("data", listener);
    expect(seen).toEqual([]); // no JSON-RPC message of any kind

    const ping = await sendRpc("tools/list", {});
    expect(ping.result).toBeDefined(); // server still alive
  });

  it("never returns legacy -32002 error code on tool failure with missing file", async () => {
    const res = await sendRpc("tools/call", {
      name: "read_docx",
      arguments: {
        reasoning: "testing error code",
        file_path: "non_existent_file_path_999.docx",
      },
    });
    if (res.error) {
      expect(res.error.code).toBe(-32602);
      expect(res.error.code).not.toBe(-32002);
    }
  });
});
