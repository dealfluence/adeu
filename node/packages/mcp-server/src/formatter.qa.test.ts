import { describe, it, expect } from "vitest";
import { formatBatchResult } from "./index.js";

describe("QA Report V2: Formatter Parity", () => {
  it("F3 & F4: Formats match_mode, occurrences_modified, heading_path, and pages correctly", () => {
    // Mock the stats object that the engine produces when `all` mode is successful
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          target_text: "the Board of Directors",
          new_text: "the Governing Body",
          warning: null,
          error: null,
          critic_markup: "the {--Board of Directors--}{++Governing Body++}",
          clean_text: "the Governing Body",
          pages: [3, 12],
          heading_path: "6. Term > 6.1",
          occurrences_modified: 33,
          match_mode: "all"
        }
      ],
      skipped_details: []
    };

    const res = formatBatchResult(stats, "dummy.docx", false);

    // Verify the new §5.4.2 visual format
    expect(res).toContain("### Edit 1 ✅ [applied] (p3, p12)");
    expect(res).toContain("**Path:** `6. Term > 6.1`");
    expect(res).toContain("**Mode:** `all` (33 occurrences modified)");
    expect(res).toContain("*Preview (CriticMarkup):*");
    expect(res).toContain("> the {--Board of Directors--}{++Governing Body++}");
    expect(res).toContain("*Preview (Clean):*");
    expect(res).toContain("> the Governing Body");
  });

  it("R1: Formats dry-run with full enrichment fields identical to real writes", () => {
    const stats = {
      actions_applied: 0,
      actions_skipped: 0,
      edits_applied: 1,
      edits_skipped: 0,
      edits: [
        {
          status: "applied",
          target_text: "Target",
          new_text: "Replaced",
          warning: null,
          error: null,
          critic_markup: "{--Target--}{++Replaced++}",
          clean_text: "Replaced",
          pages: [1],
          heading_path: "1. Intro",
          occurrences_modified: 1,
          match_mode: "all"
        }
      ],
      skipped_details: []
    };

    const res = formatBatchResult(stats, "dummy.docx", true);
    expect(res).toContain("**Path:** `1. Intro`");
    expect(res).toContain("**Mode:** `all` (1 occurrence modified)");
    expect(res).toContain("(p1)");
  });
});