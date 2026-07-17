import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { parseXml, serializeXml } from "./docx/dom.js";
import { create_unified_diff } from "./diff.js";
import { extract_outline } from "./outline.js";
import { paginate } from "./pagination.js";

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
    expect(caught.message).toContain("Provide more surrounding context");
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

  it("BUG-BOM-1: parseXml successfully strips leading UTF-8 BOM (\\uFEFF)", () => {
    const rawXml = `\uFEFF<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:document>`;
    const docXml = parseXml(rawXml);
    expect(docXml.documentElement.tagName).toBe("w:document");
  });
  it("BUG-11b: Sweeps orphaned comment anchors when accepting tracked changes", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Confidential Information");
    const engine = new RedlineEngine(doc, "Reviewer");

    // Add a tracked change with a comment attached
    engine.process_batch([
      {
        type: "modify",
        target_text: "Confidential Information",
        new_text: "Confidential Data",
        comment: "Changed term",
      },
    ]);

    let xml = doc.element.toString();
    expect(xml).toContain("w:commentRangeStart");
    expect(xml).toContain("w:commentReference");

    // Accept it
    engine.accept_all_revisions();

    xml = doc.element.toString();
    // Assert clean up
    expect(xml).not.toContain("w:commentRangeStart");
    expect(xml).not.toContain("w:commentReference");
  });

  it("BUG-2: Collapses multiple newlines to prevent empty paragraphs", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Section 1");
    const engine = new RedlineEngine(doc, "Reviewer");

    engine.process_batch([
      {
        type: "modify",
        target_text: "Section 1",
        new_text: "Section 1\n\n# Section 2\n\nSection 3",
      },
    ]);

    const buf = await doc.save();
    const cleanText = await extractTextFromBuffer(buf, true);

    // We shouldn't see four newlines in a row
    expect(cleanText).toContain("Section 1\n\n# Section 2\n\nSection 3");
    expect(cleanText).not.toContain("\n\n\n\n");
  });

  it("BUG-3: Outline reader gracefully falls back to style_id for headings missing in cache", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "Dynamically Assigned Heading");

    // Force a heading style without explicitly putting it in a styles.xml cache
    const docEl = p.ownerDocument!;
    const pPr = docEl.createElement("w:pPr");
    const pStyle = docEl.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "Heading2");
    pPr.appendChild(pStyle);
    p.insertBefore(pPr, p.firstChild);

    const buf = await doc.save();
    const body = await extractTextFromBuffer(buf, false);
    const pages = paginate(body, "");

    const outlineNodes = extract_outline(
      doc,
      body,
      pages.body_pages,
      pages.body_page_offsets,
    );

    expect(outlineNodes.length).toBe(1);
    expect(outlineNodes[0].text).toBe("Dynamically Assigned Heading");
    expect(outlineNodes[0].level).toBe(2);
  });

  it("BUG-9b: Enforces strict timeout on pathologically complex diffs to prevent hanging", () => {
    // Create highly complex, repetitive, slightly altered text that induces O(N^2) explosion
    const base = "The quick brown fox jumps over the lazy dog. ".repeat(200);
    const mod = base.replace(/e/g, "E").replace(/a/g, "A").replace(/o/g, "O");

    const start = Date.now();
    const diff = create_unified_diff(base, mod);
    const elapsed = Date.now() - start;

    // Should finish well under 5 seconds (target is ~2.0s due to timeout, + setup overhead)
    expect(elapsed).toBeLessThan(5000);
    expect(diff.length).toBeGreaterThan(0);
  });

  it("BUG-2.1: _track_insert_inline with empty string returns null instead of empty <w:ins>", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Target word.");
    const engine = new RedlineEngine(doc);

    // Call internal method directly via any to match Python parity test
    const ins = (engine as any)._build_tracked_ins_for_line(
      "",
      null,
      "123",
      doc.element.ownerDocument!,
    );
    expect(ins).toBeNull();
  });

  it("BUG-3.1: Outline reader detects inherited outlineLvl from style cache", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "Short heading");

    const fakeCache = {
      Heading3: { name: "Heading 3", outline_level: 2, bold: true },
    };
    (doc.pkg as any)._adeu_style_cache = [fakeCache, "Normal"];

    const docEl = p.ownerDocument!;
    const pPr = docEl.createElement("w:pPr");
    const pStyle = docEl.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "Heading3");
    pPr.appendChild(pStyle);
    p.insertBefore(pPr, p.firstChild);

    const buf = await doc.save();
    const body = await extractTextFromBuffer(buf, false);
    const pages = paginate(body, "");

    const outlineNodes = extract_outline(
      doc,
      body,
      pages.body_pages,
      pages.body_page_offsets,
    );

    expect(outlineNodes.length).toBe(1);
    expect(outlineNodes[0].text).toBe("Short heading");
    expect(outlineNodes[0].level).toBe(3);
  });

  it("VAL-OBS-NEW-5: Orphaned comment anchors spanning redlines are swept on accept", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "");
    const engine = new RedlineEngine(doc);

    const c_id = engine.comments_manager.addComment("Test", "Spanning comment");
    const xmlDoc = doc.element.ownerDocument!;

    const start = xmlDoc.createElement("w:commentRangeStart");
    start.setAttribute("w:id", c_id);
    p.appendChild(start);

    const del_tag = xmlDoc.createElement("w:del");
    del_tag.setAttribute("w:id", "1");
    p.appendChild(del_tag);

    const ins_tag = xmlDoc.createElement("w:ins");
    ins_tag.setAttribute("w:id", "1");
    p.appendChild(ins_tag);

    const end = xmlDoc.createElement("w:commentRangeEnd");
    end.setAttribute("w:id", c_id);
    p.appendChild(end);

    const ref_run = xmlDoc.createElement("w:r");
    const ref = xmlDoc.createElement("w:commentReference");
    ref.setAttribute("w:id", c_id);
    ref_run.appendChild(ref);
    p.appendChild(ref_run);

    engine.accept_all_revisions();

    const xml = doc.element.toString();
    expect(xml).not.toContain("w:commentRangeStart");
    expect(xml).not.toContain("w:commentRangeEnd");
    expect(xml).not.toContain("w:commentReference");
  });

  it("BUG-DOM-1: Safely handles multi-paragraph replace with heading and comment without throwing DOM errors", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is the old text that will be replaced.");
    const engine = new RedlineEngine(doc, "Reviewer");

    // This specific combination caused a "child not in parent" DOM error in Node:
    // 1. Modifying text
    // 2. new_text has multiple paragraphs (\n\n)
    // 3. new_text includes a markdown heading (##) which triggers block mode
    // 4. A comment is attached
    expect(() => {
      engine.process_batch([
        {
          type: "modify",
          target_text: "old text that will be replaced.",
          new_text: "new introduction\n\n## Section 1\n\nNew paragraph content",
          comment: "Restructuring this section",
        },
      ]);
    }).not.toThrow();

    const xml = doc.element.toString();
    expect(xml).toContain("w:commentRangeStart");
    expect(xml).toContain("w:commentRangeEnd");
    expect(xml).toContain("Section 1");
  });

  it("BUG-OUTLINE-1: Normalizes lowercase 'heading N' style names to 'Heading N' for parity", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "My lowercase heading");

    // Force a heading style with lowercase name in styles.xml
    const docEl = p.ownerDocument!;
    const pPr = docEl.createElement("w:pPr");
    const pStyle = docEl.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "heading1");
    pPr.appendChild(pStyle);
    p.insertBefore(pPr, p.firstChild);

    // Mock the styles.xml
    const stylesXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
      <w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:style w:type="paragraph" w:styleId="heading1">
          <w:name w:val="heading 1"/>
          <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
        </w:style>
      </w:styles>`;

    const existingStyles = doc.pkg.getPartByPath("word/styles.xml");
    if (existingStyles) {
      existingStyles._element = parseXml(stylesXml).documentElement;
    } else {
      const stylesPart = doc.pkg.addPart(
        "/word/styles.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
        stylesXml,
      );
      doc.relateTo(
        stylesPart,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
      );
    }

    const buf = await doc.save();
    const body = await extractTextFromBuffer(buf, false);
    const pages = paginate(body, "");

    const outlineNodes = extract_outline(
      doc,
      body,
      pages.body_pages,
      pages.body_page_offsets,
    );

    expect(outlineNodes.length).toBe(1);
    expect(outlineNodes[0].style).toBe("Heading 1"); // Instead of (outline_level)
    expect(outlineNodes[0].text).toBe("My lowercase heading");
  });

  it("BUG-EXPLORE-1: Full-paragraph deletion safely removes the paragraph mark", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Paragraph 1");
    const pTarget = addParagraph(doc, "Target paragraph to delete.");
    addParagraph(doc, "Paragraph 3");

    const engine = new RedlineEngine(doc, "Reviewer");
    engine.process_batch([
      {
        type: "modify",
        target_text: "Target paragraph to delete.",
        new_text: "",
      },
    ]);

    engine.accept_all_revisions();

    // If the paragraph mark <w:del> was successfully injected into pPr/rPr,
    // accept_all_revisions will completely remove the <w:p> element.
    // If the bug is present, the <w:p> will survive as an empty orphaned node.
    expect(pTarget.parentNode).toBeNull();
  });

  it("BUG-EXPLORE-2: a strict edit inside a foreign author's insertion applies (nested change)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Original baseline.");

    // Author A makes an insertion
    const engineA = new RedlineEngine(doc, "Author A");
    engineA.process_batch([
      {
        type: "modify",
        target_text: "Original baseline.",
        new_text: "Original baseline. Inserted by A.",
      },
    ]);

    // Author B modifies Author A's pending insertion — this now applies: the
    // enclosing <w:ins> is split and Author B's change nested inside it.
    const engineB = new RedlineEngine(doc, "Author B");
    const result = engineB.process_batch([
      {
        type: "modify",
        target_text: "Inserted by A.",
        new_text: "Modified by B.",
      },
    ]);
    expect(result.edits_applied).toBe(1);
    expect(result.edits_skipped).toBe(0);
  });

  it("BUG-CROSS-PARA-1: Cross-paragraph modify coalesces paragraphs and tracks para-mark deletion", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Clause 1 ends here.");
    addParagraph(doc, "Clause 2 begins here.");
    const engine = new RedlineEngine(doc, "Reviewer");

    engine.process_batch([
      {
        type: "modify",
        target_text: "ends here.\n\nClause 2 begins",
        new_text: "ends here. MERGED",
      },
    ]);

    engine.accept_all_revisions();

    const buf = await doc.save();
    const cleanText = await extractTextFromBuffer(buf, true);

    expect(cleanText).not.toContain("ends here.\n\n");
    expect(cleanText).toContain("Clause 1 ends here. MERGED here.");
  });

  it("BUG-CROSS-PARA-3: 3-paragraph modify cleanly merges bottom-up without leaving orphans", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Paragraph 1 ends here.");
    addParagraph(doc, "Paragraph 2 is in the middle.");
    addParagraph(doc, "Paragraph 3 begins here.");
    const engine = new RedlineEngine(doc, "Reviewer");

    engine.process_batch([
      {
        type: "modify",
        target_text:
          "ends here.\n\nParagraph 2 is in the middle.\n\nParagraph 3 begins",
        new_text: "ends here. MERGED",
      },
    ]);

    engine.accept_all_revisions();
    const cleanText = await extractTextFromBuffer(await doc.save(), true);

    expect(cleanText).not.toContain("Paragraph 2");
    expect(cleanText).toContain("Paragraph 1 ends here. MERGED here.");
  });

  it("BUG-REPRO: accept_all_revisions leaks comments and in-body comment anchors", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is the original text of the agreement.");
    const engine = new RedlineEngine(doc, "Reviewer AI");

    // Add a tracked change with a comment attached
    engine.process_batch([
      {
        type: "modify",
        target_text: "original text",
        new_text: "updated text",
        comment: "Should this be updated or kept as original?",
      },
    ]);

    // Pre-condition check: comment parts exist
    const original_comment_parts = doc.pkg.parts.filter(p => p.contentType.includes("comments"));
    expect(original_comment_parts.length).toBeGreaterThan(0);

    const original_xml = doc.element.toString();
    expect(original_xml).toContain("w:commentRangeStart");
    expect(original_xml).toContain("w:commentReference");

    // Accept all
    engine.accept_all_revisions();

    // Verify comment removal
    const final_xml = doc.element.toString();
    
    // Assert NO in-body comment anchors survive (anchors must be completely gone)
    expect(final_xml).not.toContain("w:commentRangeStart");
    expect(final_xml).not.toContain("w:commentRangeEnd");
    expect(final_xml).not.toContain("w:commentReference");

    const final_comment_parts = doc.pkg.parts.filter(p => p.contentType.includes("comments"));
    expect(final_comment_parts.length).toBe(0);
  });

  it("Double-Serialization Core: process_batch successfully processes double-serialized JSON strings", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "original text");
    const engine = new RedlineEngine(doc);

    // On unpatched code, this will throw a raw TypeError: Cannot create property '_applied_status' on string
    // On patched code, it will successfully parse and apply the modify change
    engine.process_batch([
      JSON.stringify({
        type: "modify",
        target_text: "original text",
        new_text: "updated text",
      }),
    ] as any);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, true);
    expect(text).toContain("updated text");
  });

  it("Unparseable String Core: process_batch throws validation error instead of TypeError for raw strings", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "original text");
    const engine = new RedlineEngine(doc);

    let caught: any = null;
    try {
      engine.process_batch([
        "modify original text to updated text"
      ] as any);
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeDefined();
    expect(caught.name).toBe("BatchValidationError");
    expect(caught.message).toContain("Invalid change format");
  });
});
