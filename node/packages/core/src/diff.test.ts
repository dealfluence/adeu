import { describe, it, expect } from 'vitest';
import { trim_common_context, generate_edits_from_text, create_word_patch_diff, generate_edits_via_paragraph_alignment } from './diff.js';
import { RedlineEngine } from './engine.js';
import { addParagraph, createTestDocument } from './test-utils.js';

describe('Diff Logic & Context Trimming', () => {
  it('handles basic prefix and suffix', () => {
    const t = 'Context A Context';
    const n = 'Context B Context';
    const [p, s] = trim_common_context(t, n);
    expect(p).toBe(8); // "Context "
    expect(s).toBe(8); // " Context"
  });

  it('handles prefix only', () => {
    const t = 'Hello World';
    const n = 'Hello User';
    const [p, s] = trim_common_context(t, n);
    expect(p).toBe(6); // "Hello "
    expect(s).toBe(0);
  });

  it('handles suffix only', () => {
    const t = 'Old Item';
    const n = 'New Item';
    const [p, s] = trim_common_context(t, n);
    expect(p).toBe(0);
    expect(s).toBe(5); // " Item"
  });

  it('handles morph to insert (no suffix overlap)', () => {
    const t = 'Prefix';
    const n = 'Prefix Added';
    const [p, s] = trim_common_context(t, n);
    expect(p).toBe(6);
    expect(s).toBe(0);
  });

  it('prevents full suffix overlap crash (IndexError repro)', () => {
    const target = 'Agreement';
    const new_val = 'New Agreement';
    const [p, s] = trim_common_context(target, new_val);
    expect(p).toBe(0);
    expect(s).toBe(9); // "Agreement"
  });

  it('fixes start-of-doc insertion duplication bug', () => {
    const original = 'Contract Agreement';
    const modified = 'Big Contract Agreement';

    const edits = generate_edits_from_text(original, modified);

    // We want exactly 1 semantic edit to represent this change.
    expect(edits.length).toBe(1);

    const edit = edits[0];
    if (edit.target_text === '') {
      expect(edit.new_text.trim()).toBe('Big');
    } else {
      expect(edit.target_text).toContain('Contract');
      expect(edit.new_text).toContain('Big');
    }
  });

  it('generates a Word Patch formatted diff matching Python parity', () => {
    const original = "This agreement is made between the Company and the Contractor.";
    const modified = "This agreement is made between the Corporation and the Contractor.";
    
    const diff = create_word_patch_diff(original, modified);
    
    expect(diff).toContain("@@ Word Patch @@");
    expect(diff).toContain("- Company");
    expect(diff).toContain("+ Corporation");
    expect(diff).toContain(" This agreement is made between the"); // Within 40-char context window so no truncation
  });

  it('handles trailing newline and whitespace safely in paragraph alignment', () => {
    const original = "This is a single paragraph of the draft.";
    const modified = "This is a single paragraph of the draft.\n";
    const edits = generate_edits_via_paragraph_alignment(original, modified);
    expect(edits.length).toBe(0);
  });
});

// Punctuation-only changes trim to the punctuation. The prefix backtrack used
// to accept only whitespace as a word boundary, so "Cause." -> "Cause;" struck
// the whole word. Python twin: tests/test_context_trimming.py.
describe('punctuation-only changes', () => {
  const remainder = (t: string, n: string) => {
    const [p, s] = trim_common_context(t, n);
    return [t.substring(p, t.length - s), n.substring(p, n.length - s)];
  };

  it('trims a trailing punctuation swap to the punctuation', () => {
    expect(remainder('Cause.', 'Cause;')).toEqual(['.', ';']);
    expect(remainder('shall.', 'shall,')).toEqual(['.', ',']);
  });

  it('keeps the word when a clause follows the changed punctuation', () => {
    const [deleted, inserted] = remainder('Cause.', 'Cause; provided that Involuntary Termination shall not include');
    expect(deleted).toBe('.');
    expect(inserted.startsWith(';')).toBe(true);
  });

  it('does not treat punctuation inside a token as a boundary', () => {
    expect(remainder('color', 'colour')).toEqual(['color', 'colour']);
    expect(remainder('$10,000.00', '$15,000.00')).toEqual(['$10,000.00', '$15,000.00']);
    expect(remainder('https://old-site.com/x', 'https://new-site.com/x')).toEqual([
      'https://old-site.com/x',
      'https://new-site.com/x',
    ]);
    expect(remainder('example.com', 'example.org')).toEqual(['example.com', 'example.org']);
    expect(remainder('text.', 'document.')).toEqual(['text.', 'document.']);
  });

  it('does not restate the word in a commented edit', async () => {
    const doc = await createTestDocument();
    addParagraph(doc, 'Involuntary Termination shall mean a termination by the Company without Cause.');
    const engine = new RedlineEngine(doc, 'QA');
    engine.process_batch([
      {
        type: 'modify',
        target_text: 'without Cause.',
        new_text: 'without Cause; provided that Involuntary Termination shall not include a termination',
        comment: 'Carve-out.',
      } as any,
    ]);
    const deleted = Array.from(doc.element.getElementsByTagName('w:delText')).map((d) => d.textContent);
    expect(deleted).toEqual(['.']);
  });
});