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

  it("publishes tool schemas with no JSON Schema dialect declaration", async () => {
    // The MCP SDK hardcodes `target: 'draft-7'` when converting Zod schemas
    // (sdk/server/zod-json-schema-compat.js `mapMiniTarget`), so Zod v4 stamps
    // `$schema: "http://json-schema.org/draft-07/schema#"` on every published
    // schema. Claude Desktop rejects the tool outright:
    //   Tool 'read_docx' has an invalid outputSchema: JSON Schema declares an
    //   unsupported dialect ... supports JSON Schema 2020-12 only
    const res = await sendRpc("tools/list", {});
    const tools = res.result.tools;
    expect(tools.length).toBeGreaterThan(0);

    for (const tool of tools) {
      expect(
        tool.inputSchema,
        `${tool.name} must publish an inputSchema`,
      ).toBeDefined();
      expect(
        tool.inputSchema.$schema,
        `${tool.name}.inputSchema must not declare a dialect`,
      ).toBeUndefined();
      if (tool.outputSchema !== undefined) {
        expect(
          tool.outputSchema.$schema,
          `${tool.name}.outputSchema must not declare a dialect`,
        ).toBeUndefined();
      }
    }

    expect(JSON.stringify(res)).not.toContain("json-schema.org/draft-07");
  });

  it("still publishes read_docx.outputSchema after dialect stripping", async () => {
    // Guard against the obvious wrong fix: handing the SDK a plain JSON Schema
    // dict makes `normalizeObjectSchema` return undefined, so mcp.js silently
    // omits outputSchema and the MCP Apps host stops forwarding
    // structuredContent to the markdown viewer.
    const res = await sendRpc("tools/list", {});
    const readDocx = res.result.tools.find((t: any) => t.name === "read_docx");
    expect(readDocx).toBeDefined();
    expect(readDocx.outputSchema).toBeDefined();
    expect(readDocx.outputSchema.type).toBe("object");
    expect(readDocx.outputSchema.properties.markdown).toBeDefined();
    expect(readDocx.outputSchema.required).toContain("markdown");
  });

  it("publishes no draft-07-only constructs that change meaning under 2020-12", async () => {
    // Stripping `$schema` is only lossless while the emitted schemas stay
    // within the draft-07 ∩ 2020-12 subset. These keywords are the ones whose
    // meaning changed: tuple-form `items` + `additionalItems` became
    // `prefixItems` + `items`, and `definitions` became `$defs`.
    const res = await sendRpc("tools/list", {});

    const offenders: string[] = [];
    const walk = (node: any, path: string): void => {
      if (Array.isArray(node)) {
        node.forEach((v, i) => walk(v, `${path}[${i}]`));
        return;
      }
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node.items)) offenders.push(`${path}.items (tuple form)`);
      if ("additionalItems" in node) offenders.push(`${path}.additionalItems`);
      if ("definitions" in node) offenders.push(`${path}.definitions`);
      for (const [k, v] of Object.entries(node)) walk(v, `${path}.${k}`);
    };

    for (const tool of res.result.tools) {
      walk(tool.inputSchema, `${tool.name}.inputSchema`);
      if (tool.outputSchema) walk(tool.outputSchema, `${tool.name}.outputSchema`);
    }

    expect(offenders).toEqual([]);
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
