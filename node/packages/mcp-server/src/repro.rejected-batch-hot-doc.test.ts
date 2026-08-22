// FILE: node/packages/mcp-server/src/repro.rejected-batch-hot-doc.test.ts
/**
 * BUG 2026-08-12 — rejected batches accumulated their review actions in the
 * hot-DOM slot, exercised end-to-end against the REAL compiled MCP server.
 *
 * The reported run: three `process_document_batch` calls, each carrying the
 * same `reply` to the reviewer's only comment plus one `modify`. Calls 1 and 2
 * came back "Batch rejected. Some edits failed validation:"; call 3 succeeded.
 * The saved file carried THREE agent replies.
 *
 * Two defects compounded:
 *   1. the engine applied review actions BEFORE taking the batch's
 *      transactional snapshot, so the rollback could not undo them
 *      (core/src/repro.rejected-batch-action-leak.test.ts);
 *   2. this layer then called `docCache.restoreHotDoc()` on the rejection
 *      path, re-pinning that MUTATED DOM under the unchanged input file's
 *      cache key — so the retry inherited the previous attempt's reply
 *      instead of a clean parse.
 *
 * Only the pair is observable from outside, and only across CALLS: one process,
 * one file, three calls. That is what this file drives.
 *
 * Written test-first: fails on pre-fix main.
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
  statSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject, RedlineEngine } from "@adeu/core";
import { createTestDocument, addParagraph } from "../../core/src/test-utils.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

const REVIEWER = "Sarah Chen";
const AGENT = "Adeu AI (TS)";
const REVIEW_NOTE = "Please add an attorneys'-eyes-only tier.";
const REPLY = "Updated - added the AEO tier per your 28 July note.";
const ANCHOR = "reasonably necessary";
const BODY =
  "Discovery Material may be disclosed to outside counsel of record and to " +
  "any person to whom disclosure is reasonably necessary for this litigation.";
const NO_SUCH_TEXT = "TEXT THAT IS NOT ANYWHERE IN THIS DOCUMENT";

describe("BUG 2026-08-12 - a rejected batch must not leave its reply in the hot DOM", () => {
  let serverProc: ChildProcess;
  let workDir: string;
  let inputPath: string;
  let outputPath: string;
  let commentId: string;

  const pending = new Map<number, (msg: any) => void>();
  let rpcId = 9200;
  let stdoutBuffer = "";

  function rpc(method: string, params: any): Promise<any> {
    const id = ++rpcId;
    return new Promise((resolveRpc, rejectRpc) => {
      const timeout = setTimeout(
        () => rejectRpc(new Error(`RPC timeout for ${method}`)),
        20000,
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

  function batch(changes: any[]): Promise<any> {
    return rpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        reasoning: "Answer the reviewer and add the AEO tier.",
        original_docx_path: inputPath,
        author_name: AGENT,
        changes,
        partial: false,
      },
    });
  }

  /** Comment bodies of a saved package, straight from comments.xml. */
  async function commentTexts(path: string): Promise<string[]> {
    const doc = await DocumentObject.load(readFileSync(path));
    const part = doc.pkg.parts.find((p) =>
      p.contentType.endsWith("comments+xml"),
    );
    if (!part) return [];
    return Array.from(
      part._element.toString().matchAll(/<w:comment\b[^>]*>([\s\S]*?)<\/w:comment>/g),
    ).map((m) =>
      Array.from(m[1].matchAll(/<w:t[^>]*>([^<]*)<\/w:t>/g))
        .map((t) => t[1])
        .join("")
        .trim(),
    );
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_bug20260812_"));

    const doc = await createTestDocument();
    addParagraph(doc, BODY);

    // One reviewer comment, no tracked changes - the reported shape.
    new RedlineEngine(doc, REVIEWER).process_batch([
      {
        type: "modify",
        target_text: ANCHOR,
        new_text: ANCHOR,
        comment: REVIEW_NOTE,
      } as any,
    ]);
    inputPath = join(workDir, "protective_order.docx");
    outputPath = join(workDir, "protective_order_processed.docx");
    writeFileSync(inputPath, await doc.save());

    const texts = await commentTexts(inputPath);
    expect(texts, "fixture precondition: exactly one reviewer comment").toEqual([
      REVIEW_NOTE,
    ]);
    const reloaded = await DocumentObject.load(readFileSync(inputPath));
    const part = reloaded.pkg.parts.find((pp) =>
      pp.contentType.endsWith("comments+xml"),
    )!;
    commentId = part._element
      .toString()
      .match(/<w:comment\b[^>]*\bw:id="(\d+)"/)![1];

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
      clientInfo: { name: "bug-2026-08-12-repro", version: "0.0.0" },
    });
    notify("notifications/initialized", {});
  }, 30000);

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  it("two rejected retries then a success leave exactly ONE reply", async () => {
    const reply = { type: "reply", target_id: `Com:${commentId}`, text: REPLY };
    const sizeBefore = statSync(inputPath).size;

    for (const attempt of [1, 2]) {
      const res = await batch([
        { ...reply },
        { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" },
      ]);
      expect(res.result?.isError, `attempt ${attempt} must be rejected`).toBe(
        true,
      );
      expect(res.result.content[0].text).toMatch(/Batch rejected/);
      // "nothing was saved" has to be true of the INPUT too.
      expect(statSync(inputPath).size).toBe(sizeBefore);
      expect(
        existsSync(outputPath),
        `attempt ${attempt} wrote an output file for a rejected batch`,
      ).toBe(false);
    }

    const ok = await batch([
      { ...reply },
      { type: "modify", target_text: ANCHOR, new_text: "strictly necessary" },
    ]);
    expect(ok.result?.isError, ok.result?.content?.[0]?.text).toBeFalsy();
    expect(existsSync(outputPath)).toBe(true);

    const texts = await commentTexts(outputPath);
    expect(
      texts.filter((t) => t === REPLY).length,
      `the rejected retries left duplicate replies behind: ${JSON.stringify(texts)}`,
    ).toBe(1);
    expect(texts).toEqual([REVIEW_NOTE, REPLY]);
  }, 60000);

  it("a rejected batch does not leak into an unrelated later batch", async () => {
    // Same file, fresh output: the retry after a rejection carries NO reply at
    // all, so any reply in the result came from the rejected call's DOM.
    const secondInput = join(workDir, "second.docx");
    writeFileSync(secondInput, readFileSync(inputPath));
    const secondOutput = join(workDir, "second_processed.docx");

    const call = (changes: any[]) =>
      rpc("tools/call", {
        name: "process_document_batch",
        arguments: {
          reasoning: "Reply, then edit only.",
          original_docx_path: secondInput,
          author_name: AGENT,
          changes,
          partial: false,
        },
      });

    const rejected = await call([
      { type: "reply", target_id: `Com:${commentId}`, text: REPLY },
      { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" },
    ]);
    expect(rejected.result?.isError).toBe(true);

    const ok = await call([
      { type: "modify", target_text: ANCHOR, new_text: "strictly necessary" },
    ]);
    expect(ok.result?.isError, ok.result?.content?.[0]?.text).toBeFalsy();

    expect(
      await commentTexts(secondOutput),
      "the rejected batch's reply survived into an unrelated later batch",
    ).toEqual([REVIEW_NOTE]);
  }, 60000);
});
