import { describe, it, expect } from 'vitest';
import { createTestDocument } from './test-utils.js';
import { RedlineEngine } from './engine.js';
import { DocumentMapper } from './mapper.js';
import { extract_outline } from './outline.js';
import { _extractTextFromDoc } from './ingest.js';
import { paginate } from './pagination.js';

describe('Parity Gaps (TDD)', () => {
  it('GAP 2: original_view maps deleted text and validate_edits throws actionable deletion error', async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;
    
    // Create a paragraph with a tracked deletion: <w:p><w:del w:id="1" w:author="Test Negotiator"><w:r><w:t>Deleted sentence.</w:t></w:r></w:del></w:p>
    const p = xmlDoc.createElement('w:p');
    const del = xmlDoc.createElement('w:del');
    del.setAttribute('w:id', '1');
    del.setAttribute('w:author', 'Test Negotiator');
    
    const r = xmlDoc.createElement('w:r');
    const t = xmlDoc.createElement('w:t');
    t.textContent = 'Deleted sentence.';
    
    r.appendChild(t);
    del.appendChild(r);
    p.appendChild(del);
    doc.element.appendChild(p);

    // 1. Verify original_view mapping
    const mapperOrig = new DocumentMapper(doc, false, true);
    expect(mapperOrig.full_text).toContain('Deleted sentence.');

    const mapperRaw = new DocumentMapper(doc, false, false);
    expect(mapperRaw.full_text).toContain('{--Deleted sentence.--}');

    // 2. Validate modification targetting deleted text
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      {
        target_text: 'Deleted sentence.',
        new_text: 'Active replacement text.',
      }
    ]);

    expect(errors.length).toBe(1);
    expect(errors[0]).toContain('Target text matches text inside a tracked deletion by Test Negotiator.');
    expect(errors[0]).toContain('Reject/accept that change first or target the active replacement text instead.');
  });

  it('GAP 1: heading inside a deleted region is filtered out when using paragraph_offsets', async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    const p1 = xmlDoc.createElement('w:p');
    const p1Pr = xmlDoc.createElement('w:pPr');
    const p1Style = xmlDoc.createElement('w:pStyle');
    p1Style.setAttribute('w:val', 'Heading1');
    p1Pr.appendChild(p1Style);
    p1.appendChild(p1Pr);
    const r1 = xmlDoc.createElement('w:r');
    const t1 = xmlDoc.createElement('w:t');
    t1.textContent = 'Active Heading';
    r1.appendChild(t1);
    p1.appendChild(r1);
    doc.element.appendChild(p1);

    const p2 = xmlDoc.createElement('w:p');
    const p2Pr = xmlDoc.createElement('w:pPr');
    const p2Style = xmlDoc.createElement('w:pStyle');
    p2Style.setAttribute('w:val', 'Heading1');
    p2Pr.appendChild(p2Style);
    p2.appendChild(p2Pr);
    const r2 = xmlDoc.createElement('w:r');
    const t2 = xmlDoc.createElement('w:t');
    t2.textContent = 'Deleted Heading';
    r2.appendChild(t2);
    p2.appendChild(r2);
    doc.element.appendChild(p2);

    const extract_res = _extractTextFromDoc(doc, false, false, true) as { text: string; paragraph_offsets: Map<any, [number, number]> };
    
    // Simulate deletion/skipping of p2 during projection
    extract_res.paragraph_offsets.delete(p2);

    const pages = paginate(extract_res.text, '');
    const nodes = extract_outline(
      doc,
      extract_res.text,
      pages.body_pages,
      pages.body_page_offsets,
      extract_res.paragraph_offsets as any,
    );

    // Only Active Heading should be in the outline, Deleted Heading must be skipped because it is not in paragraph_offsets!
    expect(nodes.length).toBe(1);
    expect(nodes[0].text).toBe('Active Heading');
  });
});
