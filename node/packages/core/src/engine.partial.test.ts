// FILE: node/packages/core/src/engine.partial.test.ts
//
// B5, the explicit-salvage contract. `partial: false` (the library default)
// keeps today's all-or-nothing transaction; `partial: true` applies everything
// that validates, skips the rest, and reports every skip by the caller's own
// index so the agent can resubmit exactly those — never silently
// (docs/improvement_spec.md §10: no silent partial application).
// Mirrors python/src/adeu/redline/engine.py _process_batch_internal (:2656-2865).
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { ModifyText } from "./models.js";

const CONTRACT = [
  "The Supplier shall deliver the goods within thirty days.",
  "The Buyer shall pay each invoice within fourteen days.",
];

async function contractEngine(): Promise<[RedlineEngine, DocumentObject]> {
  const doc = await createTestDocument();
  for (const p of CONTRACT) addParagraph(doc, p);
  return [new RedlineEngine(doc, "Salvage Tester"), doc];
}

/** The good edit plus one whose target does not exist, in that order. */
function oneGoodOneBad(): ModifyText[] {
  return [
    { type: "modify", target_text: "thirty days", new_text: "sixty days" },
    { type: "modify", target_text: "not present anywhere", new_text: "x" },
  ] as ModifyText[];
}

describe("explicit-salvage contract (partial)", () => {
  it("rejects the whole batch transactionally when partial is false (default)", async () => {
    const [engine, doc] = await contractEngine();
    const before = doc.element.toString();

    let err: BatchValidationError | null = null;
    try {
      engine.process_batch(oneGoodOneBad());
    } catch (e) {
      err = e as BatchValidationError;
    }

    expect(err, "a bad target must still reject the batch by default").toBeInstanceOf(
      BatchValidationError,
    );
    expect(err!.failed.map(([i]) => i)).toEqual([1]);
    expect(err!.failed[0][1]).toContain("not present anywhere");
    // Rolled back: the good edit left no trace in the body XML.
    expect(doc.element.toString()).toBe(before);
  });

  it("applies the valid edit and records the invalid one when partial is true", async () => {
    const [engine, doc] = await contractEngine();

    const stats = engine.process_batch(oneGoodOneBad(), undefined, true);

    expect(stats.status).toBe("partial");
    expect(stats.edits_applied).toBe(1);
    expect(stats.edits_skipped).toBe(1);
    expect(stats.failed).toHaveLength(1);
    expect(stats.failed[0].index).toBe(1);
    expect(stats.failed[0].reason).toContain("not present anywhere");
    expect(stats.failed[0].error).toBe(stats.failed[0].reason);

    // The per-edit report carries the outcome AND the full reason.
    expect(stats.edits[0].status).toBe("applied");
    expect(stats.edits[1].status).toBe("failed");
    expect(stats.edits[1].error).toContain("not present anywhere");

    // The word-level diff narrows the redline to the word that changed.
    const text = await extractTextFromBuffer(await doc.save());
    expect(text).toContain("{--thirty--}");
    expect(text).toContain("{++sixty++}");
  });

  it("reports every failure when partial is true and nothing validates", async () => {
    const [engine] = await contractEngine();

    const stats = engine.process_batch(
      [
        { type: "modify", target_text: "no such clause", new_text: "a" },
        { type: "modify", target_text: "nor this one", new_text: "b" },
      ] as ModifyText[],
      undefined,
      true,
    );

    // The MCP layer turns applied_count === 0 into a failure envelope rather
    // than a PARTIAL success (batch-envelope.test.ts case 10).
    expect(stats.status).toBe("partial");
    expect(stats.edits_applied).toBe(0);
    expect(stats.failed.map((f: any) => f.index)).toEqual([0, 1]);
  });

  it("reports a broken dependency chain coherently without rolling back unrelated edits", async () => {
    const [engine, doc] = await contractEngine();

    // Edit 1 fails (its target is absent), so edit 2 — which targets the text
    // edit 1 would have introduced — cannot resolve either. Edit 3 is
    // independent and must survive.
    const stats = engine.process_batch(
      [
        { type: "modify", target_text: "Vendor", new_text: "Contractor" },
        { type: "modify", target_text: "Contractor", new_text: "Subcontractor" },
        { type: "modify", target_text: "fourteen days", new_text: "seven days" },
      ] as ModifyText[],
      undefined,
      true,
    );

    expect(stats.status).toBe("partial");
    expect(stats.edits_applied).toBe(1);
    expect(stats.edits_skipped).toBe(2);
    expect(stats.failed.map((f: any) => f.index)).toEqual([0, 1]);
    expect(stats.failed[0].reason).toContain("Vendor");
    expect(stats.failed[1].reason).toContain("Contractor");
    // The rollback hint belongs to the transactional mode only: nothing was
    // rolled back here (engine.py:2811).
    for (const f of stats.failed) {
      expect(f.reason).not.toContain("it was rolled back and nothing was saved");
    }

    const text = await extractTextFromBuffer(await doc.save());
    expect(text).toContain("{--fourteen--}");
    expect(text).toContain("{++seven++}");
  });

  it("skips a stale review action instead of throwing when partial is true", async () => {
    const [engine, doc] = await contractEngine();

    const stats = engine.process_batch(
      [
        { type: "modify", target_text: "thirty days", new_text: "sixty days" },
        { type: "accept", target_id: "Chg:99" },
      ] as any[],
      undefined,
      true,
    );

    expect(stats.status).toBe("partial");
    expect(stats.actions_applied).toBe(0);
    expect(stats.actions_skipped).toBe(1);
    expect(stats.edits_applied).toBe(1);
    expect(stats.failed.map((f: any) => f.index)).toEqual([1]);
    expect(stats.failed[0].reason).toContain("Chg:99");

    const text = await extractTextFromBuffer(await doc.save());
    expect(text).toContain("{++sixty++}");
  });

  it("saves a loadable document whose applied edit is a real tracked change", async () => {
    const [engine, doc] = await contractEngine();

    engine.process_batch(oneGoodOneBad(), undefined, true);

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const xml = reloaded.element.toString();
    expect(xml).toContain("<w:ins");
    expect(xml).toContain("<w:del");
    expect(xml).toContain("<w:delText>thirty</w:delText>");
    expect(xml).toContain("sixty");
  });

  it("reports status 'ok' when nothing failed, in both modes", async () => {
    const [strict] = await contractEngine();
    const strict_stats = strict.process_batch([
      { type: "modify", target_text: "thirty days", new_text: "sixty days" },
    ] as ModifyText[]);
    expect(strict_stats.status).toBe("ok");
    expect(strict_stats.failed).toEqual([]);

    const [salvage] = await contractEngine();
    const salvage_stats = salvage.process_batch(
      [
        { type: "modify", target_text: "thirty days", new_text: "sixty days" },
      ] as ModifyText[],
      undefined,
      true,
    );
    expect(salvage_stats.status).toBe("ok");
    expect(salvage_stats.failed).toEqual([]);
  });
});
