// FILE: node/packages/mcp-server/src/test-rpc.ts
//
// The live-server harness every MCP test needs: spawn the compiled server,
// speak line-buffered JSON-RPC over stdio, build fixtures, clean up. Extracted
// so a new suite costs its assertions and nothing else (the older suites still
// carry their own copy — this is not a refactor of them).
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject } from "@adeu/core";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

const RPC_TIMEOUT_MS = 15000;

export interface TestServer {
  /** One JSON-RPC request/response round trip. Resolves with the whole message. */
  rpc(method: string, params: unknown): Promise<any>;
  /** tools/call, resolved to the tool's own result ({ content, isError? }). */
  callTool(name: string, args: Record<string, unknown>): Promise<any>;
  /** A DOCX of one paragraph per string, written to a tracked temp path. */
  buildDoc(paragraphs: string[]): Promise<string>;
  /** A tracked temp path for a tool's output_path. */
  tempOut(label: string): string;
  /** Kill the server and remove every tracked temp file. */
  stop(): void;
}

/** Boots the built server (`dist/index.js`) and completes the MCP handshake. */
export async function startTestServer(label: string): Promise<TestServer> {
  const serverPath = resolve(__dirname, "../dist/index.js");
  if (!existsSync(serverPath)) {
    throw new Error("MCP server not built. Run 'npm run build' before tests.");
  }

  const proc: ChildProcess = spawn("node", [serverPath]);
  const pending = new Map<number, (msg: any) => void>();
  const tempPaths: string[] = [];
  let rpcId = 0;
  let stdoutBuffer = "";

  proc.stdout?.on("data", (data: Buffer) => {
    stdoutBuffer += data.toString();
    let idx: number;
    while ((idx = stdoutBuffer.indexOf("\n")) !== -1) {
      const line = stdoutBuffer.slice(0, idx).trim();
      stdoutBuffer = stdoutBuffer.slice(idx + 1);
      if (!line.startsWith("{")) continue;
      try {
        const msg = JSON.parse(line);
        const cb = msg.id !== undefined ? pending.get(msg.id) : undefined;
        if (cb) {
          pending.delete(msg.id);
          cb(msg);
        }
      } catch {
        // ignore non-JSON / partial lines
      }
    }
  });

  const rpc = (method: string, params: unknown): Promise<any> => {
    const id = ++rpcId;
    return new Promise((resolveRpc, rejectRpc) => {
      const timeout = setTimeout(
        () => rejectRpc(new Error(`RPC timeout for ${method}`)),
        RPC_TIMEOUT_MS,
      );
      pending.set(id, (msg) => {
        clearTimeout(timeout);
        resolveRpc(msg);
      });
      proc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  };

  await rpc("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: label, version: "0.0.0" },
  });
  proc.stdin?.write(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
      params: {},
    }) + "\n",
  );

  const track = (p: string): string => {
    tempPaths.push(p);
    return p;
  };

  return {
    rpc,
    async callTool(name, args) {
      const msg = await rpc("tools/call", { name, arguments: args });
      if (msg.error) {
        throw new Error(
          `tools/call ${name} failed at the protocol level: ${JSON.stringify(msg.error)}`,
        );
      }
      return msg.result;
    },
    async buildDoc(paragraphs) {
      // Clone the shared empty fixture and clear its body — `@adeu/core` does
      // not export its test-utils.
      const initialPath = resolve(
        __dirname,
        "../../../../shared/fixtures/initial.docx",
      );
      const doc = await DocumentObject.load(readFileSync(initialPath));
      const body = doc.element;
      while (body.firstChild) body.removeChild(body.firstChild);
      const xmlDoc = body.ownerDocument!;
      for (const text of paragraphs) {
        const p = xmlDoc.createElement("w:p");
        const r = xmlDoc.createElement("w:r");
        const t = xmlDoc.createElement("w:t");
        t.textContent = text;
        if (/\s/.test(text)) t.setAttribute("xml:space", "preserve");
        r.appendChild(t);
        p.appendChild(r);
        body.appendChild(p);
      }
      const outPath = track(
        join(tmpdir(), `adeu_${label}_${Date.now()}_${tempPaths.length}.docx`),
      );
      writeFileSync(outPath, await doc.save());
      return outPath;
    },
    tempOut(outLabel) {
      return track(
        join(
          tmpdir(),
          `adeu_${label}_out_${outLabel}_${Date.now()}_${tempPaths.length}.docx`,
        ),
      );
    },
    stop() {
      if (!proc.killed) proc.kill();
      for (const p of tempPaths) {
        if (existsSync(p)) {
          try {
            unlinkSync(p);
          } catch {
            // best-effort cleanup
          }
        }
      }
    },
  };
}
