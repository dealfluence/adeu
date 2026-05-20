// FILE: node/packages/n8n-nodes-adeu/tsup.config.ts
import { defineConfig } from "tsup";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EMPTY_SHIM = resolve(__dirname, "shims", "empty.js");
const MODULE_SHIM = resolve(__dirname, "shims", "module.js");

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
      module: MODULE_SHIM,
      worker_threads: MODULE_SHIM,
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
