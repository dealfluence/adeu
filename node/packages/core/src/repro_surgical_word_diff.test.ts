import { describe, it, expect } from 'vitest';
import { createTestDocument, addParagraph } from './test-utils.js';
import { extractTextFromBuffer } from './ingest.js';
import { RedlineEngine } from './engine.js';

describe('Surgical Word-Level Diffing', () => {
  it('preserves interior unchanged words as bare text', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumped.");
    
    const engine = new RedlineEngine(doc, "Test AI");
    engine.process_batch([{
      type: "modify",
      target_text: "The quick brown fox jumped.",
      new_text: "The slow brown fox leapt."
    } as any]);
    
    const outBuf = await doc.save();
    const resultText = await extractTextFromBuffer(outBuf, false);
    
    expect(resultText).not.toContain("{--The quick brown fox jumped.--}");
    expect(resultText).toContain("{--quick--}{++slow++}");
    expect(resultText).toContain(" brown fox ");
    expect(resultText).toContain("{--jumped--}{++leapt++}");
  });
});
