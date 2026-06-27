// Release consistency checker (zero-dependency, single source of truth).
//
// NOTE: intentionally NO `#!/usr/bin/env node` shebang. This module is imported
// by node/packages/n8n-nodes-adeu/test/codex-consistency.test.ts, and Vite/
// vitest's transform of an imported .mjs does not strip a leading shebang,
// which makes V8 throw "SyntaxError: Invalid or unexpected token" at load time
// (0 tests collected). The script is always run as `node scripts/<file>.mjs`
// (see release.yml / bump.py), so the shebang was never needed.
//
// Two independent guarantees, both of which have bitten us before:
//
//   1. n8n codex integrity — nodes/Adeu/Adeu.node.json must agree with the
//      node class in Adeu.node.ts. n8n Cloud verification rejects a codex
//      whose `nodeVersion` does not mirror the node's runtime `version`, or
//      whose `codexVersion` is anything but the schema version "1.0".
//   2. Monorepo version lockstep — every manifest bumped by scripts/bump.py
//      must carry the exact same semantic version.
//
// Run directly to gate a release (exits non-zero on any failure):
//     node scripts/check_release_consistency.mjs
// Or import { runChecks } from a test to enforce it in CI / pre-push.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Valid n8n node categories (see node-codex-files docs). Keep in sync with
// https://docs.n8n.io/integrations/creating-nodes/build/reference/node-codex-files/
const VALID_CATEGORIES = new Set([
  "Data & Storage",
  "Finance & Accounting",
  "Marketing & Content",
  "Productivity",
  "Miscellaneous",
  "Sales",
  "Development",
  "Analytics",
  "Communication",
  "Utility",
]);

// Manifests synchronized to one version by scripts/bump.py. KEEP IN SYNC with
// FILES_TO_BUMP there — this check is what catches it if they drift.
const VERSIONED_MANIFESTS = [
  "python/pyproject.toml",
  "langchain/pyproject.toml",
  "node/packages/core/package.json",
  "node/packages/mcp-server/package.json",
  "desktop-extension/manifest.json",
  "gemini-extension.json",
  "python/server.json",
  "node/packages/n8n-nodes-adeu/package.json",
];

const N8N_DIR = "node/packages/n8n-nodes-adeu";

function readVersionFromManifest(text, filepath) {
  // .toml uses `version = "x"`; .json uses `"version": "x"`.
  const re = filepath.endsWith(".toml")
    ? /^version\s*=\s*"([^"]+)"/m
    : /"version"\s*:\s*"([^"]+)"/;
  const m = text.match(re);
  return m ? m[1] : null;
}

/**
 * Run all release-consistency checks against a repo root.
 * @returns {{errors: string[]}}
 */
export function runChecks(repoRoot) {
  const errors = [];
  const read = (rel) => readFileSync(resolve(repoRoot, rel), "utf8");

  // --- Check 1: n8n codex integrity ---------------------------------------
  try {
    const nodeTs = read(`${N8N_DIR}/nodes/Adeu/Adeu.node.ts`);
    const codex = JSON.parse(read(`${N8N_DIR}/nodes/Adeu/Adeu.node.json`));
    const pkg = JSON.parse(read(`${N8N_DIR}/package.json`));

    const nameMatch = nodeTs.match(/name:\s*"([^"]+)"/);
    const versionMatch = nodeTs.match(/version:\s*(\d+(?:\.\d+)?)/);

    if (!nameMatch || !versionMatch) {
      errors.push(
        "codex: could not parse `name`/`version` from Adeu.node.ts — update the checker if the node class changed shape.",
      );
    } else {
      const nodeName = nameMatch[1];
      const nodeVersion = parseFloat(versionMatch[1]);

      // nodeVersion mirrors the class version, formatted x.y (e.g. 1 -> "1.0").
      if (!/^\d+\.\d+$/.test(String(codex.nodeVersion))) {
        errors.push(
          `codex.nodeVersion must be an "x.y" string, got ${JSON.stringify(codex.nodeVersion)}.`,
        );
      }
      if (parseFloat(codex.nodeVersion) !== nodeVersion) {
        errors.push(
          `codex.nodeVersion (${JSON.stringify(codex.nodeVersion)}) must mirror Adeu.node.ts version (${nodeVersion}). ` +
            `It is NOT the npm package version — do not sync it to ${pkg.version}.`,
        );
      }

      if (codex.codexVersion !== "1.0") {
        errors.push(
          `codex.codexVersion must be the schema version "1.0", got ${JSON.stringify(codex.codexVersion)}.`,
        );
      }

      const expectedNode = `n8n-nodes-adeu.${nodeName}`;
      if (codex.node !== expectedNode) {
        errors.push(
          `codex.node must be "${expectedNode}" (package "${pkg.name}" + node name "${nodeName}"), got ${JSON.stringify(codex.node)}.`,
        );
      }
    }

    if (!Array.isArray(codex.categories) || codex.categories.length === 0) {
      errors.push("codex.categories must be a non-empty array.");
    } else {
      for (const c of codex.categories) {
        if (!VALID_CATEGORIES.has(c)) {
          errors.push(
            `codex.categories has invalid entry ${JSON.stringify(c)} — must be one of: ${[...VALID_CATEGORIES].join(", ")}.`,
          );
        }
      }
    }
  } catch (e) {
    errors.push(`codex: failed to read/parse n8n files — ${e.message}`);
  }

  // --- Check 2: monorepo version lockstep ---------------------------------
  const versions = {};
  for (const rel of VERSIONED_MANIFESTS) {
    try {
      const v = readVersionFromManifest(read(rel), rel);
      if (!v) {
        errors.push(`lockstep: no version found in ${rel}.`);
      } else {
        versions[rel] = v;
      }
    } catch (e) {
      errors.push(`lockstep: failed to read ${rel} — ${e.message}`);
    }
  }
  const distinct = [...new Set(Object.values(versions))];
  if (distinct.length > 1) {
    const detail = Object.entries(versions)
      .map(([f, v]) => `    ${v}  ${f}`)
      .join("\n");
    errors.push(
      `lockstep: manifests are not all on the same version:\n${detail}\n  Run scripts/bump.py to resynchronize.`,
    );
  }

  return { errors };
}

// CLI entry point.
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const { errors } = runChecks(repoRoot);
  if (errors.length) {
    console.error("✖ Release consistency checks FAILED:\n");
    for (const e of errors) console.error(`  • ${e}`);
    console.error("");
    process.exit(1);
  }
  console.log("✓ Release consistency checks passed.");
}
