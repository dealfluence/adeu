import { describe, it, expect } from "vitest";
import { failure_envelope } from "@adeu/core";
import { formatBatchResult } from "./index.js";
import { build_changes_response } from "./ledger.js";
import { projectFixture } from "./conformance-utils.js";

const textOf = (r: { content: { text: string }[] }) => r.content[0].text;

describe("unicode passthrough (B6 regression guard)", () => {
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

  it("Task 6 failure envelope JSON block contains literal non-ASCII characters without \\u escapes", () => {
    const env = failure_envelope(
      "batch_validation_failed",
      [[0, "Target text '’ “ ” — €' not found"]],
      "Batch rejected. Some edits failed validation.",
      ["- Edit 1 Failed: Target text '’ “ ” — €' not found"],
    );
    const jsonStr = JSON.stringify(env);
    const rawBlock = `\`\`\`json\n${jsonStr}\n\`\`\``;

    expect(!/\\u[0-9a-fA-F]{4}/.test(rawBlock)).toBe(true);

    const match = /```json\n([\s\S]*?)\n```/.exec(rawBlock);
    expect(match).toBeTruthy();
    const parsed = JSON.parse(match![1]);

    expect(parsed.failed[0].reason).toContain("’ “ ” — €");
    expect(parsed.errors[0]).toContain("’ “ ” — €");
  });

  it("build_changes_response renders a smart-quoted comment body literally using unicode.docx", async () => {
    const fx = await projectFixture("unicode");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    const text = textOf(res);

    expect(!/\\u[0-9a-fA-F]{4}/.test(text)).toBe(true);
    // Assert that text includes smart quotes / smart-quoted comment content
    expect(text).toMatch(/[’“”—]/);
  });

  it("a non-ASCII author name survives into the ledger's Authors — roster", async () => {
    const fx = await projectFixture("unicode");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    const text = textOf(res);

    expect(text).toContain("> Authors — Åsa Öberg");
  });
});
