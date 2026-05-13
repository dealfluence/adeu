// FILE: node/packages/core/src/engine.bugs.test.ts
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { parseXml, serializeXml } from "./docx/dom.js";

describe("Resolved Bugs Core Engine Verification", () => {
  it("BUG-3 & BUG-4: Links parts to package and yields headers for extraction", async () => {
    const doc = await createTestDocument();

    // Inject a raw header part
    const xml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
      <w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:p><w:r><w:t>My Secret Header</w:t></w:r></w:p>
      </w:hdr>`;

    const headerPart = doc.pkg.addPart(
      "/word/header1.xml",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
      xml,
    );
    doc.relateTo(
      headerPart,
      "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
    );

    // BUG-3a Fix: Ensure part.package is assigned so style cache traversal works
    expect(headerPart.package).toBe(doc.pkg);

    // BUG-3b/4 Fix: Ensure headers are yielded by iter_document_parts and extracted
    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);
    expect(text).toContain("My Secret Header");
  });

  it("BUG-6: Provides context snippets for ambiguous matches", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "the apple is on the table, the dog is in the yard.");

    const engine = new RedlineEngine(doc);
    let caught: any = null;

    try {
      engine.process_batch([
        { type: "modify", target_text: "the", new_text: "THE" },
      ]);
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeDefined();
    expect(caught.name).toBe("BatchValidationError");
    expect(caught.message).toContain(
      "Ambiguous match. Target text appears 4 times",
    );
    expect(caught.message).toContain("[the]"); // Ensure the matched text is bracketed
    expect(caught.message).toContain("Please provide more surrounding context");
  });

  it("BUG-7: Unifies review-action and text-edit validation errors in a single pass", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Base text");
    const engine = new RedlineEngine(doc);

    let caught: any = null;
    try {
      engine.process_batch([
        { type: "accept", target_id: "Chg:999" },
        { type: "modify", target_text: "MISSING_TEXT", new_text: "found" },
      ]);
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeDefined();
    expect(caught.name).toBe("BatchValidationError");
    // Both errors should be accumulated and thrown together
    expect(caught.message).toContain("Target ID Chg:999 not found");
    expect(caught.message).toContain("Target text not found");
    expect(caught.message).toContain("MISSING_TEXT");
  });

  it("BUG-8: Emits full commentRange wrappers for comment replies (1:1 Python Parity)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Hello world.");
    const engine = new RedlineEngine(doc);

    // Create parent comment
    engine.process_batch([
      {
        type: "modify",
        target_text: "world",
        new_text: "world",
        comment: "Parent",
      },
    ]);

    const xml1 = doc.element.toString();
    const starts1 = (xml1.match(/<w:commentRangeStart/g) || []).length;
    expect(starts1).toBe(1); // 1 parent comment

    // Find the dynamic comment ID (usually 1 in a fresh document)
    const parentIdMatch = xml1.match(/<w:commentRangeStart w:id="(\d+)"\/>/);
    expect(parentIdMatch).not.toBeNull();
    const parentId = parentIdMatch![1];

    // Issue reply
    engine.process_batch([
      { type: "reply", target_id: `Com:${parentId}`, text: "Reply" },
    ]);

    const xml2 = doc.element.toString();
    const starts2 = (xml2.match(/<w:commentRangeStart/g) || []).length;
    const ends2 = (xml2.match(/<w:commentRangeEnd/g) || []).length;
    const refs2 = (xml2.match(/<w:commentReference/g) || []).length;

    // Both starts, ends, and refs should have incremented by exactly 1
    expect(starts2).toBe(starts1 + 1);
    expect(ends2).toBe(starts1 + 1);
    expect(refs2).toBe(starts1 + 1);
  });

  it("BUG-11: Deterministically sorts root XML attributes strictly by ASCII", () => {
    // We intentionally place standard attributes before namespaces, and w10 after w.
    const rawXml = `<w:document b="2" xmlns:w10="urn:w10" a="1" xmlns:w="urn:w" mc:Ignorable="w14" xmlns:mc="urn:mc"></w:document>`;
    const docXml = parseXml(rawXml);

    const serialized = serializeXml(docXml.documentElement);

    const expected = `<w:document xmlns:mc="urn:mc" xmlns:w="urn:w" xmlns:w10="urn:w10" a="1" b="2" mc:Ignorable="w14"/>`;
    // Direct string equality so Vitest prints the exact diff if they mismatch!
    expect(serialized).toBe(expected);
  });
});
