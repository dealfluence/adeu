// FILE: node/packages/mcp-server/src/repro.comment-threading-anchoring-typography.test.ts
/**
 * MCP-boundary repro tests for BUG_comment_threading_anchoring_and_typography.md
 * (reported 2026-08-11 against Adeu 2.1.0 / 56a97cf), exercised end-to-end
 * against the REAL compiled MCP server over stdio JSON-RPC.
 *
 *   B2  `accept_all_changes` ejected EVERY comment unconditionally. The library
 *       API defaults to keeping them (comments are review content, not
 *       revisions), so the MCP surface silently inverted the default: an agent
 *       asking to "accept all changes" also got "delete the human reviewer's
 *       comments", with no parameter to opt out. Comments whose anchor an
 *       accepted deletion consumes still go — Word does the same — but that
 *       removal must be disclosed WITH its author.
 *
 *   B1  A `reply` whose parent cannot be threaded must fail loudly instead of
 *       silently becoming a new top-level comment: the agent in the reported
 *       run consumed the false success, retried, and made the document worse.
 *       (The engine-level repros live in
 *       core/src/repro.comment-threading-anchoring-typography.test.ts; here we
 *       pin that the failure reaches an MCP caller as an error.)
 *
 * Written test-first: both fail on pre-fix main.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject, RedlineEngine } from "@adeu/core";
import { createTestDocument, addParagraph } from "../../core/src/test-utils.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

const REVIEWER = "Sarah Chen";
const STANDALONE_NOTE = "Standalone reviewer note.";
const BODY = [
  "The parties shall meet and confer before moving to compel.",
  "A second clause stands alone.",
];

describe("BUG 2026-08-11 — comment destruction is opt-in at the MCP boundary", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let allTools: any[] = [];
  let annotatedPath: string;

  const pending = new Map<number, (msg: any) => void>();
  let rpcId = 8100;
  let stdoutBuffer = "";

  function rpc(method: string, params: any): Promise<any> {
    const id = ++rpcId;
    return new Promise((resolveRpc, rejectRpc) => {
      const timeout = setTimeout(
        () => rejectRpc(new Error(`RPC timeout for ${method}`)),
        15000,
      );
      pending.set(id, (msg) => {
        clearTimeout(timeout);
        resolveRpc(msg);
      });
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  function notify(method: string, params: any): void {
    serverProc.stdin?.write(
      JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n",
    );
  }

  async function buildDoc(paragraphs: string[]): Promise<Buffer> {
    const doc = await createTestDocument();
    for (const text of paragraphs) addParagraph(doc, text);
    return doc.save();
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_bug20260811_"));

    // A reviewer's comment on text the agent never touches, plus a tracked
    // change elsewhere — the reported run's shape.
    const plain = await buildDoc(BODY);
    const reviewed = await DocumentObject.load(plain);
    new RedlineEngine(reviewed, REVIEWER).process_batch([
      {
        type: "modify",
        target_text: "A second clause",
        new_text: "A second clause",
        comment: STANDALONE_NOTE,
      } as any,
    ]);
    const withComment = await reviewed.save();

    const edited = await DocumentObject.load(withComment);
    new RedlineEngine(edited, "Agent").process_batch([
      {
        type: "modify",
        target_text: "meet and confer",
        new_text: "confer in good faith",
      } as any,
    ]);
    annotatedPath = join(workDir, "protective_order.docx");
    writeFileSync(annotatedPath, await edited.save());

    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error("MCP server not built. Run 'npm run build' before tests.");
    }
    serverProc = spawn("node", [serverPath]);
    serverProc.stdout?.on("data", (data: Buffer) => {
      stdoutBuffer += data.toString();
      let idx: number;
      while ((idx = stdoutBuffer.indexOf("\n")) !== -1) {
        const line = stdoutBuffer.slice(0, idx).trim();
        stdoutBuffer = stdoutBuffer.slice(idx + 1);
        if (!line.startsWith("{")) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            const cb = pending.get(msg.id)!;
            pending.delete(msg.id);
            cb(msg);
          }
        } catch {
          /* ignore non-JSON / partial lines */
        }
      }
    });

    await rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "bug-2026-08-11-repro", version: "0.0.0" },
    });
    notify("notifications/initialized", {});
    allTools = (await rpc("tools/list", {})).result.tools ?? [];
  }, 30000);

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  /**
   * Comment body texts straight from the saved package. Deliberately raw: the
   * published @adeu/core entrypoint does not re-export extract_comments_data,
   * and this is a black-box assertion about a file on disk anyway.
   */
  async function commentTexts(path: string): Promise<string[]> {
    const doc = await DocumentObject.load(readFileSync(path));
    const part = doc.pkg.parts.find((p) =>
      p.contentType.endsWith("comments+xml"),
    );
    if (!part) return [];
    return Array.from(
      part._element.toString().matchAll(/<w:t[^>]*>([^<]*)<\/w:t>/g),
    ).map((m) => m[1]);
  }

  it("publishes remove_comments as an explicit boolean defaulting to true", () => {
    const tool = allTools.find((t) => t.name === "accept_all_changes");
    expect(tool, "accept_all_changes must be advertised").toBeDefined();

    const prop = tool.inputSchema?.properties?.remove_comments;
    expect(
      prop,
      "accept_all_changes must expose comment removal as a caller CHOICE rather " +
        "than hard-coding it — the inversion B2 reported was that it could not " +
        "be opted out of",
    ).toBeDefined();
    // Real MCP clients strip property-level anyOf/oneOf to {} (AI_CONTEXT §7a),
    // so this must publish exactly ONE JSON type.
    expect(prop.type).toBe("boolean");
    expect(prop.default).toBe(true);
    expect(tool.inputSchema?.required ?? []).not.toContain("remove_comments");

    // §7a again: optional-property descriptions are dropped in transit and the
    // description truncates at ~2048 chars, so the operative guidance — the
    // destructive DEFAULT and how to opt out — has to fit in the description.
    const description: string = tool.description ?? "";
    expect(description).toMatch(/remove_comments/);
    expect(
      description,
      "the description must state that comment removal is the DEFAULT:\n" + description,
    ).toMatch(/default\w*\s+true/i);
    expect(
      description,
      "the description must tell the caller how to opt out:\n" + description,
    ).toMatch(/remove_comments=false/i);
    expect(description.length).toBeLessThan(2048);
  });

  it("removes every comment by default, naming each one and its author", async () => {
    // The tool produces a DISTRIBUTABLE clean document, so removal stays the
    // default (QA_ISSUES_DISCOVERED #10 logged the opposite as a
    // confidentiality risk). What changed is that it is no longer silent.
    const out = join(workDir, "accepted_default.docx");
    const res = await rpc("tools/call", {
      name: "accept_all_changes",
      arguments: {
        reasoning: "test",
        docx_path: annotatedPath,
        output_path: out,
      },
    });
    const text: string = res.result.content[0].text;

    expect(await commentTexts(out), `tool said: ${text}`).not.toContain(
      STANDALONE_NOTE,
    );
    expect(text).toMatch(/Comments removed: [1-9]/);
    expect(
      text,
      "the response must name whose review content it destroyed:\n" + text,
    ).toContain(REVIEWER);
  });

  it("keeps comments when remove_comments=false is requested", async () => {
    const out = join(workDir, "accepted_annotated.docx");
    const res = await rpc("tools/call", {
      name: "accept_all_changes",
      arguments: {
        reasoning: "test",
        docx_path: annotatedPath,
        output_path: out,
        remove_comments: false,
      },
    });
    const text: string = res.result.content[0].text;

    expect(await commentTexts(out), `tool said: ${text}`).toContain(
      STANDALONE_NOTE,
    );
    expect(text).toContain("Accepted all changes");
  });
});

