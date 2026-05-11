import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';

export default defineConfig({
  test: {
    environment: 'node',
    globals: true,
    alias: {
      '@shared/fixtures': resolve(__dirname, '../../../shared/fixtures'),
    },
  },
});