import { describe, it, expect, vi } from "vitest";
import { EXTENSION_ID, RESOURCE_MIME_TYPE } from "@modelcontextprotocol/ext-apps/server";
import {
  SUPPORTED_PROTOCOL_VERSIONS,
  MODERN_PROTOCOL_VERSION,
  MCP_ERROR_CODES,
  CACHE_POLICY,
  UI_EXTENSION_CAPABILITY,
  readServerCapabilities,
  ProtocolAdapter,
  attachProtocolAdapter,
  META_PROTOCOL_VERSION,
  META_CLIENT_CAPABILITIES,
  META_LOG_LEVEL,
  stripJsonSchemaDialect,
} from "./protocol-adapter.js";

/**
 * The dialect the MCP SDK stamps onto every Zod-derived tool schema. Kept as a
 * test-local literal on purpose: production code strips ANY `$schema`, so it
 * must never grow a dependency on this specific URI.
 */
const DRAFT_07_DIALECT = "http://json-schema.org/draft-07/schema#";

describe("ProtocolAdapter Phase 1: Constants & Capability Reader", () => {
  it("exports supported versions and modern protocol version", () => {
    expect(SUPPORTED_PROTOCOL_VERSIONS).toEqual(["2026-07-28", "2025-11-25", "2024-11-05"]);
    expect(MODERN_PROTOCOL_VERSION).toBe("2026-07-28");
  });

  it("exports standard error codes per 2026-07-28 spec", () => {
    expect(MCP_ERROR_CODES).toEqual({
      INVALID_PARAMS: -32602,
      HEADER_MISMATCH: -32020,
      MISSING_REQUIRED_CLIENT_CAPABILITY: -32021,
      UNSUPPORTED_PROTOCOL_VERSION: -32022,
      LEGACY_RESOURCE_NOT_FOUND: -32002,
    });
  });

  it("exports CACHE_POLICY with required spec endpoints and properties", () => {
    const keys = Object.keys(CACHE_POLICY).sort();
    expect(keys).toEqual([
      "prompts/list",
      "resources/list",
      "resources/read",
      "resources/templates/list",
      "server/discover",
      "tools/list",
    ]);

    for (const [method, policy] of Object.entries(CACHE_POLICY)) {
      expect(Number.isInteger(policy.ttlMs)).toBe(true);
      expect(policy.ttlMs).toBeGreaterThanOrEqual(0);
      expect(["public", "private"]).toContain(policy.cacheScope);
      if (method === "resources/read") {
        expect(policy.cacheScope).toBe("private");
      } else {
        expect(policy.cacheScope).toBe("public");
      }
    }
  });

  it("exports UI_EXTENSION_CAPABILITY matching ext-apps constants", () => {
    expect(UI_EXTENSION_CAPABILITY).toEqual({
      [EXTENSION_ID]: {
        mimeTypes: [RESOURCE_MIME_TYPE],
      },
    });
  });

  it("readServerCapabilities returns deep copy merged with UI_EXTENSION_CAPABILITY", () => {
    const mockMcpServer = {
      server: {
        getCapabilities: () => ({
          tools: { listChanged: true },
        }),
      },
    };

    const caps1 = readServerCapabilities(mockMcpServer as any);
    expect(caps1).toEqual({
      tools: { listChanged: true },
      extensions: UI_EXTENSION_CAPABILITY,
    });

    // Verify deep copy (modifying returned object does not alter subsequent call)
    (caps1 as any).tools.listChanged = false;

    const caps2 = readServerCapabilities(mockMcpServer as any);
    expect(caps2).toEqual({
      tools: { listChanged: true },
      extensions: UI_EXTENSION_CAPABILITY,
    });
  });

  it("readServerCapabilities falls back cleanly when getCapabilities throws, returns null, or returns an array", () => {
    const fallbackExpected = {
      tools: { listChanged: true },
      resources: { listChanged: true },
      extensions: UI_EXTENSION_CAPABILITY,
    };

    expect(readServerCapabilities({} as any)).toEqual(fallbackExpected);
    expect(readServerCapabilities(null as any)).toEqual(fallbackExpected);
    expect(
      readServerCapabilities({
        server: {
          getCapabilities: () => null,
        },
      } as any),
    ).toEqual(fallbackExpected);

    expect(
      readServerCapabilities({
        server: {
          getCapabilities: () => {
            throw new Error("getCapabilities error");
          },
        },
      } as any),
    ).toEqual(fallbackExpected);

    expect(
      readServerCapabilities({
        server: {
          getCapabilities: () => ["invalid_array_caps"],
        },
      } as any),
    ).toEqual(fallbackExpected);
  });
});

