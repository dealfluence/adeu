import { createRequire } from "module";
import { readFileSync, readdirSync, statSync } from "fs";
import { resolve, join } from "path";

const require = createRequire(import.meta.url);
const ts = require(resolve("node/node_modules/typescript"));

function getAllTsFiles(dir) {
  let results = [];
  const list = readdirSync(dir);
  for (const file of list) {
    const full = join(dir, file);
    if (statSync(full).isDirectory()) {
      results = results.concat(getAllTsFiles(full));
    } else if (full.endsWith(".ts") && !full.endsWith(".test.ts") && !full.endsWith(".d.ts")) {
      results.push(full);
    }
  }
  return results;
}

const files = [
  ...getAllTsFiles("node/packages/core/src"),
  ...getAllTsFiles("node/packages/mcp-server/src")
];

const privateFns = new Map();

for (const file of files) {
  const code = readFileSync(file, "utf-8");
  const sourceFile = ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true);

  function walk(node) {
    if (ts.isMethodDeclaration(node) || ts.isFunctionDeclaration(node)) {
      if (node.name && ts.isIdentifier(node.name)) {
        const name = node.name.text;
        if (name.startsWith("_") && !name.startsWith("__")) {
          privateFns.set(name, (privateFns.get(name) || 0));
        }
      }
    }
    ts.forEachChild(node, walk);
  }
  walk(sourceFile);
}

// Count occurrences across all source files
for (const file of files) {
  const code = readFileSync(file, "utf-8");
  for (const [name] of privateFns.entries()) {
    const regex = new RegExp(`\\b${name}\\b`, "g");
    const matches = (code.match(regex) || []).length;
    privateFns.set(name, privateFns.get(name) + matches);
  }
}

console.log("=== Unused or Single-Occurrence Private TS Helpers ===");
for (const [name, count] of privateFns.entries()) {
  if (count <= 1) {
    console.log(`  ${name}: ${count} total occurrence(s) (Dead function candidate)`);
  }
}
