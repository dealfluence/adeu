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
    expect(report.critic_markup).toContain("{--quick brown--}{++fast red++} fox");
    expect(report.critic_markup).toContain("The ");
    expect(report.critic_markup).toContain(" jumps over");

    expect(report.clean_text).toContain("The fast red fox jumps over");
    expect(stats.engine).toBe("node");
    expect(stats.version).toBeDefined();
  });

  it("punctuation anchor: no warning on clean apply", async () => {
    // A punctuated anchor that matches and applies cleanly must NOT raise the
    // tokenization-splitting warning. The redline preview already reports the
    // change; a warning here is a false positive that pushes agents into
    // needless "cleaner anchor" retries.
    const doc = await createTestDocument();
    addParagraph(doc, "Refer to sample_term_name in Section 4.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    const stats = (engine as any).process_batch([
      { type: "modify", target_text: "sample_term_name", new_text: "validated_term_name" }
    ]);

    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    expect(report.warning).toBeNull();

    // Same expectation under dry_run.
    const doc2 = await createTestDocument();
    addParagraph(doc2, "Refer to sample_term_name in Section 4.");
    const engine2 = new RedlineEngine(doc2, "Reviewer TS");
    const dryStats = (engine2 as any).process_batch([
      { type: "modify", target_text: "sample_term_name", new_text: "validated_term_name" }
    ], true);
    const dryReport = dryStats.edits[0];
    expect(dryReport.status).toBe("applied");
    expect(dryReport.warning).toBeNull();
  });

  it("punctuation anchor: warns only when match fails", async () => {
    // When a punctuated anchor fails to match, the warning IS surfaced as
    // recovery context (the punctuation may be why the match missed).
    const doc = await createTestDocument();
    addParagraph(doc, "Refer to sample_term_name in Section 4.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    const stats = (engine as any).process_batch([
      { type: "modify", target_text: "phantom_term-x", new_text: "anything" }
    ], true);

    const report = stats.edits[0];
    expect(report.status).toBe("failed");
    expect(report.warning).not.toBeNull();
    expect(report.warning.toLowerCase()).toContain("punctuation");
    expect(report.warning).toContain("phantom_term-x");
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

  it("preview self-consistency on underscore terms", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "ANCHOR_LINE governs the interpretation of this Agreement.");
    const engine = new RedlineEngine(doc, "Reviewer TS");

    const stats = (engine as any).process_batch([
      {
        type: "modify",
        target_text: "ANCHOR_LINE governs the interpretation of this Agreement.",
        new_text: "NEW_PARA inserted before.\n\nANCHOR_LINE governs the interpretation of this Agreement.",
      }
    ]);

    const buf = await doc.save();
    const cleanDocText = await extractTextFromBuffer(buf, true);

    const report = stats.edits[0];

    expect(report.clean_text).not.toBeNull();
    const cleanPreview = report.clean_text.replace(/^\.+|\.+$/g, "");
    expect(cleanDocText).toContain(cleanPreview);
  });

  it("preview does not contain duplicate garbling", async () => {
     const doc = await createTestDocument();
     
     addParagraph(doc, "Payment Terms");
 
     const xmlDoc = doc.element.ownerDocument!;
     const p2 = xmlDoc.createElement("w:p");
     const del = xmlDoc.createElement("w:del");
     del.setAttribute("w:id", "900");
     del.setAttribute("w:author", "Reviewer");
     del.setAttribute("w:date", "2026-06-01T00:00:00Z");
     const r = xmlDoc.createElement("w:r");
     const t = xmlDoc.createElement("w:delText");
     t.setAttribute("xml:space", "preserve");
     t.textContent = "DUP_PHRASE shall be paid within thirty days of invoice.";
     r.appendChild(t);
     del.appendChild(r);
     p2.appendChild(del);
    const firstP = doc.element.getElementsByTagName("w:p")[0];
    firstP.parentNode!.appendChild(p2);
 
     addParagraph(doc, "DUP_PHRASE shall be paid within thirty days of invoice.");
     addParagraph(doc, "Late payments accrue interest at the statutory rate.");
 
     const engine = new RedlineEngine(doc, "Reviewer TS");
     const stats = (engine as any).process_batch([
       {
         type: "modify",
         target_text: "DUP_PHRASE shall be paid within thirty days of invoice.",
         new_text: "DUP_PHRASE shall be paid within sixty days of invoice.",
       }
     ]);

    const buf = await doc.save();
    const cleanDocText = await extractTextFromBuffer(buf, true);
    
    const report = stats.edits[0];
    expect(report.clean_text).not.toBeNull();
    const cleanPreview = report.clean_text.replace(/^\.+|\.+$/g, "");
    expect(cleanDocText).toContain(cleanPreview);
   });
 });