describe("ProtocolAdapter Phase 2: validateIncomingRequest", () => {
  const adapter = new ProtocolAdapter("test-server", "1.0.0");

  it("passes legacy requests missing _meta.protocolVersion", () => {
    const res1 = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/list",
      params: {},
    });
    expect(res1).toEqual({ action: "pass" });

    const res2 = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 2,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "test", version: "1.0.0" },
      },
    });
    expect(res2).toEqual({ action: "pass" });
  });

  it("rejects unsupported protocol version in _meta with code -32022 and data", () => {
    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 10,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "1999-01-01",
          [META_CLIENT_CAPABILITIES]: {},
        },
      },
    });
    expect(res.action).toBe("respond");
    if (res.action === "respond") {
      expect(res.message.id).toBe(10);
      expect(res.message.error).toEqual({
        code: -32022,
        message: "Unsupported protocol version: 1999-01-01. Supported versions: 2026-07-28, 2025-11-25, 2024-11-05",
        data: {
          requested: "1999-01-01",
          supported: ["2026-07-28", "2025-11-25", "2024-11-05"],
        },
      });
    }
  });

  it("rejects non-string or invalid protocol version types with -32022", () => {
    for (const val of [42, null, true, {}]) {
      const res = adapter.validateIncomingRequest({
        jsonrpc: "2.0",
        id: 11,
        method: "tools/list",
        params: {
          _meta: {
            [META_PROTOCOL_VERSION]: val,
          },
        },
      });
      expect(res.action).toBe("respond");
      if (res.action === "respond") {
        expect((res.message as any).error.code).toBe(-32022);
      }
    }
  });

  it("rejects 2026-07-28 requests missing or invalid clientCapabilities in _meta with code -32602", () => {
    const invalidCaps = [undefined, null, [], "invalid", 123];
    for (const caps of invalidCaps) {
      const meta: any = {
        [META_PROTOCOL_VERSION]: "2026-07-28",
      };
      if (caps !== undefined) {
        meta[META_CLIENT_CAPABILITIES] = caps;
      }
      const res = adapter.validateIncomingRequest({
        jsonrpc: "2.0",
        id: 12,
        method: "tools/list",
        params: { _meta: meta },
      });
      expect(res.action).toBe("respond");
      if (res.action === "respond") {
        expect((res.message as any).error.code).toBe(-32602);
        expect((res.message as any).error.message).toContain(
          "Missing required _meta parameter: io.modelcontextprotocol/clientCapabilities",
        );
      }
    }
  });

  it("passes valid 2026-07-28 request with clientCapabilities object", () => {
    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 13,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "2026-07-28",
          [META_CLIENT_CAPABILITIES]: {},
        },
      },
    });
    expect(res).toEqual({ action: "pass" });
  });

  it("passes 2025-11-25 request without clientCapabilities", () => {
    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 14,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "2025-11-25",
        },
      },
    });
    expect(res).toEqual({ action: "pass" });
  });

  it("validates io.modelcontextprotocol/logLevel when present", () => {
    const badRes = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 15,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "2026-07-28",
          [META_CLIENT_CAPABILITIES]: {},
          [META_LOG_LEVEL]: "verbose",
        },
      },
    });
    expect(badRes.action).toBe("respond");
    if (badRes.action === "respond") {
      expect((badRes.message as any).error.code).toBe(-32602);
      expect((badRes.message as any).error.message).toContain("Invalid log level");
    }

    const goodRes = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 16,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "2026-07-28",
          [META_CLIENT_CAPABILITIES]: {},
          [META_LOG_LEVEL]: "warning",
        },
      },
    });
    expect(goodRes).toEqual({ action: "pass" });
  });

  it("never responds to a notification without id", () => {
    const notification = {
      jsonrpc: "2.0",
      method: "notifications/cancelled",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "1999-01-01",
        },
      },
    };
    const res = adapter.validateIncomingRequest(notification);
    expect(res).toEqual({ action: "pass" });
  });

  it("handles numeric 0 id correctly for error responses", () => {
    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 0,
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "bad-version",
        },
      },
    });
    expect(res.action).toBe("respond");
    if (res.action === "respond") {
      expect(res.message.id).toBe(0);
    }
  });

  it("passes non-request messages, non-objects, and invalid jsonrpc", () => {
    expect(adapter.validateIncomingRequest(null)).toEqual({ action: "pass" });
    expect(adapter.validateIncomingRequest("string")).toEqual({ action: "pass" });
    expect(adapter.validateIncomingRequest({})).toEqual({ action: "pass" });
    expect(
      adapter.validateIncomingRequest({
        jsonrpc: "1.0",
        id: 1,
        method: "test",
      }),
    ).toEqual({ action: "pass" });
    expect(
      adapter.validateIncomingRequest({
        jsonrpc: "2.0",
        id: 5,
        result: {},
      }),
    ).toEqual({ action: "pass" });
  });

  it("bounds pendingMethods size at 512", () => {
    const freshAdapter = new ProtocolAdapter("test", "1.0.0");
    for (let i = 1; i <= 600; i++) {
      freshAdapter.validateIncomingRequest({
        jsonrpc: "2.0",
        id: `req-${i}`,
        method: "tools/list",
      });
    }
    expect(freshAdapter.pendingRequestCount).toBeLessThanOrEqual(512);
  });
});

