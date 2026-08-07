// Sequential batch semantics — cross-engine parity with the Python engine
// (QA 2026-07-17 follow-up). Batches apply SEQUENTIALLY: each edit is
// validated and applied against the document state produced by the edits
// before it (chaining), and validation failures reject the batch
// transactionally.

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";

function chainedBatch(): any[] {
  return [
    {
      type: "modify",
      target_text: "the Recipient",
      new_text: "Receiving Party",
    },
    {
      type: "modify",
      target_text: "Receiving Party",
      new_text: "Disclosee",
    },
  ];
}

async function ndaDoc() {
  const doc = await createTestDocument();
  addParagraph(
    doc,
    "As defined in Section 1, the Recipient shall maintain confidentiality of all materials.",
  );
  return doc;
}

describe("Sequential batch semantics (Python parity)", () => {
  it("chained batch applies with correct stats", async () => {
    const engine = new RedlineEngine(await ndaDoc());

    const resWet = engine.process_batch(chainedBatch());
    expect(resWet.edits_applied).toBe(2);
    expect(resWet.edits_skipped).toBe(0);
    expect(resWet.edits.every((r: any) => r.status === "applied")).toBe(true);

    const xml = engine.doc.element.toString();
    expect(xml).toContain("Disclosee");
  });

  it("rejects a batch transactionally and leaves the document untouched", async () => {
    const engine = new RedlineEngine(await ndaDoc());

    let caught: any = null;
    try {
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "the Recipient",
            new_text: "Receiving Party",
          },
          {
            type: "modify",
            target_text: "Nonexistent text 123",
            new_text: "x",
          },
        ] as any[],
      );
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(caught.message).toContain("Edit 2 Failed");
    // Rollback: edit 1's tracked change must not survive the rejection.
    const xml = engine.doc.element.toString();
    expect(xml).not.toContain("Receiving Party");
  });

  it("validation errors after applied edits carry the sequential-contract hint", async () => {
    const engine = new RedlineEngine(await ndaDoc());

    let caught: any = null;
    try {
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "the Recipient",
            new_text: "Receiving Party",
          },
          // Stale target: "the Recipient" was just replaced by edit 1.
          {
            type: "modify",
            target_text: "the Recipient shall maintain",
            new_text: "it shall maintain",
          },
        ] as any[],
      );
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(caught.message).toContain("Batches apply sequentially");
    expect(caught.message).toContain("AFTER the preceding edits");
  });
});
