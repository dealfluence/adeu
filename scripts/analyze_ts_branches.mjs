import { createRequire } from "module";
import { readFileSync } from "fs";
import { resolve } from "path";

const require = createRequire(import.meta.url);
const ts = require(resolve("node/node_modules/typescript"));

function analyzeTsFile(filePath) {
  const code = readFileSync(filePath, "utf-8");
  const sourceFile = ts.createSourceFile(
    filePath,
    code,
    ts.ScriptTarget.Latest,
    true
  );

  const functions = [];

  function getFnName(node) {
    if (node.name && ts.isIdentifier(node.name)) {
      return node.name.text;
    }
    if (node.parent && ts.isVariableDeclaration(node.parent) && ts.isIdentifier(node.parent.name)) {
      return node.parent.name.text;
    }
    if (ts.isMethodDeclaration(node) && node.name && ts.isIdentifier(node.name)) {
      return node.name.text;
    }
    return "<anonymous>";
  }

  function visitFn(fnNode) {
    let complexity = 1;
    let ifCount = 0;
    let maxDepth = 0;

    function visitBody(node, depth) {
      let currentDepth = depth;
      if (
        ts.isIfStatement(node) ||
        ts.isForStatement(node) ||
        ts.isForOfStatement(node) ||
        ts.isForInStatement(node) ||
        ts.isWhileStatement(node) ||
        ts.isDoStatement(node) ||
        ts.isCatchClause(node) ||
        ts.isConditionalExpression(node)
      ) {
        complexity++;
        currentDepth++;
        if (ts.isIfStatement(node)) {
          ifCount++;
        }
        if (currentDepth > maxDepth) {
          maxDepth = currentDepth;
        }
      } else if (
        node.kind === ts.SyntaxKind.AmpersandAmpersandToken ||
        node.kind === ts.SyntaxKind.BarBarToken ||
        node.kind === ts.SyntaxKind.QuestionQuestionToken
      ) {
        complexity++;
      }

      ts.forEachChild(node, (child) => visitBody(child, currentDepth));
    }

    if (fnNode.body) {
      visitBody(fnNode.body, 0);
    }

    const line = sourceFile.getLineAndCharacterOfPosition(fnNode.getStart()).line + 1;
    functions.push({
      name: getFnName(fnNode),
      line,
      complexity,
      maxDepth,
      ifCount,
    });
  }

  function walk(node) {
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isFunctionExpression(node) ||
      ts.isArrowFunction(node) ||
      ts.isMethodDeclaration(node)
    ) {
      visitFn(node);
    }
    ts.forEachChild(node, walk);
  }

  walk(sourceFile);

  functions.sort((a, b) => b.complexity - a.complexity);

  console.log(`Analysis for ${filePath}`);
  console.log(
    `${"Line".padEnd(8)} ${"Complexity".padEnd(12)} ${"Max Depth".padEnd(11)} ${"If Count".padEnd(10)} Function`
  );
  console.log("=".repeat(70));
  for (const fn of functions.slice(0, 25)) {
    console.log(
      `${String(fn.line).padEnd(8)} ${String(fn.complexity).padEnd(12)} ${String(fn.maxDepth).padEnd(11)} ${String(fn.ifCount).padEnd(10)} ${fn.name}`
    );
  }
}

analyzeTsFile(process.argv[2] || "node/packages/core/src/engine.ts");
