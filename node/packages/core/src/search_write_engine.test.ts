import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph } from './test-utils.js';
import { RedlineEngine, BatchValidationError } from './engine.js';
import { ModifyText } from './models.js';
import { extractTextFromBuffer } from './ingest.js';

describe('Search and Targeted Write Engine', () => {
  
  it('match_mode="strict" fails on duplicate targets', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a repetitive clause.");
    addParagraph(doc, "Some other text.");
    addParagraph(doc, "This is a repetitive clause.");
    
    const engine = new RedlineEngine(doc);
    
    // We cast to any to bypass type checking since ModifyText doesn't have match_mode yet
    const edits: any[] = [{
      type: 'modify',
      target_text: "This is a repetitive clause.",
      new_text: "This is changed.",
      match_mode: 'strict'
    }];

    expect(() => engine.process_batch(edits)).toThrowError(BatchValidationError);
  });

  it('strict rejection carries match_mode guidance, and the suggested re-call applies (Turn Loop Trap)', async () => {
    // The DPA scenario: one placeholder repeated across clauses.
    const doc = await createTestDocument();
    addParagraph(doc, "PROVIDER: [official company name] shall process the data.");
    addParagraph(doc, "PROVIDER: [official company name] is the data processor.");

    const engine = new RedlineEngine(doc);
    const strictEdit: any = {
      type: 'modify',
      target_text: "PROVIDER: [official company name]",
      new_text: "PROVIDER: Acme Corp",
    };

    // 1. The strict edit is rejected — and the error MUST name the escape hatch
    // so the agent re-calls instead of looping on context/regex refinement.
    let caught: BatchValidationError | null = null;
    try {
      engine.process_batch([strictEdit]);
    } catch (e) {
      caught = e as BatchValidationError;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    const msg = caught!.errors.join("\n");
    expect(msg).toContain("Ambiguous match");
    expect(msg).toContain('"match_mode": "all"');
    expect(msg).toContain('"match_mode": "first"');

    // 2. Follow the guidance verbatim — the same target_text with match_mode="all"
    // applies cleanly and mutates BOTH clauses on the saved document.
    const doc2 = await createTestDocument();
    addParagraph(doc2, "PROVIDER: [official company name] shall process the data.");
    addParagraph(doc2, "PROVIDER: [official company name] is the data processor.");
    const engine2 = new RedlineEngine(doc2);
    const stats = engine2.process_batch([{ ...strictEdit, match_mode: 'all' }]);
    expect(stats.edits[0].occurrences_modified).toBe(2);

    const text = await extractTextFromBuffer(await doc2.save(), true);
    expect(text.match(/Acme Corp/g)?.length).toBe(2);
    expect(text).not.toContain("PROVIDER: [official company name]");
  });

  it('match_mode="first" modifies only the first occurrence', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a repetitive clause.");
    addParagraph(doc, "This is a repetitive clause.");
    
    const engine = new RedlineEngine(doc);
    const edits: any[] = [{
      type: 'modify',
      target_text: "This is a repetitive clause.",
      new_text: "This is changed.",
      match_mode: 'first'
    }];

    const stats = engine.process_batch(edits);
    
    // Should be applied successfully
    expect(stats.edits_applied).toBe(1);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, true);
    
    // Only one occurrence should be modified in the accepted state
    const newMatches = text.match(/This is changed/g);
    expect(newMatches?.length).toBe(1);
    
    const oldMatches = text.match(/This is a repetitive clause/g);
    expect(oldMatches?.length).toBe(1);
  });

  it('match_mode="all" modifies all occurrences', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a repetitive clause.");
    addParagraph(doc, "This is a repetitive clause.");
    
    const engine = new RedlineEngine(doc);
    const edits: any[] = [{
      type: 'modify',
      target_text: "This is a repetitive clause.",
      new_text: "This is changed.",
      match_mode: 'all'
    }];

    const stats = engine.process_batch(edits);
    
    // It's still 1 edit instruction applied
    expect(stats.edits_applied).toBe(1); 
    
    // The enriched report should show 2 occurrences modified
    expect(stats.edits[0].occurrences_modified).toBe(2);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, true);
    
    // Both occurrences should be modified
    const newMatches = text.match(/This is changed/g);
    expect(newMatches?.length).toBe(2);
  });

  it('supports regex replacements with RegExp engine', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Item cost: $500.");
    addParagraph(doc, "Item cost: $1200.");
    
    const engine = new RedlineEngine(doc);
    
    // Using ES2022 RegExp capture group $1
    const edits: any[] = [{
      type: 'modify',
      target_text: "Item cost: \\$(\\d+)\\.",
      new_text: "Item cost: EUR $1.",
      match_mode: 'all',
      regex: true
    }];

    const stats = engine.process_batch(edits);
    expect(stats.edits_applied).toBe(1);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, true);
    
    // Both should be correctly substituted
    expect(text).toContain("EUR 500");
    expect(text).toContain("EUR 1200");
  });
});