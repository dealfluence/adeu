import { describe, it, expect } from 'vitest';
import { strFromU8, unzipSync } from 'fflate';
import { DocumentObject } from './docx/bridge.js';
import { CommentsManager, extract_comments_data } from './comments.js';
import { RedlineEngine } from './engine.js';
import { createTestDocumentWithComments, loadFixtureDoc } from './test-utils.js';

describe('CommentsManager & DOM Serialization', () => {
  it('should safely add a comment and serialize to buffer', async () => {
    // 1. Load the golden DOCX from shared fixtures
    const doc = await loadFixtureDoc('golden.docx');
    
    // 2. Perform a mutation via the TS implementation of CommentsManager
    const mgr = new CommentsManager(doc);
    const cid = mgr.addComment('Adeu AI (TS)', 'This is a test comment injected by Node.js');
    
    expect(cid).toBeDefined();
    
    // 3. Serialize the mutated DOM back to a zipped DOCX buffer
    const savedBuf = await doc.save();
    expect(savedBuf.length).toBeGreaterThan(0);
    
    // 4. Ensure we can load the serialized buffer back (verifying [Content_Types] and .rels integrity)
    const doc2 = await DocumentObject.load(savedBuf);
    expect(doc2.pkg.parts.length).toBeGreaterThan(0);
    
    // Assert the comment survived the roundtrip
    const data = extract_comments_data(doc2.pkg);
    // golden.docx has 3 comments initially, plus our 1 new one = 4
    expect(Object.keys(data).length).toBe(4); 
    expect(data[cid].author).toBe('Adeu AI (TS)');
  });
});

// An existing comments.xml root keeps its declarations and gains the extension
// namespaces + mc:Ignorable entries Word needs (TechGC fork 309fc4c / LLO-1694).
// Python twin: python/tests/test_comments.py.
describe('existing comments root namespaces', () => {
  const W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
  const R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  const MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006';
  const W14_NS = 'http://schemas.microsoft.com/office/word/2010/wordml';
  const W16SE_NS = 'http://schemas.microsoft.com/office/word/2015/wordml/symex';

  async function applyToExistingComments(rootAttrs: string, commentBody: string): Promise<string> {
    const doc = await createTestDocumentWithComments(
      '<w:p><w:r><w:t>The quick brown fox.</w:t></w:r></w:p>',
      `<w:comments ${rootAttrs}><w:comment w:id="0" w:author="Counterparty">${commentBody}</w:comment></w:comments>`,
    );
    const engine = new RedlineEngine(doc, 'Jane Doe');
    engine.process_batch([
      { type: 'modify', target_text: 'quick', new_text: 'slow', comment: 'Foxes are not always quick.' } as any,
    ]);
    const files = unzipSync(new Uint8Array(await doc.save()));
    return strFromU8(files['word/comments.xml']);
  }

  const rootTag = (xml: string) => xml.match(/<w:comments\b[^>]*>/)![0];
  const ignorable = (xml: string) => (rootTag(xml).match(/mc:Ignorable="([^"]*)"/)?.[1] ?? '').split(/\s+/);

  it('keeps the r namespace an existing hyperlinked comment uses', async () => {
    const xml = await applyToExistingComments(
      `xmlns:w="${W_NS}" xmlns:r="${R_NS}"`,
      '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>see policy</w:t></w:r></w:hyperlink></w:p>',
    );
    expect(rootTag(xml)).toContain(`xmlns:r="${R_NS}"`);
    expect(xml).toContain('Foxes are not always quick.');
    expect(ignorable(xml)).toEqual(['w14', 'w15', 'w16cid', 'w16cex']);
  });

  it('merges existing mc:Ignorable prefixes with the added ones', async () => {
    const xml = await applyToExistingComments(
      `xmlns:w="${W_NS}" xmlns:mc="${MC_NS}" xmlns:w16se="${W16SE_NS}" mc:Ignorable="w16se"`,
      '<w:p><w:r><w:t>Earlier note</w:t></w:r></w:p>',
    );
    expect(ignorable(xml)).toEqual(['w16se', 'w14', 'w15', 'w16cid', 'w16cex']);
    expect(rootTag(xml)).toContain(`xmlns:w16se="${W16SE_NS}"`);
  });

  it('rebinds w14 when the root binds it to another URI', async () => {
    const xml = await applyToExistingComments(
      `xmlns:w="${W_NS}" xmlns:w14="urn:other"`,
      '<w:p><w:r><w:t>Earlier note</w:t></w:r></w:p>',
    );
    expect(rootTag(xml)).toContain(`xmlns:w14="${W14_NS}"`);
    expect(rootTag(xml)).not.toContain('urn:other');
  });
});