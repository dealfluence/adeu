import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

/**
 * Dual-era MCP Protocol Adapter for revision 2026-07-28.
 *
 * Modern clients supply `io.modelcontextprotocol/protocolVersion: "2026-07-28"` in `_meta`
 * on every request and are validated per the 2026-07-28 specification.
 * Legacy clients that initiate connections via the `initialize` handshake and omit
 * `_meta.protocolVersion` on subsequent requests are served as legacy (spec §Backward Compatibility).
 */

export const SUPPORTED_PROTOCOL_VERSIONS = [
  "2026-07-28",
  "2025-11-25",
  "2024-11-05",
] as const;

export const MODERN_PROTOCOL_VERSION = "2026-07-28";

export const META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion";
export const META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities";
export const META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo";
export const META_LOG_LEVEL = "io.modelcontextprotocol/logLevel";
export const META_SERVER_INFO = "io.modelcontextprotocol/serverInfo";

/**
 * Spec 2026-07-28 §Error Codes.
 * -32020 is HTTP-only (stdio has no headers) and is never emitted here.
 * -32021 is reserved for missing client capabilities required by the server.
 */
export const MCP_ERROR_CODES = {
  INVALID_PARAMS: -32602,
  HEADER_MISMATCH: -32020,
  MISSING_REQUIRED_CLIENT_CAPABILITY: -32021,
  UNSUPPORTED_PROTOCOL_VERSION: -32022,
  LEGACY_RESOURCE_NOT_FOUND: -32002,
} as const;

export const LOG_LEVELS = [
  "debug",
  "info",
  "notice",
  "warning",
  "error",
  "critical",
  "alert",
  "emergency",
] as const;

export const DEFAULT_TTL_MS = 3600000; // 1 hour freshness hint for cacheable results

export type CacheScope = "public" | "private";

export const CACHE_POLICY: Record<
  string,
  { ttlMs: number; cacheScope: CacheScope }
> = {
  // Static lists identical for every caller -> "public" (spec §Choosing a Cache Scope)
  "server/discover": { ttlMs: DEFAULT_TTL_MS, cacheScope: "public" },
  "tools/list": { ttlMs: DEFAULT_TTL_MS, cacheScope: "public" },
  "prompts/list": { ttlMs: DEFAULT_TTL_MS, cacheScope: "public" },
  "resources/list": { ttlMs: DEFAULT_TTL_MS, cacheScope: "public" },
  "resources/templates/list": { ttlMs: DEFAULT_TTL_MS, cacheScope: "public" },
  // Reads may become document-derived; stay conservative
  "resources/read": { ttlMs: 60000, cacheScope: "private" },
};

/**
 * Mirrors @modelcontextprotocol/ext-apps EXTENSION_ID + RESOURCE_MIME_TYPE.
 * Kept as a literal constant to avoid extra transport dependencies in the adapter.
 */
export const UI_EXTENSION_CAPABILITY = {
  "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] },
} as const;

/**
 * Inspects live server capabilities safely using internal server instance or falls back to defaults.
 */
export function readServerCapabilities(
  server: unknown,
): Record<string, unknown> {
  let caps: Record<string, unknown> | null = null;
  try {
    const rawCaps = (server as any)?.server?.getCapabilities?.();
    if (rawCaps && typeof rawCaps === "object" && !Array.isArray(rawCaps)) {
      caps = rawCaps;
    }
  } catch {
    caps = null;
  }

  if (!caps) {
    caps = {
      tools: { listChanged: true },
      resources: { listChanged: true },
    };
  }

  const existingExt =
    caps.extensions &&
    typeof caps.extensions === "object" &&
    !Array.isArray(caps.extensions)
      ? caps.extensions
      : {};

  return structuredClone({
    ...caps,
    extensions: {
      ...existingExt,
      ...UI_EXTENSION_CAPABILITY,
    },
  });
}

export type AdapterDecision =
  | { action: "pass" }
  | { action: "respond"; message: Record<string, unknown> };

export interface ProtocolAdapterOptions {
  /** Returns the live server capabilities; defaults to a static tools+resources set. */
  getCapabilities?: () => Record<string, unknown>;
}

/**
 * Locale-independent: localeCompare varies by ICU locale, which would make the
 * order non-deterministic across machines and defeat SEP-2243 caching.
 */
