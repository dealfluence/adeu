import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { FUSED_JSON_HINT } from "@adeu/core";
import { startTestServer, TestServer } from "./test-rpc.js";

function envelopeOf(text: string): any {
  const m = /```json\n([\s\S]*?)\n```\s*$/.exec(text);
  expect(m, `response carries no trailing JSON envelope:\n${text}`).toBeTruthy();
  return JSON.parse(m![1]);
}

describe("fused-JSON hint on unrecognized type", () => {
  let server: TestServer;
  let docPath: string;

  beforeAll(async () => {
    server = await startTestServer("fused_json");
    docPath = await server.buildDoc([
      "The Supplier shall deliver the goods within thirty days.",
    ]);
  }, 30000);

  afterAll(() => server?.stop());

  it("produces a per-index error ending with exact FUSED_JSON_HINT sentence for fused type", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "fused JSON test 1",
      original_docx_path: docPath,
      author_name: "Fused Tester",
      changes: [
        {
          type: "modify}],{comment:",
          target_text: "goods",
          new_text: "Goods",
        },
      ],
      output_path: server.tempOut("fused1"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text).toContain(FUSED_JSON_HINT);

    const env = envelopeOf(text);
    expect(env.failed).toHaveLength(1);
    expect(env.failed[0].reason.endsWith(FUSED_JSON_HINT)).toBe(true);
  });

  it("triggers hint for all three observed fused shapes", async () => {
    const fusedShapes = [
      "modify}],{comment:",
      '{"type"',
      'accept":',
    ];

    for (const shape of fusedShapes) {
      const res = await server.callTool("process_document_batch", {
        reasoning: `fused JSON shape ${shape}`,
        original_docx_path: docPath,
        author_name: "Fused Tester",
        changes: [
          {
            type: shape,
            target_text: "goods",
            new_text: "Goods",
          },
        ],
        output_path: server.tempOut(`fused_shape_${shape.replace(/[^a-zA-Z0-9]/g, "_")}`),
      });

      expect(res.isError).toBe(true);
      const text: string = res.content[0].text;
      expect(text).toContain(FUSED_JSON_HINT);

      const env = envelopeOf(text);
      expect(env.failed[0].reason.endsWith(FUSED_JSON_HINT)).toBe(true);
    }
  });

  it("produces existing message WITHOUT hint for plain unknown type", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "plain unknown type test",
      original_docx_path: docPath,
      author_name: "Fused Tester",
      changes: [
        {
          type: "modifyy",
          target_text: "goods",
          new_text: "Goods",
        },
      ],
      output_path: server.tempOut("plain_unknown"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text).not.toContain(FUSED_JSON_HINT);

    const env = envelopeOf(text);
    expect(env.failed[0].reason).not.toContain(FUSED_JSON_HINT);
  });

  it("carries correct 0-based failed[].index for fused element", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "fused JSON index test",
      original_docx_path: docPath,
      author_name: "Fused Tester",
      changes: [
        {
          type: "modify",
          target_text: "goods",
          new_text: "Goods",
        },
        {
          type: '{"type"',
          target_text: "thirty days",
          new_text: "60 days",
        },
      ],
      output_path: server.tempOut("fused_index"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    const env = envelopeOf(text);
    expect(env.failed).toHaveLength(1);
    expect(env.failed[0].index).toBe(1);
    expect(env.failed[0].reason).toContain(FUSED_JSON_HINT);
  });

  it("does NOT advise smaller batches in hint or response", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "fused JSON no smaller batch wording test",
      original_docx_path: docPath,
      author_name: "Fused Tester",
      changes: [
        {
          type: 'accept":',
          target_id: "Chg:1",
        },
      ],
      output_path: server.tempOut("no_smaller_batch"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text).toContain(FUSED_JSON_HINT);
    expect(text.toLowerCase()).not.toContain("smaller");
    expect(text.toLowerCase()).not.toContain("fewer edits");
  });
});
