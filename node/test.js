// FILE: verify_bug2.js
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  DocumentObject,
  RedlineEngine,
  extractTextFromBuffer,
} from "./packages/core/dist/index.js";

async function main() {
  const inputPath = "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex.docx";
  const outPath =
    "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex_Bug2_Cleaned.docx";

  console.log(`Loading ${inputPath}...`);
  const buf = readFileSync(inputPath);
  const doc = await DocumentObject.load(buf);

  const engine = new RedlineEngine(doc, "Bug Tester");

  console.log("Injecting multi-line tracked change with \\n\\n ...");
  engine.process_batch([
    {
      type: "modify",
      target_text: "Mutual Confidentiality Agreement",
      new_text:
        "Mutual Confidentiality Contract\n\n# New Added Section\n\nMore text added here.",
      comment: "Testing markdown injection with double newlines",
    },
  ]);

  const outBuf = await doc.save();
  writeFileSync(outPath, outBuf);
  console.log(`Saved document to ${outPath}`);

  // Extract clean text to verify layout
  const text = await extractTextFromBuffer(outBuf, true);

  // extractTextFromBuffer joins blocks with \n\n.
  // If an empty paragraph was created, we will see \n\n\n\n.
  const hasEmptyParagraph = text.includes("\n\n\n\n");

  console.log("\n--- DIAGNOSTICS ---");
  if (hasEmptyParagraph) {
    console.error(
      "❌ BUG-2 is still present: Extra empty paragraphs detected in the extracted text.",
    );
  } else {
    console.log(
      "✅ PASS: No extra empty paragraphs detected. \\n\\n correctly collapsed.",
    );
  }
}

main().catch(console.error);
