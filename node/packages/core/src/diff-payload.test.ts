import { describe, it, expect } from "vitest";
import {
  generate_edits_from_text,
  generate_edits_via_paragraph_alignment,
  generate_structured_edits,
} from "./diff.js";
import { RedlineEngine } from "./engine.js";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { extract_comments_data } from "./comments.js";
import { DocumentObject } from "./docx/bridge.js";
import type { ExtractStructure } from "./ingest.js";

describe("A5: compact structured-diff payload audit", () => {
  it("emitted DiffEdits contain no match_mode or regex default keys", () => {
    const orig = "The party of the first part shall deliver the goods on Monday.";
    const mod = "The supplier shall deliver the merchandise on Tuesday.";

    const editsFromText = generate_edits_from_text(orig, mod);
    const editsViaPara = generate_edits_via_paragraph_alignment(orig, mod);

    const emptyStruct: ExtractStructure = {
      part_ranges: [[0, orig.length, "body"]],
      tables: [],
    };
    const emptyStructMod: ExtractStructure = {
      part_ranges: [[0, mod.length, "body"]],
      tables: [],
    };
    const { edits: structEdits } = generate_structured_edits(
      orig,
      emptyStruct,
      mod,
      emptyStructMod,
    );

    const allEdits = [...editsFromText, ...editsViaPara, ...structEdits];
    expect(allEdits.length).toBeGreaterThan(0);

    for (const edit of allEdits) {
      const json = JSON.stringify(edit);
      expect(json.includes('"match_mode"')).toBe(false);
      expect(json.includes('"regex"')).toBe(false);
    }
  });

  it("round-trip: process_batch applies emitted edits with zero skipped edits", async () => {
    const orig = "The party of the first part shall deliver the goods on Monday.";
    const mod = "The supplier shall deliver the merchandise on Tuesday.";

    const edits = generate_edits_via_paragraph_alignment(orig, mod);
    const doc = await createTestDocument();
    addParagraph(doc, orig);

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch(edits);

    expect(res.edits_skipped).toBe(0);
    expect(res.edits_applied).toBeGreaterThan(0);

    const savedDoc = await DocumentObject.load(await doc.save());
    expect(Object.keys(extract_comments_data(savedDoc.pkg)).length).toBeGreaterThan(0);
  });

  it("size regression guard: JSON payload length for fixed fixture stays within upper bound", () => {
    const origText =
      "Section 1. Terms of Agreement.\n\nThe Seller agrees to supply 100 units of widgets to the Buyer by January 15, 2026. The price per unit shall be $50. Payment shall be made within 30 days of invoice receipt.\n\nSection 2. Termination.\n\nEither party may terminate this Agreement with 30 days written notice.";
    const modText =
      "Section 1. Terms of Supply.\n\nThe Supplier agrees to deliver 150 units of high-grade widgets to the Purchaser by February 1, 2026. The price per unit shall be $45. Payment must be completed within 15 days of invoice receipt.\n\nSection 2. Cancellation.\n\nEither party may cancel this Agreement with 60 days prior written notice.";

    const edits = generate_edits_via_paragraph_alignment(origText, modText);
    const jsonStr = JSON.stringify(edits);

    const UPPER_BOUND_WITH_HEADROOM = 1800;
    expect(jsonStr.length).toBeLessThanOrEqual(UPPER_BOUND_WITH_HEADROOM);
  });
});
