// FILE: node/packages/n8n-nodes-adeu/tsup.config.ts
import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["nodes/Adeu/Adeu.node.ts"],
  format: ["cjs"],
  outDir: "dist/nodes/Adeu",
  target: "node18",
  clean: true,
  dts: false,
  minify: false,
  sourcemap: false,
  noExternal: [
    "@adeu/core",
    "@xmldom/xmldom",
    "diff-match-patch",
    "jszip",
    "xpath",
  ],
  external: ["n8n-workflow"],
});
