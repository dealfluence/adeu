import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

export const SUPPORTED_PROTOCOL_VERSIONS = [
  "2026-07-28",
  "2025-11-25",
  "2024-11-05",
] as const;

export const DEFAULT_TTL_MS = 3600000; // 1 hour freshness hint for cacheable results

export class ProtocolAdapter {
  private serverName: string;
  private serverVersion: string;

  constructor(serverName: string, serverVersion: string) {
    this.serverName = serverName;
    this.serverVersion = serverVersion;
  }

  /**
   * Transforms an outgoing JSON-RPC result or error response to ensure 2026-07-28 spec compliance.
   */
  public transformOutgoingMessage(msg: any): any {
    if (!msg || typeof msg !== "object") return msg;

    // Handle error responses: map legacy -32002 (Resource Not Found) to -32602 (Invalid Params)
    if (msg.error && typeof msg.error === "object") {
      if (msg.error.code === -32002) {
        return {
          ...msg,
          error: {
            ...msg.error,
            code: -32602,
          },
        };
      }
      return msg;
    }

    // Handle result responses
    if (msg.result && typeof msg.result === "object") {
      const result = { ...msg.result };

      // Requirement: resultType must be present on all result objects (default "complete")
      if (!("resultType" in result)) {
        result.resultType = "complete";
      }

      // Requirement: _meta.io.modelcontextprotocol/serverInfo SHOULD be included in result _meta
      const meta = (result._meta && typeof result._meta === "object") ? { ...result._meta } : {};
      if (!meta["io.modelcontextprotocol/serverInfo"]) {
        meta["io.modelcontextprotocol/serverInfo"] = {
          name: this.serverName,
          version: this.serverVersion,
        };
      }
      result._meta = meta;

      // Requirement: Deterministic tool sorting and CacheableResult for tools/list
      if (Array.isArray(result.tools)) {
        result.tools = [...result.tools].sort((a: any, b: any) =>
          (a.name || "").localeCompare(b.name || "")
        );
        if (!("ttlMs" in result)) result.ttlMs = DEFAULT_TTL_MS;
        if (!("cacheScope" in result)) result.cacheScope = "private";
      }

      // Requirement: CacheableResult for resources/list, resources/read, resources/templates/list
      if (Array.isArray(result.resources) || Array.isArray(result.contents) || Array.isArray(result.resourceTemplates)) {
        if (!("ttlMs" in result)) result.ttlMs = DEFAULT_TTL_MS;
        if (!("cacheScope" in result)) result.cacheScope = "private";
      }

      return {
        ...msg,
        result,
      };
    }

    return msg;
  }

  /**
   * Validates an incoming JSON-RPC request message according to 2026-07-28 spec rules.
   * Returns an error message object if validation fails, or null if valid.
   */
  public validateIncomingRequest(msg: any): any | null {
    if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return null;

    const id = msg.id;
    const method = msg.method;
    const params = msg.params;
    const meta = params?._meta;

    // Handle server/discover RPC per SEP-2575
    if (method === "server/discover") {
      return {
        isCustomResponse: true,
        response: {
          jsonrpc: "2.0",
          id,
          result: {
            resultType: "complete",
            protocolVersions: SUPPORTED_PROTOCOL_VERSIONS,
            capabilities: {
              tools: {},
            },
            serverInfo: {
              name: this.serverName,
              version: this.serverVersion,
            },
            _meta: {
              "io.modelcontextprotocol/serverInfo": {
                name: this.serverName,
                version: this.serverVersion,
              },
            },
          },
        },
      };
    }

    // Check per-request _meta rules if _meta carries protocolVersion
    if (meta && typeof meta === "object" && "io.modelcontextprotocol/protocolVersion" in meta) {
      const version = meta["io.modelcontextprotocol/protocolVersion"];
      if (typeof version !== "string" || !(SUPPORTED_PROTOCOL_VERSIONS as readonly string[]).includes(version)) {
        return {
          jsonrpc: "2.0",
          id,
          error: {
            code: -32022, // UnsupportedProtocolVersion
            message: `Unsupported protocol version: ${version}. Supported versions: ${SUPPORTED_PROTOCOL_VERSIONS.join(", ")}`,
          },
        };
      }

      // On 2026-07-28, clientCapabilities is required in _meta
      if (version === "2026-07-28") {
        if (!("io.modelcontextprotocol/clientCapabilities" in meta) || typeof meta["io.modelcontextprotocol/clientCapabilities"] !== "object") {
          return {
            jsonrpc: "2.0",
            id,
            error: {
              code: -32602, // InvalidParams
              message: "Missing required _meta parameter: io.modelcontextprotocol/clientCapabilities",
            },
          };
        }
      }
    }

    return null;
  }
}

/**
 * Attaches the ProtocolAdapter to a Transport and McpServer instance.
 */
export function attachProtocolAdapter(
  server: McpServer,
  transport: Transport,
  serverName: string,
  serverVersion: string
): ProtocolAdapter {
  const adapter = new ProtocolAdapter(serverName, serverVersion);

  const originalSend = transport.send.bind(transport);
  transport.send = async (message: any) => {
    const transformed = adapter.transformOutgoingMessage(message);
    return originalSend(transformed);
  };

  const originalOnMessage = transport.onmessage;
  transport.onmessage = (message: any, extra?: any) => {
    const validationResult = adapter.validateIncomingRequest(message);
    if (validationResult) {
      if (validationResult.isCustomResponse) {
        transport.send(validationResult.response);
        return;
      }
      transport.send(validationResult);
      return;
    }

    if (originalOnMessage) {
      originalOnMessage(message, extra);
    }
  };

  return adapter;
}
