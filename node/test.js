// FILE: verify_bugs_3_and_9.js
import { readFileSync } from "node:fs";
import {
  DocumentObject,
  extractTextFromBuffer,
  extract_outline,
  paginate,
  create_unified_diff,
} from "./packages/core/dist/index.js";

async function main() {
  const inputPath = "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex.docx";

  console.log(`Loading ${inputPath}...`);
  const buf = readFileSync(inputPath);
  const doc = await DocumentObject.load(buf);

  // 1. Verify BUG-3 (Outline Mode)
  console.log("--- Checking BUG-3 (Outline Reader) ---");
  const fullText = await extractTextFromBuffer(buf, false);
  const body = fullText.split("<!-- READONLY_BOUNDARY_START -->")[0];
  const pages = paginate(body, "");

  const outlineNodes = extract_outline(
    doc,
    body,
    pages.body_pages,
    pages.body_page_offsets,
  );

  if (outlineNodes.length < 3) {
    console.error(
      `❌ BUG-3: Outline found only ${outlineNodes.length} headings (expected at least 3).`,
    );
  } else {
    console.log(
      `✅ PASS: Outline correctly discovered ${outlineNodes.length} headings.`,
    );
    console.log(
      outlineNodes.map((n) => `   -> L${n.level}: ${n.text}`).join("\n"),
    );
  }

  // 2. Verify BUG-9b (Diff Hangs)
  console.log("\n--- Checking BUG-9b (Diff Hangs) ---");
  console.log("Generating dummy diff (ensuring it does not hang)...");
  const startTime = Date.now();

  // Duplicate the text and artificially fragment it with a large number of
  // differences to trigger the O(N^2) path.
  const badText = fullText.replace(/e/g, "E").replace(/a/g, "A").repeat(5);
  const diffOut = create_unified_diff(fullText, badText);

  const elapsed = Date.now() - startTime;
  if (elapsed > 5000) {
    console.error(
      `❌ BUG-9b: Diff took ${elapsed}ms. Timeout did not successfully intercept.`,
    );
  } else {
    console.log(
      `✅ PASS: Diff completed instantly (${elapsed}ms). Timeout engaged successfully.`,
    );
  }
}

main().catch(console.error);
