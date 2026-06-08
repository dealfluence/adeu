import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";

describe("Feedback Layer & Dry Run Verification", () => {
  it("process_batch returns detailed edit reports", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumps over the lazy dog.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    const stats = (engine as any).process_batch([
      { type: "modify", target_text: "quick brown fox", new_text: "fast red fox" }
    ]);

    expect(stats.edits).toBeDefined();
    expect(stats.edits.length).toBe(1);

    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    expect(report.target_text).toBe("quick brown fox");
    expect(report.new_text).toBe("fast red fox");

    // Previews with context window
    expect(report.critic_markup).toContain("{--quick brown fox--}{++fast red fox++}");
    expect(report.critic_markup).toContain("The ");
    expect(report.critic_markup).toContain(" jumps over");

    expect(report.clean_text).toContain("The fast red fox jumps over");
    expect(stats.engine).toBe("node");
    expect(stats.version).toBeDefined();
  });

  it("punctuation anchor triggers warning", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Refer to sample_term_name in Section 4.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    const stats = (engine as any).process_batch([
      { type: "modify", target_text: "sample_term_name", new_text: "validated_term_name" }
    ]);

    const report = stats.edits[0];
    expect(report.warning).not.toBeNull();
    expect(report.warning.toLowerCase()).toContain("punctuation");
    expect(report.warning).toContain("sample_term_name");
  });

  it("dry_run does not mutate and reports safely", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Baseline text.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    // 1. Valid Dry Run
    const stats = (engine as any).process_batch([
      { type: "modify", target_text: "Baseline", new_text: "Modified Preview" }
    ], true);

    expect(stats.edits_applied).toBe(1);
    expect(stats.edits[0].status).toBe("applied");
    expect(stats.edits[0].clean_text).toContain("Modified Preview");

    // Verify original document remains pristine
    const buf = await doc.save();
    const cleanText = await extractTextFromBuffer(buf, true);
    expect(cleanText).not.toContain("Modified Preview");
    expect(cleanText).toContain("Baseline text");

    // 2. Invalid Dry Run should not throw and instead report the failure safely
    const statsInvalid = (engine as any).process_batch([
      { type: "modify", target_text: "NON_EXISTENT", new_text: "fail" }
    ], true);

    expect(statsInvalid.edits_skipped).toBe(1);
    expect(statsInvalid.edits[0].status).toBe("failed");
    expect(statsInvalid.edits[0].error).not.toBeNull();
    expect(statsInvalid.edits[0].error.toLowerCase()).toContain("not found");
  });
});