export function sortToolsDeterministically<T extends { name?: string }>(
  tools: T[],
): T[] {
  return [...tools].sort((a, b) => {
    const x = typeof a?.name === "string" ? a.name : "";
    const y = typeof b?.name === "string" ? b.name : "";
    return x < y ? -1 : x > y ? 1 : 0;
  });
}

/** Fallback when the originating request was not observed (adapter attached mid-flight). */
export function inferCachePolicy(
  result: any,
): { ttlMs: number; cacheScope: CacheScope } | undefined {
  if (Array.isArray(result.tools)) return CACHE_POLICY["tools/list"];
  if (Array.isArray(result.resourceTemplates))
    return CACHE_POLICY["resources/templates/list"];
  if (Array.isArray(result.resources)) return CACHE_POLICY["resources/list"];
  if (Array.isArray(result.prompts)) return CACHE_POLICY["prompts/list"];
  if (Array.isArray(result.contents)) return CACHE_POLICY["resources/read"];
  return undefined;
}

export class ProtocolAdapter {
  private serverName: string;
  private serverVersion: string;
  private options: ProtocolAdapterOptions;
  public readonly pendingMethods = new Map<string | number, string>();

  constructor(
    serverName: string,
    serverVersion: string,
    options: ProtocolAdapterOptions = {},
  ) {
    this.serverName = serverName;
    this.serverVersion = serverVersion;
    this.options = options;
  }

  public get pendingRequestCount(): number {
    return this.pendingMethods.size;
  }

  private errorResponse(
    id: string | number,
    code: number,
    message: string,
    data?: unknown,
  ): AdapterDecision {
    return {
      action: "respond",
      message: {
        jsonrpc: "2.0",
        id,
        error: {
          code,
          message,
          ...(data !== undefined ? { data } : {}),
        },
      },
    };
  }

  /**
   * Transforms an outgoing JSON-RPC result or error response to ensure 2026-07-28 spec compliance.
   */
  public transformOutgoingMessage(msg: any): any {
    if (!msg || typeof msg !== "object") return msg;

    if (msg.error && typeof msg.error === "object") {
      if (msg.id !== undefined && msg.id !== null) {
        this.pendingMethods.delete(msg.id);
      }
      if (msg.error.code === MCP_ERROR_CODES.LEGACY_RESOURCE_NOT_FOUND) {
        return {
          ...msg,
          error: {
            ...msg.error,
            code: MCP_ERROR_CODES.INVALID_PARAMS,
          },
        };
      }
      return msg;
    }

    if (!msg.result || typeof msg.result !== "object" || Array.isArray(msg.result)) {
      return msg;
    }

    const method =
      msg.id !== undefined && msg.id !== null
        ? this.pendingMethods.get(msg.id)
        : undefined;
    if (msg.id !== undefined && msg.id !== null) {
      this.pendingMethods.delete(msg.id);
    }

    const result = { ...msg.result };

    if (!("resultType" in result)) {
      result.resultType = "complete";
    }

    const meta =
      result._meta && typeof result._meta === "object" && !Array.isArray(result._meta)
        ? { ...result._meta }
        : {};
    if (!meta[META_SERVER_INFO]) {
      meta[META_SERVER_INFO] = {
        name: this.serverName,
        version: this.serverVersion,
      };
    }
    result._meta = meta;

    if (Array.isArray(result.tools)) {
      result.tools = sortToolsDeterministically(result.tools);
    }

    if (result.resultType === "complete") {
      const policy = method ? CACHE_POLICY[method] : inferCachePolicy(result);
      if (policy) {
        if (!("ttlMs" in result)) result.ttlMs = policy.ttlMs;
        if (!("cacheScope" in result)) result.cacheScope = policy.cacheScope;
      }
    }

    return {
      ...msg,
      result,
    };
  }

