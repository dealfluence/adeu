// Sequential batch semantics — cross-engine parity with the Python engine
// (QA 2026-07-17 follow-up). Batches apply SEQUENTIALLY in both modes: each
// edit is validated and applied against the document state produced by the
// edits before it (chaining), validation failures reject the batch
// transactionally, and the dry-run report mirrors the real run's outcome.

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
  it("chained batch applies in BOTH modes with identical stats", async () => {
    const engine = new RedlineEngine(await ndaDoc());

    const resDry = engine.process_batch(chainedBatch(), true);
    expect(resDry.edits_applied).toBe(2);
    expect(resDry.edits_skipped).toBe(0);
    expect(resDry.edits.every((r: any) => r.status === "applied")).toBe(true);

    const resWet = engine.process_batch(chainedBatch(), false);
    expect(resWet.edits_applied).toBe(2);
    expect(resWet.edits_skipped).toBe(0);

    const xml = engine.doc.element.toString();
    expect(xml).toContain("Disclosee");
  });

  it("dry-run mirrors transactional rejection: no edit reported applied when any fails validation", async () => {
    const engine = new RedlineEngine(await ndaDoc());

    const res = engine.process_batch(
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
      true,
    );

    expect(res.edits_applied).toBe(0);
    expect(res.edits_skipped).toBe(2);
    expect(res.edits.every((r: any) => r.status === "failed")).toBe(true);
    expect(res.edits[0].error).toContain("transactional");
    expect(res.edits[1].error.toLowerCase()).toContain("not found");
    // Labeled with the edit's true position in the batch.
    expect(res.edits[1].error).toContain("Edit 2 Failed");
  });

  it("real run rejects the same batch transactionally and leaves the document untouched", async () => {
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
        false,
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
        false,
      );
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(caught.message).toContain("Batches apply sequentially");
    expect(caught.message).toContain("AFTER the preceding edits");
  });
});
