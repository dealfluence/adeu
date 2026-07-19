import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { extractTextFromBuffer } from './ingest.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

describe('Ingestion Engine (Node.js Port)', () => {
  it('should successfully extract text and markup from golden.docx', async () => {
    // Resolve path across the monorepo boundary to the shared fixtures
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    
    // Read the physical file into a Node Buffer
    const buf = readFileSync(fixturePath);
    
    // Execute the ported Node extraction pipeline
    const markdown = await extractTextFromBuffer(buf);
    
    // Basic structural assertions
    expect(typeof markdown).toBe('string');
    expect(markdown.length).toBeGreaterThan(0);
    
    // Assert exact parity with the Python engine's CriticMarkup generation
    // The del+ins pair of one modification is annotated as a resolution
    // group (QA 2026-07-19 ADEU-QA-004).
    expect(markdown).toBe('This is the {--initial --}{++golden ++}{>>[Chg:3 delete] Mikko Korpela (pairs with Chg:4)\n[Chg:4 insert] Mikko Korpela (pairs with Chg:3)\n[Com:0] Mikko Korpela @ 2026-01-23T07:25:00Z: Start of comment thread\n[Com:1] Mikko Korpela @ 2026-01-23T07:25:00Z: Second comment\n[Com:2] Mikko Korpela @ 2026-01-23T07:26:00Z: Third comment in the thread<<}document');
  });

  it('should execute in cleanView mode without failing', async () => {
    const fixturePath = resolve(__dirname, '../../../../shared/fixtures/golden.docx');
    const buf = readFileSync(fixturePath);
    
    // Execute extraction simulating "Accept All Changes"
    const cleanMarkdown = await extractTextFromBuffer(buf, true);
    
    expect(typeof cleanMarkdown).toBe('string');
    // cleanView should not output raw CriticMarkup tracking markers
    expect(cleanMarkdown).not.toContain('{++');
    expect(cleanMarkdown).not.toContain('{--');
    
    // It should simulate "Accept All Changes" (initial -> golden)
    expect(cleanMarkdown).toBe('This is the golden document');
  });
});