describe("ProtocolAdapter Phase 3: server/discover", () => {
  it("builds a fully spec-compliant discover response", () => {
    const mockServer = {
      server: {
        getCapabilities: () => ({
          tools: { listChanged: true },
          resources: { listChanged: true },
        }),
      },
    };
    const adapter = new ProtocolAdapter("adeu-test-server", "1.2.3", {
      getCapabilities: () => readServerCapabilities(mockServer),
    });

    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "d1",
      method: "server/discover",
      params: {},
    });

    expect(res.action).toBe("respond");
    if (res.action === "respond") {
      const msg = res.message as any;
      expect(msg.id).toBe("d1");
      expect(msg.jsonrpc).toBe("2.0");
      expect(msg.result.resultType).toBe("complete");
      expect(msg.result.supportedVersions).toEqual([
        "2026-07-28",
        "2025-11-25",
        "2024-11-05",
      ]);
      expect(msg.result.protocolVersions).toEqual([
        "2026-07-28",
        "2025-11-25",
        "2024-11-05",
      ]);
      expect(msg.result.capabilities.tools).toEqual({ listChanged: true });
      expect(msg.result.capabilities.resources).toEqual({ listChanged: true });
      expect(
        msg.result.capabilities.extensions["io.modelcontextprotocol/ui"]
          .mimeTypes,
      ).toContain("text/html;profile=mcp-app");

      expect(msg.result.serverInfo).toEqual({
        name: "adeu-test-server",
        version: "1.2.3",
      });
      expect(msg.result._meta["io.modelcontextprotocol/serverInfo"]).toEqual({
        name: "adeu-test-server",
        version: "1.2.3",
      });
      expect(msg.result.ttlMs).toBe(3600000);
      expect(msg.result.cacheScope).toBe("public");

      // Idempotency check with transformOutgoingMessage
      const transformed = adapter.transformOutgoingMessage(msg);
      expect(transformed).toEqual(msg);

      // Verify deep copy isolation: mutating returned capabilities object does NOT mutate constant
      msg.result.capabilities.extensions["io.modelcontextprotocol/ui"].mimeTypes.push("invalid/mime");
      expect(
        UI_EXTENSION_CAPABILITY["io.modelcontextprotocol/ui"].mimeTypes,
      ).not.toContain("invalid/mime");
    }
  });
});

