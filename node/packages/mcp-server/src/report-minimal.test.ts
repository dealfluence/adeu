import { describe, it, expect } from "vitest";
import { formatBatchResult } from "./index.js";
import { approxTokens, projectFixture } from "./conformance-utils.js";
import { RedlineEngine } from "@adeu/core";

describe("Minimal Batch Report Rendering (Task 8)", () => {
  it("applied edit renders path, mode, occurrences, single CriticMarkup preview, and no clean preview", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          heading_path: "1. Intro",
          match_mode: "strict",
          occurrences_modified: 1,
          pages: [1],
          critic_markup: "The {--quick--}{++fast++} fox",
          clean_text: "The fast fox"
        }
      ]
    };
    const res = formatBatchResult(stats, "output.docx");
    expect(res).toContain("### Edit 1 ✅ [applied] (p1)");
    expect(res).toContain("**Path:** `1. Intro`");
    expect(res).toContain("**Mode:** `strict` (1 occurrence modified)");
    expect(res).toContain("*Preview (CriticMarkup):*\n> The {--quick--}{++fast++} fox");
    expect(res).not.toContain("*Preview (Clean):*");
    expect(res).not.toContain("The fast fox");
  });

  it("renders comment line between Mode and warning when present", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          match_mode: "strict",
          occurrences_modified: 1,
          comment: "Clarifying definition of term",
          warning: "Target contains punctuation.",
          critic_markup: "{--old--}{++new++}"
        }
      ]
    };
    const res = formatBatchResult(stats, "output.docx");
    expect(res).toContain('**Mode:** `strict` (1 occurrence modified)\n**Comment:** "Clarifying definition of term"\n*Warning:* Target contains punctuation.');
  });

  it("renders (0 occurrences modified) when occurrences_modified is 0", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          match_mode: "strict",
          occurrences_modified: 0,
          critic_markup: "{--old--}{++new++}"
        }
      ]
    };
    const res = formatBatchResult(stats, "output.docx");
    expect(res).toContain("**Mode:** `strict` (0 occurrences modified)");
  });

  it("renders failed edit with error text", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 0,
      edits_skipped: 1,
      edits: [
        {
          status: "failed",
          match_mode: "strict",
          occurrences_modified: 0,
          error: "Target text not found in document"
        }
      ]
    };
    const res = formatBatchResult(stats, "output.docx");
    expect(res).toContain("### Edit 1 ❌ [failed]");
    expect(res).toContain("*Error:* Target text not found in document");
  });

  it("renders author_impersonation_warning immediately after Saved to: line", () => {
    const stats = {
      author_impersonation_warning: "[!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.",
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: []
    };
    const res = formatBatchResult(stats, "output.docx");
    expect(res).toContain("Batch complete. Saved to: output.docx\n\n*Warning:* [!] Warning: acting author 'Jane Doe' matches an author with pending revisions in this document.\nActions: 0 applied");
  });

  // Measured on the shape the MCP tool actually emits: RAW engine stats, never
  // `shrink_batch_stats` (opt-in and JSON-only in both engines — `cli.py:1465`,
  // `serve.py:345`), with the CriticMarkup preview rendered in full to match
  // Python's renderer (`tools/document.py:813-821`: a shortened preview "is not
  // verification, and a cut through a bubble is not even valid CriticMarkup").
  //
  // Ceiling corrected from the plan's 60 to 85 (Task 8 deviation note): a
  // 200-char preview alone is 50 approx-tokens and the `> ` block label adds 7,
  // so 60 is unreachable — the floor for one edit, with the path and mode lines
  // dropped entirely, is 64. The remaining 27 tokens are the edit header, path
  // and mode lines; the exact assertion below is the tripwire that keeps that
  // chrome from growing unnoticed.
  it("token budget for 10 applied edits with 200-char previews on raw stats (measured: 84 tokens/edit)", () => {
    const preview200 = `{--${"A".repeat(97)}--}{++${"B".repeat(91)}++}`;
    expect(preview200.length).toBe(200);
    const rawStats = {
      version: "2.2.0",
      actions_applied: 10,
      actions_skipped: 0,
      edits_applied: 10,
      edits_skipped: 0,
      edits: Array.from({ length: 10 }, (_, i) => ({
        status: "applied",
        heading_path: `Section ${i + 1}`,
        match_mode: "strict",
        occurrences_modified: 1,
        pages: [i + 1],
        critic_markup: preview200,
        clean_text: "Clean text preview that should not appear"
      }))
    };

    const res = formatBatchResult(rawStats, "output.docx");
    const tokensPerEdit = Math.round(approxTokens(res) / 10);
    expect(tokensPerEdit).toBe(84);
    expect(tokensPerEdit).toBeLessThanOrEqual(85);

    expect(res).not.toContain("*Preview (Clean):*");
    expect(res).not.toContain("Clean text preview that should not appear");
  });

  it("stats.version is a non-empty string and is not '1.18.2'", async () => {
    const { doc } = await projectFixture("unicode");
    const engine = new RedlineEngine(doc, "TestAuthor");
    const stats = engine.process_batch([]);
    expect(typeof stats.version).toBe("string");
    expect(stats.version.length).toBeGreaterThan(0);
    expect(stats.version).not.toBe("1.18.2");
  });
});
