import { defineConfig } from 'tsup';

export default defineConfig([
  {
    entry: ['src/index.ts'],
    format: ['esm'],
    dts: true,
    sourcemap: true,
    clean: true,
    outDir: 'dist',
  },
  {
    entry: ['src/index.ts'],
    format: ['esm'],
    outExtension: () => ({ js: '.js' }),
    noExternal: [/(.*)/], // Bundle all NPM dependencies
    external: [/^node:/], // Leave Node.js native built-ins external
    outDir: '../../../desktop-extension',
    dts: false,
    sourcemap: false,
    clean: false, // Don't clean the whole dir (preserves icon and manifest)
  }
]);