describe("ProtocolAdapter Phase 4: transformOutgoingMessage", () => {
  it("adds resultType and serverInfo to ordinary result responses", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const msg = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: 1,
      result: {},
    });
    expect(msg.result.resultType).toBe("complete");
    expect(msg.result._meta["io.modelcontextprotocol/serverInfo"]).toEqual({
      name: "srv",
      version: "1.0",
    });
  });

  it("does NOT overwrite input_required resultType or add caching hints to input_required", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/list",
    });

    const msg = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: 2,
      result: {
        resultType: "input_required",
        tools: [{ name: "tool1" }],
      },
    });

    expect(msg.result.resultType).toBe("input_required");
    expect(msg.result.ttlMs).toBeUndefined();
    expect(msg.result.cacheScope).toBeUndefined();
  });

  it("preserves existing _meta properties and does NOT overwrite existing serverInfo", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const msg = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: 3,
      result: {
        _meta: {
          ui: { resourceUri: "ui://adeu/markdown-ui" },
          "io.modelcontextprotocol/serverInfo": {
            name: "custom-server",
            version: "0.1",
          },
        },
      },
    });

    expect(msg.result._meta.ui).toEqual({
      resourceUri: "ui://adeu/markdown-ui",
    });
    expect(msg.result._meta["io.modelcontextprotocol/serverInfo"]).toEqual({
      name: "custom-server",
      version: "0.1",
    });
  });

  it("attaches cache policy to primed requests (tools/list, resources/read, etc)", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");

    // tools/list -> public
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "t1",
      method: "tools/list",
    });
    const toolsRes = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "t1",
      result: { tools: [{ name: "b" }, { name: "a" }] },
    });
    expect(toolsRes.result.ttlMs).toBe(3600000);
    expect(toolsRes.result.cacheScope).toBe("public");
    expect(toolsRes.result.tools.map((t: any) => t.name)).toEqual(["a", "b"]);

    // resources/read -> private
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "r1",
      method: "resources/read",
    });
    const readRes = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "r1",
      result: { contents: [{ uri: "test://uri" }] },
    });
    expect(readRes.result.ttlMs).toBe(60000);
    expect(readRes.result.cacheScope).toBe("private");

    // tools/call -> no caching policy
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "c1",
      method: "tools/call",
    });
    const callRes = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "c1",
      result: { content: [{ type: "text", text: "hello" }] },
    });
    expect(callRes.result.resultType).toBe("complete");
    expect(callRes.result.ttlMs).toBeUndefined();
    expect(callRes.result.cacheScope).toBeUndefined();

    // Primed tools/call returning contents key still gets NO caching policy
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "c2",
      method: "tools/call",
    });
    const callResWithContents = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "c2",
      result: {
        content: [{ type: "text", text: "hello" }],
        contents: [{ uri: "test://uri" }],
      },
    });
    expect(callResWithContents.result.resultType).toBe("complete");
    expect(callResWithContents.result.ttlMs).toBeUndefined();
    expect(callResWithContents.result.cacheScope).toBeUndefined();
  });

  it("falls back to shape inference when request id was unprimed", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const unprimedList = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "unprimed-1",
      result: { tools: [{ name: "x" }] },
    });
    expect(unprimedList.result.ttlMs).toBe(3600000);
    expect(unprimedList.result.cacheScope).toBe("public");
  });

  it("does NOT overwrite explicit server-provided ttlMs or cacheScope", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "custom1",
      method: "tools/list",
    });

    const msg = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "custom1",
      result: {
        tools: [],
        ttlMs: 0,
        cacheScope: "private",
      },
    });

    expect(msg.result.ttlMs).toBe(0);
    expect(msg.result.cacheScope).toBe("private");
  });

  it("sorts tools deterministically by UTF-16 code point and handles missing names", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const inputTools = [
      { name: "Z_tool" },
      { name: "a_tool" },
      { name: "B_tool" },
      { name: undefined },
      { name: "_x" },
    ];
    // Create copy so we can verify non-mutation
    const originalInput = JSON.parse(JSON.stringify(inputTools));

    const res = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "sort1",
      result: { tools: inputTools },
    });

    const names = res.result.tools.map((t: any) => t.name);
    expect(names).toEqual([undefined, "B_tool", "Z_tool", "_x", "a_tool"]);
    // Original input array was not mutated in-place
    expect(inputTools).toEqual(originalInput);
  });

  it("maps legacy -32002 error code to -32602, leaving other error codes and payload untouched", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");

    const err2002 = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "e1",
      error: { code: -32002, message: "Resource not found", data: { uri: "x" } },
    });
    expect(err2002.error).toEqual({
      code: -32602,
      message: "Resource not found",
      data: { uri: "x" },
    });

    const err2603 = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "e2",
      error: { code: -32603, message: "Internal error" },
    });
    expect(err2603.error).toEqual({
      code: -32603,
      message: "Internal error",
    });
    expect(err2603.result).toBeUndefined();
  });

  it("removes processed request from pendingMethods map", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "p1",
      method: "tools/list",
    });
    expect(adapter.pendingRequestCount).toBe(1);

    adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "p1",
      result: { tools: [] },
    });
    expect(adapter.pendingRequestCount).toBe(0);
  });

  it("handles legacy initialize response, notifications, and primitives correctly", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");

    // Legacy initialize response preserves protocolVersion, adds resultType + serverInfo, no caching hints
    const initRes = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "init1",
      result: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        serverInfo: { name: "test", version: "1.0" },
      },
    });
    expect(initRes.result.protocolVersion).toBe("2024-11-05");
    expect(initRes.result.resultType).toBe("complete");
    expect(initRes.result._meta["io.modelcontextprotocol/serverInfo"]).toBeDefined();
    expect(initRes.result.ttlMs).toBeUndefined();

    // Notifications returned unchanged
    const notif = { jsonrpc: "2.0", method: "notifications/test", params: {} };
    expect(adapter.transformOutgoingMessage(notif)).toBe(notif);

    // Primitives returned unchanged
    expect(adapter.transformOutgoingMessage(null)).toBeNull();
    expect(adapter.transformOutgoingMessage(undefined)).toBeUndefined();
    expect(adapter.transformOutgoingMessage("string")).toBe("string");
  });
});

