import { describe, it, expect } from 'vitest';
import { DocumentObject } from './docx/bridge.js';
import { CommentsManager, extract_comments_data } from './comments.js';
import { loadFixtureDoc } from './test-utils.js';

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