describe("BUG 2026-08-11 — an unthreadable reply is an error, not a stray comment", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let brokenParentPath: string;

  const pending = new Map<number, (msg: any) => void>();
  let rpcId = 8200;
  let stdoutBuffer = "";

  function rpc(method: string, params: any): Promise<any> {
    const id = ++rpcId;
    return new Promise((resolveRpc, rejectRpc) => {
      const timeout = setTimeout(
        () => rejectRpc(new Error(`RPC timeout for ${method}`)),
        15000,
      );
      pending.set(id, (msg) => {
        clearTimeout(timeout);
        resolveRpc(msg);
      });
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  function notify(method: string, params: any): void {
    serverProc.stdin?.write(
      JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n",
    );
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_bug20260811_b1_"));

    const doc = await createTestDocument();
    addParagraph(doc, "The receiving party shall bear the cost of production.");

    new RedlineEngine(doc, REVIEWER).process_batch([
      {
        type: "modify",
        target_text: "bear the cost",
        new_text: "bear the cost",
        comment: "Whose cost is this really?",
      } as any,
    ]);

    // Empty every comment body: `EG_BlockLevelElts` is minOccurs="0", so this is
    // schema-legal and it is the one shape where a paragraph identity genuinely
    // cannot be minted — i.e. threading is truly impossible.
    const reloaded = await DocumentObject.load(await doc.save());
    const commentsPart = reloaded.pkg.parts.find((pt) =>
      pt.contentType.endsWith("comments+xml"),
    )!;
    const stack: any[] = [commentsPart._element];
    while (stack.length) {
      const el = stack.pop();
      for (const child of Array.from(el.childNodes ?? [])) {
        const node = child as any;
        if (node.nodeType === 1 && node.tagName === "w:comment") {
          for (const grand of Array.from(node.childNodes)) {
            node.removeChild(grand as any);
          }
        } else if (node.nodeType === 1) {
          stack.push(node);
        }
      }
    }
    brokenParentPath = join(workDir, "unthreadable.docx");
    writeFileSync(brokenParentPath, await reloaded.save());

    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error("MCP server not built. Run 'npm run build' before tests.");
    }
    serverProc = spawn("node", [serverPath]);
    serverProc.stdout?.on("data", (data: Buffer) => {
      stdoutBuffer += data.toString();
      let idx: number;
      while ((idx = stdoutBuffer.indexOf("\n")) !== -1) {
        const line = stdoutBuffer.slice(0, idx).trim();
        stdoutBuffer = stdoutBuffer.slice(idx + 1);
        if (!line.startsWith("{")) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            const cb = pending.get(msg.id)!;
            pending.delete(msg.id);
            cb(msg);
          }
        } catch {
          /* ignore */
        }
      }
    });

    await rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "bug-2026-08-11-b1", version: "0.0.0" },
    });
    notify("notifications/initialized", {});
  }, 30000);

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  it("reports an error instead of a silent extra comment", async () => {
    const out = join(workDir, "replied.docx");
    const res = await rpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        reasoning: "test",
        original_docx_path: brokenParentPath,
        output_path: out,
        author_name: "Agent",
        changes: [{ type: "reply", target_id: "Com:1", text: "Addressed." }],
      },
    });

    const text: string = res.result.content?.[0]?.text ?? "";
    expect(
      res.result.isError,
      `a reply that cannot be threaded must not report success: ${text}`,
    ).toBe(true);
    expect(text.toLowerCase()).toContain("thread");
    // And nothing was written: the caller's file must not exist.
    expect(existsSync(out)).toBe(false);
  }, 20000);
});