describe("ProtocolAdapter Phase 5: attachProtocolAdapter", () => {
  it("throws when called before server.connect (when transport.onmessage is undefined)", () => {
    const mockTransport: any = { send: vi.fn(), onmessage: undefined };
    const mockServer: any = {};
    expect(() =>
      attachProtocolAdapter(mockServer, mockTransport, "srv", "1.0"),
    ).toThrow(/server\.connect/);
  });

  it("intercepts and handles incoming messages correctly when attached after connect", async () => {
    const sentMessages: any[] = [];
    let downstreamCalls = 0;

    const mockTransport: any = {
      send: vi.fn(async (msg: any) => {
        sentMessages.push(msg);
      }),
      onmessage: (msg: any, extra?: any) => {
        downstreamCalls++;
      },
    };
    const mockServer: any = {
      server: {
        getCapabilities: () => ({ tools: {} }),
      },
    };

    attachProtocolAdapter(mockServer, mockTransport, "srv", "1.0");

    // Plain request passes downstream
    mockTransport.onmessage({
      jsonrpc: "2.0",
      id: 101,
      method: "tools/list",
    });
    expect(downstreamCalls).toBe(1);
    expect(sentMessages.length).toBe(0);

    // server/discover is intercepted, does not call downstream
    mockTransport.onmessage({
      jsonrpc: "2.0",
      id: "disc-1",
      method: "server/discover",
    });
    expect(downstreamCalls).toBe(1); // stayed 1
    expect(sentMessages.length).toBe(1);
    expect(sentMessages[0].id).toBe("disc-1");
    expect(sentMessages[0].result.resultType).toBe("complete");

    // Unsupported version is intercepted, does not call downstream
    mockTransport.onmessage({
      jsonrpc: "2.0",
      id: "bad-ver",
      method: "tools/list",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "1900-01-01",
        },
      },
    });
    expect(downstreamCalls).toBe(1); // stayed 1
    expect(sentMessages.length).toBe(2);
    expect(sentMessages[1].id).toBe("bad-ver");
    expect(sentMessages[1].error.code).toBe(-32022);
  });

  it("forwards send options and onmessage extra argument", () => {
    let capturedOptions: any = null;
    let capturedExtra: any = null;

    const mockTransport: any = {
      send: vi.fn(async (msg: any, options?: any) => {
        capturedOptions = options;
      }),
      onmessage: (msg: any, extra?: any) => {
        capturedExtra = extra;
      },
    };
    const mockServer: any = {};

    attachProtocolAdapter(mockServer, mockTransport, "srv", "1.0");

    mockTransport.send({ jsonrpc: "2.0", id: 1, result: {} }, { meta: "opt" });
    expect(capturedOptions).toEqual({ meta: "opt" });

    mockTransport.onmessage(
      { jsonrpc: "2.0", id: 2, method: "tools/list" },
      { session: "s1" },
    );
    expect(capturedExtra).toEqual({ session: "s1" });
  });

  it("rejects server/discover with unsupported _meta version", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const res = adapter.validateIncomingRequest({
      jsonrpc: "2.0",
      id: "disc-bad-ver",
      method: "server/discover",
      params: {
        _meta: {
          [META_PROTOCOL_VERSION]: "1900-01-01",
        },
      },
    });
    expect(res.action).toBe("respond");
    if (res.action === "respond") {
      expect((res.message as any).error.code).toBe(-32022);
    }
  });
});

