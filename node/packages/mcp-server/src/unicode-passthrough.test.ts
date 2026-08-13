import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { formatBatchResult } from "./index.js";
import { build_changes_response } from "./ledger.js";
import { projectFixture } from "./conformance-utils.js";
import { startTestServer, TestServer } from "./test-rpc.js";

const textOf = (r: { content: { text: string }[] }) => r.content[0].text;

describe("unicode passthrough (B6 regression guard)", () => {
  let server: TestServer;
  let docPath: string;

  beforeAll(async () => {
    server = await startTestServer("unicode_passthrough");
    docPath = await server.buildDoc([
      "The Supplier shall deliver the goods within thirty days.",
    ]);
  }, 30000);

  afterAll(() => server?.stop());

  it("renders non-ASCII characters literally in formatBatchResult", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          target_text: "sample text",
          new_text: "’ “ ” — €",
          critic_markup: "The {--sample text--}{++’ “ ” — €++} end.",
          clean_text: "The ’ “ ” — € end.",
          match_mode: "strict",
          occurrences_modified: 1,
          pages: [1],
          heading_path: "1. Intro",
        },
      ],
      skipped_details: [],
    };
    const text = formatBatchResult(stats, "output.docx");
    expect(text.includes("’ “ ” — €")).toBe(true);
    expect(!/\\u[0-9a-fA-F]{4}/.test(text)).toBe(true);
  });

  it("Task 6 failure envelope JSON block contains literal non-ASCII characters without \\u escapes", async () => {
    const res = await server.callTool("process_document_batch", {
      reasoning: "test unicode in failure envelope",
      original_docx_path: docPath,
      author_name: "Unicode Tester",
      changes: [
        {
          type: "modify",
          target_text: "Non-ASCII ’ “ ” — € target",
          new_text: "replacement",
        },
      ],
      output_path: server.tempOut("failing_unicode"),
    });

    expect(res.isError).toBe(true);
    const text: string = res.content[0].text;
    expect(text).toContain("Non-ASCII ’ “ ” — € target");

    const match = /```json\n([\s\S]*?)\n```/.exec(text);
    expect(match).toBeTruthy();
    const jsonBlock = match![1];

    expect(!/\\u[0-9a-fA-F]{4}/.test(jsonBlock)).toBe(true);

    const parsed = JSON.parse(jsonBlock);
    expect(parsed.failed[0].reason).toContain("Non-ASCII ’ “ ” — € target");
  });

  it("build_changes_response renders comment body literally using unicode.docx", async () => {
    const fx = await projectFixture("unicode");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    const text = textOf(res);

    expect(text.includes("Add a five-year tail — see §12.3.")).toBe(true);
    expect(!/\\u[0-9a-fA-F]{4}/.test(text)).toBe(true);
  });

  it("a non-ASCII author name appears literally on entry lines when filtered by author", async () => {
    const fx = await projectFixture("unicode");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
      changes_author: "Åsa",
    });
    const text = textOf(res);

    expect(text).toContain("Com:1  Åsa Öberg");
    expect(!/\\u[0-9a-fA-F]{4}/.test(text)).toBe(true);
  });
});
