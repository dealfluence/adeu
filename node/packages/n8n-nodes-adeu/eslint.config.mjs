// FILE: node/packages/n8n-nodes-adeu/eslint.config.mjs
import { n8nCommunityNodesPlugin } from "@n8n/eslint-plugin-community-nodes";
import tsParser from "@typescript-eslint/parser";

export default [
  {
    ignores: ["dist/**", "node_modules/**", "test/**", "**/*.test.ts"],
  },
  {
    files: ["nodes/**/*.ts"],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: 2022,
      sourceType: "module",
    },
  },
  n8nCommunityNodesPlugin.configs.recommended,
  {
    files: ["nodes/**/*.ts"],
    rules: {
      // Override to allow bundling @adeu/core natively at build time
      "@n8n/community-nodes/no-restricted-imports": "off",
    },
  },
];
