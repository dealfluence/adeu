// FILE: node/packages/n8n-nodes-adeu/tsup.config.ts
import { defineConfig } from "tsup";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const EMPTY_SHIM = require.resolve("./shims/empty.js");

export default defineConfig({
  entry: ["nodes/Adeu/Adeu.node.ts"],
  format: ["cjs"],
  outDir: "dist/nodes/Adeu",
  target: "node22",
  platform: "node",
  clean: true,
  dts: false,
  minify: true,
  treeshake: true,
  sourcemap: false,
  metafile: true,
  esbuildOptions(options) {
    options.keepNames = true;
    options.alias = {
      ...options.alias,
      process: EMPTY_SHIM,
      "process/browser": EMPTY_SHIM,
      "process/browser.js": EMPTY_SHIM,
      setimmediate: EMPTY_SHIM,
    };
    options.drop = ["console", "debugger"];
    options.legalComments = "none";
    options.mainFields = ["main", "module"];
    options.conditions = ["node", "require", "default"];
  },
  define: {
    "process.browser": "false",
    "process.env.NODE_ENV": '"production"',
  },
  noExternal: [
    "@adeu/core",
    "@xmldom/xmldom",
    "diff-match-patch",
    "fflate",
    "xpath",
  ],
  external: ["n8n-workflow"],
});
