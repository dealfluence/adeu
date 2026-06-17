import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const serverBundlePath = resolve(__dirname, "../dist/index.js");
const coreBundlePath = resolve(__dirname, "../../core/dist/index.js");

if (!existsSync(serverBundlePath)) {
  console.error(`Error: Compiled server bundle not found at ${serverBundlePath}. Run 'npm run build' first.`);
  process.exit(1);
}

if (!existsSync(coreBundlePath)) {
  console.error(`Error: Compiled core bundle not found at ${coreBundlePath}. Run 'npm run build' first.`);
  process.exit(1);
}

const serverContent = readFileSync(serverBundlePath, "utf-8");
const coreContent = readFileSync(coreBundlePath, "utf-8");

// Sentinel patterns that MUST be present in the compiled core bundle
const coreSentinels = [
  "matches text inside a tracked deletion",
  "Reject/accept that change first",
  "_heading_passes_quality_filter_fast",
];

let failed = false;

// 1. Verify server bundle is present and imports @adeu/core
if (!serverContent.includes("@adeu/core")) {
  console.error("Error: Server bundle does not import '@adeu/core'!");
  failed = true;
}

// 2. Verify core bundle has the actual parity fixes
for (const sentinel of coreSentinels) {
  if (!coreContent.includes(sentinel)) {
    console.error(`Error: Core dependency bundle is missing critical sentinel string: "${sentinel}"`);
    failed = true;
  }
}

if (failed) {
  console.error("Bundle verification FAILED! The bundle might be stale or built without required dependencies.");
  process.exit(1);
}

console.log("Bundle verification passed! All sentinel checks succeeded against server and core bundles.");
