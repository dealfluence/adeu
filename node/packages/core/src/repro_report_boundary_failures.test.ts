import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";

describe("Report Bug Reproductions: Boundaries and Active Insertions", () => {
  it("TC_BOUNDARY: target_text spans a paragraph boundary with body text on both sides", async () => {
    const doc = await createTestDocument();
    
    // Add two paragraphs
    addParagraph(doc, "First paragraph text.");
    addParagraph(doc, "Second paragraph text.");

    const engine = new RedlineEngine(doc);

    // Target text that spans across the paragraph boundary
    const edit = {
      type: "modify",
      target_text: "First paragraph text.\n\nSecond paragraph text.",
      new_text: "First paragraph text. New.\n\nSecond paragraph text. New.",
    } as any;

    // On the unpatched codebase, this throws a BatchValidationError ("target_text spans a paragraph boundary").
    // We assert that the batch executes successfully to replicate the failure on unpatched environments.
    const result = engine.process_batch([edit], false);
    expect(result.edits_applied).toBe(1);
    expect(result.edits_skipped).toBe(0);
  });

  it("TC_CONFLICT: Modification targets an active insertion from another author", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    // Create a paragraph with an active insertion from another author (Supplier's Counsel)
    const p = addParagraph(doc, "The party shall provide ");
    const ins = xmlDoc.createElement("w:ins");
    ins.setAttribute("w:id", "201");
    ins.setAttribute("w:author", "Supplier's Counsel");
    ins.setAttribute("w:date", "2026-06-30T08:00:00Z");

    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "written notice";
    r.appendChild(t);
    ins.appendChild(r);
    p.appendChild(ins);

    const suffixRun = xmlDoc.createElement("w:r");
    const suffixText = xmlDoc.createElement("w:t");
    suffixText.textContent = " within 30 days.";
    suffixRun.appendChild(suffixText);
    p.appendChild(suffixRun);

    // Engine is instantiated by Reviewer AI (a different author)
    const engine = new RedlineEngine(doc, "Reviewer AI");

    const edit = {
      type: "modify",
      target_text: "written notice",
      new_text: "email notification",
    } as any;

    // On the unpatched codebase, this throws a BatchValidationError ("Modification targets an active insertion from another author").
    // We assert that the batch executes successfully to replicate the failure on unpatched environments.
    const result = engine.process_batch([edit], false);
    expect(result.edits_applied).toBe(1);
    expect(result.edits_skipped).toBe(0);
  });
});
