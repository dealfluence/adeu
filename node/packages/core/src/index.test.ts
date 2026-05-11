import { describe, it, expect } from 'vitest';
import { identifyEngine } from './index.js';

describe('Core Engine', () => {
  it('should identify as adeu-core-node', () => {
    expect(identifyEngine()).toBe('adeu-core-node');
  });
});