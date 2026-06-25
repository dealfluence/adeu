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