describe("ProtocolAdapter Phase 6: JSON Schema dialect normalization", () => {
  it("strips the root $schema dialect declaration", () => {
    const out = stripJsonSchemaDialect({
      $schema: DRAFT_07_DIALECT,
      type: "object",
      properties: { markdown: { type: "string" } },
      required: ["markdown"],
    });
    expect(out).toEqual({
      type: "object",
      properties: { markdown: { type: "string" } },
      required: ["markdown"],
    });
  });

  it("strips $schema from nested subschemas at every applicator position", () => {
    const out = stripJsonSchemaDialect({
      $schema: DRAFT_07_DIALECT,
      type: "object",
      properties: {
        nested: { $schema: DRAFT_07_DIALECT, type: "string" },
      },
      $defs: {
        Ref: { $schema: DRAFT_07_DIALECT, type: "number" },
      },
      items: { $schema: DRAFT_07_DIALECT, type: "boolean" },
      anyOf: [
        { $schema: DRAFT_07_DIALECT, type: "null" },
        { type: "integer" },
      ],
      additionalProperties: { $schema: DRAFT_07_DIALECT },
    });
    expect(JSON.stringify(out)).not.toContain("$schema");
    expect(out).toEqual({
      type: "object",
      properties: { nested: { type: "string" } },
      $defs: { Ref: { type: "number" } },
      items: { type: "boolean" },
      anyOf: [{ type: "null" }, { type: "integer" }],
      additionalProperties: {},
    });
  });

  it("preserves a declared property literally named $schema", () => {
    // `properties` keys are user-chosen property names, not JSON Schema
    // keywords: a tool whose payload has a `$schema` field must keep it.
    const out: any = stripJsonSchemaDialect({
      $schema: DRAFT_07_DIALECT,
      type: "object",
      properties: {
        $schema: { type: "string", description: "dialect of the payload" },
      },
      required: ["$schema"],
    });
    expect(out.$schema).toBeUndefined();
    expect(out.properties.$schema).toEqual({
      type: "string",
      description: "dialect of the payload",
    });
    expect(out.required).toEqual(["$schema"]);
  });

  it("leaves literal keyword payloads (const, default, enum, examples) untouched", () => {
    const out: any = stripJsonSchemaDialect({
      $schema: DRAFT_07_DIALECT,
      type: "object",
      properties: {
        cfg: {
          type: "object",
          default: { $schema: DRAFT_07_DIALECT, a: 1 },
          const: { $schema: DRAFT_07_DIALECT },
          enum: [{ $schema: DRAFT_07_DIALECT }],
          examples: [{ $schema: DRAFT_07_DIALECT }],
        },
      },
    });
    expect(out.properties.cfg.default).toEqual({
      $schema: DRAFT_07_DIALECT,
      a: 1,
    });
    expect(out.properties.cfg.const).toEqual({ $schema: DRAFT_07_DIALECT });
    expect(out.properties.cfg.enum).toEqual([{ $schema: DRAFT_07_DIALECT }]);
    expect(out.properties.cfg.examples).toEqual([{ $schema: DRAFT_07_DIALECT }]);
  });

  it("does not mutate its input and passes through non-objects", () => {
    const input = { $schema: DRAFT_07_DIALECT, type: "object" };
    const snapshot = JSON.parse(JSON.stringify(input));
    stripJsonSchemaDialect(input);
    expect(input).toEqual(snapshot);

    expect(stripJsonSchemaDialect(null)).toBeNull();
    expect(stripJsonSchemaDialect(undefined)).toBeUndefined();
    expect(stripJsonSchemaDialect("x")).toBe("x");
    expect(stripJsonSchemaDialect(7)).toBe(7);
  });

  it("strips the dialect from tools[].inputSchema and tools[].outputSchema on tools/list", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const tools = [
      {
        name: "read_docx",
        inputSchema: {
          $schema: DRAFT_07_DIALECT,
          type: "object",
          properties: { file_path: { type: "string" } },
          required: ["file_path"],
        },
        outputSchema: {
          $schema: DRAFT_07_DIALECT,
          type: "object",
          properties: { markdown: { type: "string" } },
          required: ["markdown"],
          additionalProperties: {},
        },
      },
    ];
    const snapshot = JSON.parse(JSON.stringify(tools));

    const res = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "tl-1",
      result: { tools },
    });

    const tool = res.result.tools[0];
    expect(tool.inputSchema.$schema).toBeUndefined();
    expect(tool.outputSchema.$schema).toBeUndefined();
    // Everything else survives verbatim: dropping the declaration must not
    // silently drop the outputSchema (see mcp.js `if (obj)` guard).
    expect(tool.inputSchema).toEqual({
      type: "object",
      properties: { file_path: { type: "string" } },
      required: ["file_path"],
    });
    expect(tool.outputSchema).toEqual({
      type: "object",
      properties: { markdown: { type: "string" } },
      required: ["markdown"],
      additionalProperties: {},
    });
    expect(JSON.stringify(res)).not.toContain("json-schema.org/draft-07");
    // Source array untouched
    expect(tools).toEqual(snapshot);
  });

  it("tolerates tools without schemas, with null schemas, and non-object schemas", () => {
    const adapter = new ProtocolAdapter("srv", "1.0");
    const res = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "tl-2",
      result: {
        tools: [
          { name: "a" },
          { name: "b", inputSchema: null, outputSchema: undefined },
          { name: "c", inputSchema: "not-a-schema" },
          null,
        ],
      },
    });
    const [t0, t1, t2, t3] = res.result.tools;
    expect(t0).toBeNull(); // null sorts first (missing name -> "")
    // A tool that publishes no schema must not acquire one.
    expect(Object.keys(t1)).toEqual(["name"]);
    expect(t2.inputSchema).toBeNull();
    expect(t2.outputSchema).toBeUndefined();
    expect(t3.inputSchema).toBe("not-a-schema");
  });

  it("does NOT strip $schema outside tools[] schema positions", () => {
    // tools/call payloads are opaque tool output; a document that happens to
    // contain a `$schema` key must survive the adapter untouched.
    const adapter = new ProtocolAdapter("srv", "1.0");
    const res = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "tc-1",
      result: {
        content: [{ type: "text", text: "ok" }],
        structuredContent: { $schema: DRAFT_07_DIALECT, markdown: "# Hi" },
      },
    });
    expect(res.result.structuredContent).toEqual({
      $schema: DRAFT_07_DIALECT,
      markdown: "# Hi",
    });
  });

  it("strips the dialect from a tool description-adjacent _meta schema too", () => {
    // Regression guard: the adapter must reach every schema-bearing key it
    // publishes, not just the two well-known ones.
    const adapter = new ProtocolAdapter("srv", "1.0");
    const res = adapter.transformOutgoingMessage({
      jsonrpc: "2.0",
      id: "tl-3",
      result: {
        tools: [
          {
            name: "x",
            inputSchema: { $schema: DRAFT_07_DIALECT, type: "object" },
            _meta: { ui: { resourceUri: "ui://x" } },
          },
        ],
      },
    });
    expect(res.result.tools[0]._meta).toEqual({ ui: { resourceUri: "ui://x" } });
    expect(res.result.tools[0].inputSchema).toEqual({ type: "object" });
  });
});


