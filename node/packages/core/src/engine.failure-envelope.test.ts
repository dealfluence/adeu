// FILE: node/packages/core/src/engine.failure-envelope.test.ts
//
// B9 — every batch failure carries machine-readable blame, and the blame points
// at the CALLER's index space. Two things are pinned here:
//
//   • extract_failed_indices — the "- Edit N Failed: reason" / "- Action N …" /
//     "- Note: Action N …" prose is parsed back into (0-based index, reason)
//     pairs, mirroring python/src/adeu/redline/engine.py:70-83.
//   • the index space itself — Node used to number each BUCKET from 1, so a
//     batch of [accept, modify] blamed "Edit 1" for the caller's changes[1] and
//     an agent fixing "edit 1" edited the wrong item. Indices are now global
//     across actions and edits, with an explicit original_indices override for
//     callers (the MCP schema-salvage path) that already dropped items.
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import {
  BatchValidationError,
  RedlineEngine,
  extract_failed_indices,
} from "./engine.js";

async function twoParagraphDoc() {
  const doc = await createTestDocument();
  addParagraph(doc, "The Supplier shall deliver the goods.");
  addParagraph(doc, "The Buyer shall pay the invoice.");
  return doc;
}

/** The BatchValidationError a batch is expected to throw. */
function rejectedBatch(engine: RedlineEngine, changes: any[], original_indices?: number[]) {
  try {
    engine.process_batch(changes, original_indices);
  } catch (e) {
    expect(e).toBeInstanceOf(BatchValidationError);
    return e as BatchValidationError;
  }
  throw new Error("the batch was expected to fail validation");
}

describe("extract_failed_indices", () => {
  it("reads the 1-based edit number as a 0-based index and keeps the reason", () => {
    expect(
      extract_failed_indices([
        '- Edit 5 Failed: Target text not found in document:\n  "x"',
      ]),
    ).toEqual([[4, 'Target text not found in document:\n  "x"']]);
  });

  it("matches Action, Note: Action and mixed case, and falls back to index 0", () => {
    expect(extract_failed_indices(["- Action 3 Failed: boom"])).toEqual([
      [2, "boom"],
    ]);
    expect(extract_failed_indices(["- edit 7 Failed: boom"])).toEqual([
      [6, "boom"],
    ]);
    // "Note: Action N" carries no "Failed: " — the whole line is the reason.
    const note =
      "- Note: Action 2 ('accept' on Chg:5) had no additional effect.";
    expect(extract_failed_indices([note])).toEqual([[1, note]]);
    // Unrecognized prose still produces an entry, blamed on index 0.
    expect(extract_failed_indices(["  something went wrong  "])).toEqual([
      [0, "something went wrong"],
    ]);
  });
});

describe("BatchValidationError.failed", () => {
  it("derives failed pairs from the error prose", () => {
    const err = new BatchValidationError(["- Edit 2 Failed: boom"]);
    expect(err.failed).toEqual([[1, "boom"]]);
    expect(err.errors).toEqual(["- Edit 2 Failed: boom"]);
  });

  it("prefers an explicitly passed failed list over derivation", () => {
    const err = new BatchValidationError(["- Edit 2 Failed: boom"], [
      [9, "explicit"],
    ]);
    expect(err.failed).toEqual([[9, "explicit"]]);
  });
});

describe("batch blame uses the caller's index space", () => {
  it("numbers a mixed [action, edit] batch globally, not per bucket", async () => {
    const engine = new RedlineEngine(await twoParagraphDoc(), "Blamer");
    const err = rejectedBatch(engine, [
      { type: "accept", target_id: "Chg:9999" },
      { type: "modify", target_text: "not in this document", new_text: "x" },
    ]);

    const joined = err.errors.join("\n");
    expect(joined).toContain("- Action 1 Failed:");
    // The parity fix: the modify is changes[1], so it is "Edit 2".
    expect(joined).toContain("- Edit 2 Failed:");
    expect(joined).not.toContain("- Edit 1 Failed:");
    expect(err.failed.map(([index]) => index)).toEqual([0, 1]);
  });

  it("blames the right two of three changes", async () => {
    const engine = new RedlineEngine(await twoParagraphDoc(), "Blamer");
    const err = rejectedBatch(engine, [
      { type: "modify", target_text: "the goods", new_text: "the Goods" },
      { type: "accept", target_id: "Chg:9999" },
      { type: "modify", target_text: "absent from the document", new_text: "y" },
    ]);

    expect(err.failed.map(([index]) => index)).toEqual([1, 2]);
    const joined = err.errors.join("\n");
    expect(joined).toContain("- Action 2 Failed:");
    expect(joined).toContain("- Edit 3 Failed:");
  });

  it("honours an explicit original_indices map", async () => {
    const engine = new RedlineEngine(await twoParagraphDoc(), "Blamer");
    const err = rejectedBatch(
      engine,
      [
        { type: "modify", target_text: "first missing target", new_text: "x" },
        { type: "modify", target_text: "second missing target", new_text: "y" },
      ],
      [3, 7],
    );

    const joined = err.errors.join("\n");
    expect(joined).toContain("- Edit 4 Failed:");
    expect(joined).toContain("- Edit 8 Failed:");
    expect(err.failed.map(([index]) => index)).toEqual([3, 7]);
  });

  it("reports status 'ok' and an empty failed list on a clean batch", async () => {
    const engine = new RedlineEngine(await twoParagraphDoc(), "Blamer");
    const stats = engine.process_batch([
      { type: "modify", target_text: "the goods", new_text: "the Goods" },
    ] as any);

    expect(stats.status).toBe("ok");
    expect(stats.failed).toEqual([]);
    expect(stats.edits_applied).toBe(1);
  });
});
