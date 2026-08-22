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
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { DocumentObject, RedlineEngine } from "@adeu/core";
import { createTestDocument, addParagraph } from "../../core/src/test-utils.js";
import { startTestServer, type TestServer } from "./test-rpc.js";

const REVIEWER = "Sarah Chen";
const STANDALONE_NOTE = "Standalone reviewer note.";
const BODY = [
  "The parties shall meet and confer before moving to compel.",
  "A second clause stands alone.",
];

describe("BUG 2026-08-11 — comment destruction is opt-in at the MCP boundary", () => {
  let server: TestServer;
  let workDir: string;
  let allTools: any[] = [];
  let annotatedPath: string;

  async function buildDoc(paragraphs: string[]): Promise<Buffer> {
    const doc = await createTestDocument();
    for (const text of paragraphs) addParagraph(doc, text);
    return doc.save();
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_bug20260811_"));

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

    server = await startTestServer("bug-2026-08-11-repro");
    allTools = (await server.rpc("tools/list", {})).result.tools ?? [];
  }, 30000);

  afterAll(() => {
    server?.stop();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

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
    expect(prop).toBeDefined();
    expect(prop.type).toBe("boolean");
    expect(prop.default).toBe(true);
    expect(tool.inputSchema?.required ?? []).not.toContain("remove_comments");

    const description: string = tool.description ?? "";
    expect(description).toMatch(/remove_comments/);
    expect(description).toMatch(/default\w*\s+true/i);
    expect(description).toMatch(/remove_comments=false/i);
    expect(description.length).toBeLessThan(2048);
  });

  it("removes every comment by default, naming each one and its author", async () => {
    const out = join(workDir, "accepted_default.docx");
    const res = await server.rpc("tools/call", {
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
    expect(text).toContain(REVIEWER);
  });

  it("keeps comments when remove_comments=false is requested", async () => {
    const out = join(workDir, "accepted_annotated.docx");
    const res = await server.rpc("tools/call", {
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
  let server: TestServer;
  let workDir: string;
  let brokenParentPath: string;

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

    server = await startTestServer("bug-2026-08-11-b1");
  }, 30000);

  afterAll(() => {
    server?.stop();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  it("reports an error instead of a silent extra comment", async () => {
    const out = join(workDir, "replied.docx");
    const res = await server.rpc("tools/call", {
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
    expect(existsSync(out)).toBe(false);
  }, 20000);
});

