// FILE: node/packages/mcp-server/tsup.config.ts
import { defineConfig } from "tsup";
import { cpSync, existsSync } from "node:fs";
import { join } from "node:path";

function copyAssets(outDir: string) {
  if (existsSync("src/templates")) {
    cpSync("src/templates", join(outDir, "templates"), {
      recursive: true,
      force: true,
    });
  }
  if (existsSync("src/assets")) {
    cpSync("src/assets", join(outDir, "assets"), {
      recursive: true,
      force: true,
    });
  }
}

import { execSync } from "node:child_process";

let gitSha = "unknown";
try {
  gitSha = execSync("git rev-parse --short HEAD", { encoding: "utf-8" }).trim();
} catch (e) {
  // fallback if not in git repo or git not found
}
const buildTimestamp = new Date().toISOString();

export default defineConfig([
  {
    entry: ["src/index.ts"],
    format: ["esm"],
    dts: true,
    sourcemap: true,
    clean: true,
    outDir: "dist",
    banner: {
      js: "#!/usr/bin/env node",
    },
    define: {
      "process.env.GIT_SHA": JSON.stringify(gitSha),
      "process.env.BUILD_TIMESTAMP": JSON.stringify(buildTimestamp),
    },
    onSuccess: async () => {
      copyAssets("dist");
    },
  },
  {
    entry: ["src/index.ts"],
    format: ["esm"],
    outExtension: () => ({ js: ".js" }),
    noExternal: [/(.*)/], // Bundle all NPM dependencies
    external: [/^node:/], // Leave Node.js native built-ins external
    outDir: "../../../desktop-extension",
    dts: false,
    sourcemap: false,
    clean: false, // Don't clean the whole dir (preserves icon and manifest)
    define: {
      "process.env.GIT_SHA": JSON.stringify(gitSha),
      "process.env.BUILD_TIMESTAMP": JSON.stringify(buildTimestamp),
    },
    onSuccess: async () => {
      copyAssets("../../../desktop-extension");
    },
  },
]);
