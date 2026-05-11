import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph } from './test-utils.js';
import { RedlineEngine } from './engine.js';
import { ModifyText } from './models.js';

describe('Safety Engine Constraints (Node.js Port)', () => {
  it('rejects empty target heuristic to prevent accidental insertions', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Content");

    const engine = new RedlineEngine(doc);
    const edit: ModifyText = { type: 'modify', target_text: "", new_text: "Unexpected Header" };

    const [applied, skipped] = engine.apply_edits([edit]);

    expect(applied).toBe(0);
    expect(skipped).toBe(1);

    const xml = (doc.element as Element).ownerDocument?.documentElement.toString();
    expect(xml).not.toContain("Unexpected Header");
  });

  it('applies heuristic edits strictly once for multiple occurrences', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Repeat");
    addParagraph(doc, "Repeat");

    const engine = new RedlineEngine(doc);
    const edit: ModifyText = { type: 'modify', target_text: "Repeat", new_text: "Changed" };

    const [applied, skipped] = engine.apply_edits([edit]);

    expect(applied).toBe(1);
    expect(skipped).toBe(0);

    const xml = (doc.element as Element).ownerDocument?.documentElement.toString();
    
    // We expect one deletion of Repeat and one insertion of Changed
    expect((xml?.match(/<w:delText[^>]*>Repeat<\/w:delText>/g) || []).length).toBe(1);
    expect((xml?.match(/<w:t[^>]*>Changed<\/w:t>/g) || []).length).toBe(1);
  });
});