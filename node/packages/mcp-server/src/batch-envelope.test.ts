// FILE: node/packages/mcp-server/src/batch-envelope.test.ts
//
// B9 at the MCP boundary: a rejected batch answers with prose AND a fenced JSON
// failure envelope, so an agent can read WHICH change failed instead of
// re-deriving it from the prose. Pinned live, over stdio, because the envelope
// only matters if it survives the real tool response.
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { BATCH_RECOVERY_PROTOCOL } from "@adeu/core";
import { approxTokens } from "./conformance-utils.js";
import { startTestServer, TestServer } from "./test-rpc.js";

/** The fenced JSON block a failure response ends with. */
function envelopeOf(text: string): any {
  const m = /```json\n([\s\S]*?)\n```\s*$/.exec(text);
  expect(m, `response carries no trailing JSON envelope:\n${text}`).toBeTruthy();
  return JSON.parse(m![1]);
}

describe("process_document_batch failure envelope", () => {
  let server: TestServer;
  let docPath: string;

  beforeAll(async () => {
    server = await startTestServer("batch_envelope");
    docPath = await server.buildDoc([
      "The Supplier shall deliver the goods within thirty days.",
      "The Buyer shall pay each invoice within fourteen days.",
    ]);
  }, 30000);

  afterAll(() => server?.stop());

  it("rejects an unmatchable edit with prose plus a 0-based blame envelope", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "test the envelope",
      original_docx_path: docPath,
      author_name: "Envelope Tester",
      changes: [
        { type: "modify", target_text: "not present anywhere", new_text: "x" },
      ],
      output_path: server.tempOut("reject"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text.startsWith("Batch rejected. Some edits failed validation:")).toBe(
      true,
    );

    const env = envelopeOf(text);
    expect(env.error).toBe("batch_validation_failed");
    expect(env.failed).toHaveLength(1);
    expect(env.failed[0].index).toBe(0);
    expect(env.failed[0].reason).toContain("not present anywhere");
    expect(typeof env.message).toBe("string");
  });

  it("ends the envelope message with the batch recovery protocol", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "test the recovery protocol",
      original_docx_path: docPath,
      author_name: "Envelope Tester",
      changes: [
        { type: "modify", target_text: "also absent", new_text: "y" },
      ],
      output_path: server.tempOut("protocol"),
    });

    const env = envelopeOf(res.content[0].text);
    expect(env.message.endsWith(BATCH_RECOVERY_PROTOCOL)).toBe(true);
  });

  it("keeps a 20-edit batch with one bad edit under 500 tokens", async () => {
    const paragraphs = Array.from(
      { length: 20 },
      (_, i) => `Clause ${i + 1}: the parties agree to term number ${i + 1}.`,
    );
    const budgetDoc = await server.buildDoc(paragraphs);
    const changes: Record<string, unknown>[] = paragraphs
      .slice(0, 19)
      .map((_, i) => ({
        type: "modify",
        // The trailing period keeps "term number 1" from also matching
        // "term number 19" — an ambiguity error, not the failure under test.
        target_text: `term number ${i + 1}.`,
        new_text: `revised term number ${i + 1}.`,
      }));
    changes.push({
      type: "modify",
      target_text: "a clause this document does not contain",
      new_text: "z",
    });

    const res = await server.callTool("process_document_batch", {
      reasoning: "test the failure budget",
      original_docx_path: budgetDoc,
      author_name: "Envelope Tester",
      changes,
      output_path: server.tempOut("budget"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(envelopeOf(text).failed.map((f: any) => f.index)).toEqual([19]);
    expect(
      approxTokens(text),
      `batch failure over budget (${text.length} chars): ${text}`,
    ).toBeLessThanOrEqual(500);
  }, 30000);

  it("blames every malformed change by 0-based index", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "test the malformed-type envelope",
      original_docx_path: docPath,
      author_name: "Envelope Tester",
      changes: [
        { target_id: "Chg:1" },
        { type: "modify", target_text: "the goods", new_text: "the Goods" },
        { note: "no type here either" },
      ],
      output_path: server.tempOut("malformed"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text.startsWith("Batch rejected. Some changes are malformed:")).toBe(
      true,
    );
    expect(text).toContain("- Change 1:");
    expect(text).toContain("- Change 3:");

    const env = envelopeOf(text);
    expect(env.error).toBe("invalid_changes_file");
    expect(env.failed.map((f: any) => f.index)).toEqual([0, 2]);
    for (const f of env.failed) {
      expect(f.reason).toContain('unrecognized "type"');
    }
  });
});
