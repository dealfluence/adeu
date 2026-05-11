import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DocumentObject } from './docx/bridge.js';
import { DocumentMapper } from './mapper.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

describe('Virtual DOM Mapper (Node.js Port)', () => {
  it('should construct a full virtual map from the DOCX', async () => {
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    const buf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(buf);

    const mapper = new DocumentMapper(doc);

    // Verify full_text was structurally mapped identically to what `ingest.ts` would produce
    // for the body.
    expect(mapper.full_text).toContain('golden');
    expect(mapper.full_text).toContain('document');
    expect(mapper.spans.length).toBeGreaterThan(0);
  });

  it('should locate target string matches via find_match_index', async () => {
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    const buf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(doc);

    // Search for a word we know exists in golden.docx
    const [startIdx, len] = mapper.find_match_index('golden');
    
    expect(startIdx).toBeGreaterThan(0);
    expect(len).toBe(6); // 'golden'.length
  });

  it('should resolve strings back to physical Run elements', async () => {
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    const buf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(doc);

    // Find the backing physical Run elements for the target string
    const runs = mapper.find_target_runs('golden');
    
    // Ensure we successfully crossed the Virtual DOM boundary
    expect(runs.length).toBeGreaterThan(0);
    expect(runs[0].constructor.name).toBe('Run');
    
    // Underneath, the Run must contain the real xmldom Element reference
    expect(runs[0]._element).toBeDefined();
    expect(runs[0]._element.tagName).toBe('w:r');
  });

  it('should safely return empty arrays for non-existent text', async () => {
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    const buf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(doc);
    
    const runs = mapper.find_target_runs('NON_EXISTENT_TEXT_12345');
    expect(runs.length).toBe(0);
  });
});