  /**
   * Validates an incoming JSON-RPC request message according to 2026-07-28 spec rules.
   * Returns AdapterDecision indicating whether to pass or respond.
   */
  public validateIncomingRequest(msg: any): AdapterDecision {
    if (
      !msg ||
      typeof msg !== "object" ||
      Array.isArray(msg) ||
      msg.jsonrpc !== "2.0" ||
      typeof msg.method !== "string"
    ) {
      return { action: "pass" };
    }

    const isRequest = msg.id !== undefined && msg.id !== null;
    if (!isRequest) {
      // Never respond to notifications or id-less messages
      return { action: "pass" };
    }

    const id = msg.id;
    const method = msg.method;
    const params = msg.params;
    const meta =
      params?._meta && typeof params._meta === "object" && !Array.isArray(params._meta)
        ? params._meta
        : undefined;

    if (meta && META_PROTOCOL_VERSION in meta) {
      const version = meta[META_PROTOCOL_VERSION];
      if (
        typeof version !== "string" ||
        !(SUPPORTED_PROTOCOL_VERSIONS as readonly string[]).includes(version)
      ) {
        return this.errorResponse(
          id,
          MCP_ERROR_CODES.UNSUPPORTED_PROTOCOL_VERSION,
          `Unsupported protocol version: ${version}. Supported versions: ${SUPPORTED_PROTOCOL_VERSIONS.join(", ")}`,
          {
            requested: version,
            supported: [...SUPPORTED_PROTOCOL_VERSIONS],
          },
        );
      }

      if (version === MODERN_PROTOCOL_VERSION) {
        const clientCaps = meta[META_CLIENT_CAPABILITIES];
        if (
          !(META_CLIENT_CAPABILITIES in meta) ||
          !clientCaps ||
          typeof clientCaps !== "object" ||
          Array.isArray(clientCaps)
        ) {
          return this.errorResponse(
            id,
            MCP_ERROR_CODES.INVALID_PARAMS,
            "Missing required _meta parameter: io.modelcontextprotocol/clientCapabilities",
          );
        }
      }

      if (META_LOG_LEVEL in meta) {
        const logLevel = meta[META_LOG_LEVEL];
        if (
          typeof logLevel !== "string" ||
          !(LOG_LEVELS as readonly string[]).includes(logLevel)
        ) {
          return this.errorResponse(
            id,
            MCP_ERROR_CODES.INVALID_PARAMS,
            `Invalid log level: ${logLevel}. Supported: ${LOG_LEVELS.join(", ")}`,
          );
        }
      }
    }

    if (method === "server/discover") {
      return {
        action: "respond",
        message: this.buildDiscoverResponse(id),
      };
    }

    this.pendingMethods.set(id, method);
    while (this.pendingMethods.size > 512) {
      const firstKey = this.pendingMethods.keys().next().value;
      if (firstKey !== undefined) {
        this.pendingMethods.delete(firstKey);
      }
    }

    return { action: "pass" };
  }

  public buildDiscoverResponse(id: string | number): Record<string, unknown> {
    const caps =
      this.options.getCapabilities?.() ?? readServerCapabilities(null);

    return {
      jsonrpc: "2.0",
      id,
      result: {
        resultType: "complete",
        supportedVersions: [...SUPPORTED_PROTOCOL_VERSIONS],
        protocolVersions: [...SUPPORTED_PROTOCOL_VERSIONS],
        capabilities: caps,
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
        ttlMs: DEFAULT_TTL_MS,
        cacheScope: "public",
      },
    };
  }
}

/**
 * Attaches the ProtocolAdapter to a Transport and McpServer instance.
 * MUST be called AFTER server.connect(transport).
 */
export function attachProtocolAdapter(
  server: McpServer,
  transport: Transport,
  serverName: string,
  serverVersion: string,
): ProtocolAdapter {
  const downstream = transport.onmessage;
  if (typeof downstream !== "function") {
    throw new Error(
      "attachProtocolAdapter must be called AFTER server.connect(transport): the MCP SDK " +
        "chains onmessage handlers, so a pre-connect wrapper cannot suppress SDK handling " +
        "and every intercepted request would be answered twice.",
    );
  }

  const adapter = new ProtocolAdapter(serverName, serverVersion, {
    getCapabilities: () => readServerCapabilities(server),
  });

  const originalSend = transport.send.bind(transport);
  transport.send = async (message: any, options?: any) => {
    const transformed = adapter.transformOutgoingMessage(message);
    return originalSend(transformed, options);
  };

  transport.onmessage = (message: any, extra?: any) => {
    const decision = adapter.validateIncomingRequest(message);
    if (decision.action === "respond") {
      transport.send(decision.message as any).catch((err) => {
        transport.onerror?.(
          err instanceof Error ? err : new Error(String(err)),
        );
      });
      return;
    }
    downstream(message, extra);
  };

  return adapter;
}
