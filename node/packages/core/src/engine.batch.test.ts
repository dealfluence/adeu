import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph } from './test-utils.js';
import { DocumentObject } from './docx/bridge.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine } from './engine.js';
import { ModifyText, AcceptChange, RejectChange } from './models.js';

describe('Batch Reliability (Node.js Port)', () => {
  it('batch accept does not corrupt the document', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Para 1");
    addParagraph(doc, "Para 2");
    addParagraph(doc, "Para 3");

    const engine = new RedlineEngine(doc);
    const edits: ModifyText[] = [
      { type: 'modify', target_text: "Para 1", new_text: "Para One" },
      { type: 'modify', target_text: "Para 2", new_text: "Para Two" },
      { type: 'modify', target_text: "Para 3", new_text: "Para Three" },
    ];

    engine.apply_edits(edits);
    const redlinedBuf = await doc.save();

    const text = await extractTextFromBuffer(redlinedBuf);
    expect(text).toContain("[Chg:1 ");
    expect(text).toContain("[Chg:6 ");

    // BATCH ACCEPT ALL
    const midDoc = await DocumentObject.load(redlinedBuf);
    const engine2 = new RedlineEngine(midDoc);
    
    const actions: AcceptChange[] = [1, 2, 3, 4, 5, 6].map(id => ({ type: 'accept', target_id: `Chg:${id}` }));
    
    // Test direct apply_review_actions (Note: method missing in raw TS port right now, needs implementing)
    const [applied, skipped] = (engine2 as any).apply_review_actions(actions);
    
    expect(applied).toBe(6);
    expect(skipped).toBe(0);

    const finalBuf = await midDoc.save();
    const final_text = await extractTextFromBuffer(finalBuf);

    expect(final_text).toContain("Para One");
    expect(final_text).toContain("Para Two");
    expect(final_text).toContain("Para Three");

    expect(final_text).not.toContain("Para 1");
    expect(final_text).not.toContain("Para 2");
    expect(final_text).not.toContain("Para 3");

    expect(final_text).not.toContain("[Chg:");
    expect(final_text).not.toContain("{++");
    expect(final_text).not.toContain("{--");
  });

  it('batch mixed accept and reject maintains integrity', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Para 1");
    addParagraph(doc, "Para 2");

    const engine = new RedlineEngine(doc);
    const edits: ModifyText[] = [
      { type: 'modify', target_text: "Para 1", new_text: "Para One" },
      { type: 'modify', target_text: "Para 2", new_text: "Para Two" },
    ];

    engine.apply_edits(edits);
    const redlinedBuf = await doc.save();

    const midDoc = await DocumentObject.load(redlinedBuf);
    const engine2 = new RedlineEngine(midDoc);

    const actions = [
      { type: 'accept', target_id: "Chg:3" } as AcceptChange,
      { type: 'accept', target_id: "Chg:4" } as AcceptChange,
      { type: 'reject', target_id: "Chg:1" } as RejectChange,
      { type: 'reject', target_id: "Chg:2" } as RejectChange,
    ];

    const [applied] = (engine2 as any).apply_review_actions(actions);
    expect(applied).toBe(4);

    const finalBuf = await midDoc.save();
    const text_final = await extractTextFromBuffer(finalBuf);

    expect(text_final).toContain("Para One");
    expect(text_final).not.toContain("Para 1");

    expect(text_final).toContain("Para 2");
    expect(text_final).not.toContain("Para Two");
  });
});