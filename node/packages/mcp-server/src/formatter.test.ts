import { describe, it, expect } from "vitest";
import { formatBatchResult } from "./index.js";

describe("MCP Server Tool Output Formatter", () => {
  it("formats a successful dry-run batch result correctly", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          target_text: "quick brown fox",
          new_text: "fast red fox",
          warning: "Warning: target_text contains punctuation.",
          error: null,
          critic_markup: "The {--quick brown fox--}{++fast red fox++} jumps over",
          clean_text: "The fast red fox jumps over",
          match_mode: "strict",
          occurrences_modified: 1,
          pages: [1],
          heading_path: "1. Intro"
        }
      ],
      skipped_details: []
    };

    const res = formatBatchResult(stats, "dummy_processed.docx", true);

    expect(res).toContain("Dry-run simulation complete.");
    expect(res).not.toContain("Batch complete. Saved to:");
    expect(res).toContain("Actions: 0 applied");
    expect(res).toContain("Edits: 1 applied");
    expect(res).toContain("Detailed Edit Reports:");
    expect(res).toContain("### Edit 1 ✅ [applied] (p1)");
    expect(res).toContain("**Path:** `1. Intro`");
    expect(res).toContain("**Mode:** `strict` (1 occurrence modified)");
    expect(res).toContain("*Warning:* Warning: target_text contains punctuation.");
    expect(res).toContain("*Preview (CriticMarkup):*\n> The {--quick brown fox--}{++fast red fox++} jumps over");
  });

  it("formats a failed batch result correctly", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 0,
      edits_skipped: 1,
      edits: [
        {
          status: "failed",
          target_text: "NON_EXISTENT",
          new_text: "fail",
          warning: null,
          error: "Target text not found in document",
          critic_markup: null,
          clean_text: null
        }
      ],
      skipped_details: ["- Failed to apply edit targeting: 'NON_EXISTENT...'"]
    };

    const res = formatBatchResult(stats, "dummy_processed.docx", false);

    expect(res).toContain("Batch complete. Saved to: dummy_processed.docx");
    expect(res).toContain("### Edit 1 ❌ [failed]");
    expect(res).toContain("*Error:* Target text not found in document");
    expect(res).toContain("Skipped Details:\n- Failed to apply edit targeting: 'NON_EXISTENT...'");
  });
});