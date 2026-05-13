// FILE: test_bug9.mjs
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { readFileSync, writeFileSync } from "node:fs";
import {
  DocumentObject,
  RedlineEngine,
} from "../node/packages/core/dist/index.js";

async function run() {
  const docPath = "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex_clean.docx";
  const dirtyPath =
    "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex_dirty.docx";

  // 1. Create a dirty document with tracked changes
  console.log("1. Creating dirty fixture...");
  const buf = readFileSync(docPath);
  const doc = await DocumentObject.load(buf);
  const engine = new RedlineEngine(doc);
  engine.process_batch([
    { type: "modify", target_text: "entered into", new_text: "agreed to" },
  ]);
  const dirtyBuf = await doc.save();
  writeFileSync(dirtyPath, dirtyBuf);

  // 2. Boot Server
  const serverPath = resolve("./packages/mcp-server/dist/index.js");
  console.log("2. Booting MCP server...");
  const child = spawn("node", [serverPath]);

  child.stdout.on("data", (data) => {
    const lines = data.toString().trim().split("\n");
    for (const line of lines) {
      if (!line.startsWith("{")) continue;
      try {
        const response = JSON.parse(line);
        if (response.id === 1) {
          const text = response.result.content[0].text;
          console.log("\n--- diff_docx_files (compare_clean=true) ---");
          if (text.includes("agreed to") && !text.includes("{++")) {
            console.log("✅ PASS: Clean diff produced.");
          } else {
            console.error(
              "❌ FAIL: Diff contains CriticMarkup or missing text.",
              text.substring(0, 200),
            );
          }

          // Send request 2
          child.stdin.write(JSON.stringify(req2) + "\n");
        } else if (response.id === 2) {
          const text = response.result.content[0].text;
          console.log("\n--- diff_docx_files (compare_clean=false) ---");
          if (text.includes("{++agreed to++}")) {
            console.log("✅ PASS: Raw CriticMarkup diff produced.");
          } else {
            console.error(
              "❌ FAIL: Diff missing CriticMarkup.",
              text.substring(0, 200),
            );
          }
          child.kill();
          process.exit(0);
        }
      } catch (e) {}
    }
  });

  const req1 = {
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: {
      name: "diff_docx_files",
      arguments: {
        original_path: docPath,
        modified_path: dirtyPath,
        compare_clean: true,
      },
    },
  };

  const req2 = {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "diff_docx_files",
      arguments: {
        original_path: docPath,
        modified_path: dirtyPath,
        compare_clean: false,
      },
    },
  };

  child.stdin.write(JSON.stringify(req1) + "\n");
}

run().catch(console.error);
