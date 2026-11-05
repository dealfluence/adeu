/**
 * Each per-edit report names its position in the caller's change list.
 * `edits[]` holds reports for text edits only — review actions are filtered
 * out — so in a mixed batch `edits[0]` can describe change #1. `failed[]`
 * already carried the caller's index; every report now does too.
 *
 * Python twin: python/tests/test_edit_report_index.py
 */
import { describe, it, expect } from "vitest";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { shrink_batch_stats } from "./payloads.js";
import { addParagraph, createTestDocument } from "./test-utils.js";

async function docWithOneTrackedChange(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Bonus in March.");
  addParagraph(doc, "Governing law is the State of Delaware.");
  new RedlineEngine(doc, "A").process_batch([
    { type: "modify", target_text: "in March", new_text: "in January" } as any,
  ]);
  return DocumentObject.load(Buffer.from(await doc.save()));
}

async function feeBatch() {
  const doc = await createTestDocument();
  addParagraph(doc, "The termination fee is twenty five thousand dollars.");
  return new RedlineEngine(doc, "T").process_batch(
    [
      { type: "modify", target_text: "twenty five thousand", new_text: "fifty thousand" } as any,
      { type: "modify", target_text: "twenty five thousand", new_text: "ten thousand" } as any,
    ],
    undefined,
    true,
  ) as any;
}

describe("per-edit report index", () => {
  it("carries the caller's index in a mixed batch", async () => {
    const doc = await docWithOneTrackedChange();
    const result: any = new RedlineEngine(doc, "B").process_batch([
      { type: "accept", target_id: "Chg:1" } as any,
      { type: "modify", target_text: "State of Delaware", new_text: "State of New York" } as any,
    ]);
    expect(result.actions_applied).toBe(1);
    expect(result.edits.map((r: any) => r.index)).toEqual([1]);
  });

  it("numbers applied and failed reports like `failed`", async () => {
    const result = await feeBatch();
    expect(result.edits.map((r: any) => [r.index, r.status])).toEqual([
      [0, "applied"],
      [1, "failed"],
    ]);
    expect(result.failed.map((f: any) => f.index)).toEqual([1]);
  });

  it("keeps the index on failed edits only in the minimal report", async () => {
    const [applied, failed] = (shrink_batch_stats(await feeBatch()) as any).edits;
    expect("index" in applied).toBe(false);
    expect(failed.index).toBe(1);
  });
});
