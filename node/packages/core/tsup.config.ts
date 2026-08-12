import { defineConfig } from 'tsup';
import { readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync("package.json", "utf-8"));

export default defineConfig({
  entry: ['src/index.ts'],
  format: ['cjs', 'esm'],
  dts: false,
  splitting: false,
  sourcemap: true,
  clean: true,
  define: { "process.env.ADEU_CORE_VERSION": JSON.stringify(pkg.version) },
});
