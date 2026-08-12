import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph } from './test-utils.js';
import { DocumentObject } from './docx/bridge.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine, BatchValidationError } from './engine.js';
import { ModifyText, AcceptChange } from './models.js';

describe('Atomic Batch Pipeline (Node.js Port)', () => {
  it('prevents cascading misanchor when accepting changes shifts indices', async () => {
    // 1. Setup initial doc
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph.");
    addParagraph(doc, "Second paragraph.");
    addParagraph(doc, "Third paragraph.");

    // 2. Make an initial tracked change (Simulating Round 1)
    const engine = new RedlineEngine(doc, "Round1");
    engine.apply_edits([{ type: 'modify', target_text: "First", new_text: "1st" } as ModifyText]);

    const midBuf = await doc.save();

    // Verify intermediate state (Round 1)
    const midText = await extractTextFromBuffer(midBuf);
    expect(midText).toContain("{--First--}");
    expect(midText).toContain("{++1st++}");

    // Extract dynamically generated Change IDs for the Accept action
    const matches = Array.from(midText.matchAll(/\[Chg:(\d+)(?:\s+\w+)?\]/g));
    const chgIds = new Set(matches.map(m => m[1]));
    expect(chgIds.size).toBeGreaterThan(0);

    // 3. Execute the Atomic Batch (Simulating Round 2)
    const midDoc = await DocumentObject.load(midBuf);
    const engine2 = new RedlineEngine(midDoc, "Round2");

    const actions = Array.from(chgIds).map(id => ({ type: 'accept', target_id: `Chg:${id}` } as AcceptChange));
    const edits = [{ type: 'modify', target_text: "Third", new_text: "3rd" } as ModifyText];
    
    const changes = [...actions, ...edits];
    const stats = engine2.process_batch(changes);

    // 4. Assertions on the Tool Execution
    // The two ids form ONE replacement pair: the first accept resolves both,
    // the second is an accurate no-op (QA 2026-07-19 ADEU-QA-004) — never a
    // second "applied" state transition.
    expect(stats.actions_applied).toBe(1);
    expect(stats.actions_already_resolved).toBe(actions.length - 1);
    expect(stats.edits_applied).toBe(1);

    // 5. Assertions on the Final Document State
    const finalBuf = await midDoc.save();
    const final_text = await extractTextFromBuffer(finalBuf);

    // The first paragraph should be cleanly accepted
    expect(final_text).toContain("1st paragraph.");
    expect(final_text).not.toContain("{--First--}");

    // The third paragraph should have the new tracked change anchored perfectly
    expect(final_text).toContain("{--Third--}");
    expect(final_text).toContain("{++3rd++}");
  });

  // The explicit-salvage contract (B5) added `partial`; strict mode is still
  // the library default and still all-or-nothing — see engine.partial.test.ts
  // for the salvage half.
  it('rolls back every applied edit when a later one fails (partial: false)', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph.");
    addParagraph(doc, "Second paragraph.");

    const engine = new RedlineEngine(doc, "Strict");
    const before = doc.element.toString();

    let err: BatchValidationError | null = null;
    try {
      engine.process_batch(
        [
          { type: 'modify', target_text: "First", new_text: "1st" },
          { type: 'modify', target_text: "absent phrase", new_text: "x" },
        ] as ModifyText[],
        undefined,
        false,
      );
    } catch (e) {
      err = e as BatchValidationError;
    }

    expect(err).toBeInstanceOf(BatchValidationError);
    expect(err!.failed.map(([i]) => i)).toEqual([1]);
    // The rollback hint belongs to this mode: something WAS applied and then
    // undone, and the caller has to know its target may now read stale.
    expect(err!.errors.join("\n")).toContain(
      "it was rolled back and nothing was saved",
    );
    expect(doc.element.toString()).toBe(before);
  });
});
