// FILE: test_bug8.mjs
import { readFileSync } from "node:fs";
import {
  DocumentObject,
  RedlineEngine,
} from "../node/packages/core/dist/index.js";

async function run() {
  const fixturePath =
    "C:\\Users\\Uzair\\Desktop\\NDA\\NDA_Acme_Vertex_clean.docx";
  const buf = readFileSync(fixturePath);
  const doc = await DocumentObject.load(buf);

  const engine = new RedlineEngine(doc);

  // 1. Create a parent comment
  console.log("1. Applying edit with comment...");
  engine.process_batch([
    {
      type: "modify",
      target_text: "entered into",
      new_text: "entered into",
      comment: "Parent comment",
    },
  ]);

  // Find the generated comment ID
  const allStarts = doc.element.getElementsByTagName("w:commentRangeStart");
  const parentId = allStarts[0].getAttribute("w:id");
  console.log(`-> Created parent comment with ID: ${parentId}`);

  // 2. Reply to it
  console.log("2. Replying to comment...");
  engine.process_batch([
    { type: "reply", target_id: `Com:${parentId}`, text: "This is a reply!" },
  ]);

  // 3. Verify
  const xml = doc.element.toString();

  const starts = xml.match(/<w:commentRangeStart w:id="\d+"\/>/g) || [];
  const ends = xml.match(/<w:commentRangeEnd w:id="\d+"\/>/g) || [];
  const refs = xml.match(/<w:commentReference w:id="\d+"\/>/g) || [];

  console.log(`Found starts: ${starts.length} (Expected 2)`);
  console.log(`Found ends: ${ends.length} (Expected 2)`);
  console.log(`Found refs: ${refs.length} (Expected 2)`);

  if (starts.length === 2 && ends.length === 2 && refs.length === 2) {
    console.log(
      "✅ PASS: Reply comment range was emitted correctly (1:1 parity with Python).",
    );
  } else {
    console.error("❌ FAIL: Comment markers missing.");
  }
}

run().catch(console.error);
