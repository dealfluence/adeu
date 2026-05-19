// FILE: node/packages/n8n-nodes-adeu/vitest.config.ts
import { defineConfig } from "vitest/config";
import { resolve } from "path";

export default defineConfig({
  resolve: {
    alias: {
      "@node": resolve(__dirname, "nodes"),
    },
  },
  test: {
    environment: "node",
    globals: true,
    env: {
      ADEU_FIXTURES: resolve(__dirname, "../../../shared/fixtures"),
    },
  },
});
