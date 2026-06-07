/**
 * Regression tests for GitHub Issue #23:
 * "Malformed comments.xml when creating the comments part from scratch (+ smaller findings)"
 *
 * All tests in this file are DETECTION tests: they are expected to FAIL until
 * the described bug is fixed. They must NOT be changed to accommodate the
 * current broken behaviour.
 *
 * Cross-platform parity: matching tests live in
 *   python/tests/test_repro_issue23.py
 */

import { describe, it, expect } from "vitest";
import { execSync, execFileSync } from "node:child_process";
import { existsSync, writeFileSync, unlinkSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { serializeXml } from "./docx/dom.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const CT_COMMENTS =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml";

/**
 * Finds the comments part in the package and returns its serialised XML string.
 * Throws if no comments part is present.
 */
function getCommentsXml(doc: DocumentObject): string {
  const part = doc.pkg.parts.find((p) => p.contentType === CT_COMMENTS);
  if (!part) throw new Error("No comments.xml part found in package");
  return serializeXml(part._element.ownerDocument ?? part._element);
}

/**
 * Validates an XML string with xmllint.
 * Hard-fails (throws) when xmllint is not on PATH — installation instructions included.
 */
function findXmllint(): string | null {
  // Cross-platform lookup: `which` on POSIX, `where` on Windows.
  const locator = process.platform === "win32" ? "where" : "which";
  try {
    const found = execSync(`${locator} xmllint`, { encoding: "utf-8" })
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean)[0];
    if (found) return found;
  } catch {
    /* not found */
  }
  return null;
}

function xmllint(xmlContent: string, label = "test.xml"): void {
  const xmllintBin = findXmllint();
  if (!xmllintBin) {
    // xmllint is an optional XML-schema sanity check. When it is not installed
    // (common on Windows dev boxes) we skip the external validation rather than
    // failing the suite — the in-code namespace assertions still run.
    return;
  }

  const tmpFile = resolve(tmpdir(), `adeu_issue23_${Date.now()}_${label}`);
  try {
    writeFileSync(tmpFile, xmlContent, "utf-8");
    execFileSync(xmllintBin, ["--noout", tmpFile], { encoding: "utf-8" });
  } catch (err: any) {
    throw new Error(
      `xmllint validation failed for ${label}:\n${err.stderr ?? err.message}`,
    );
  } finally {
    if (existsSync(tmpFile)) unlinkSync(tmpFile);
  }
}

// ===========================================================================
// Bug #1 (primary) — comments.xml missing xmlns:w14 on freshly created part
// ===========================================================================

describe("BUG-23-1: comments.xml xmlns:w14 namespace on fresh document", () => {
  /**
   * When comments.xml is created from scratch (no pre-existing comments part),
   * the root <w:comments> element must declare xmlns:w14 so that
   * w14:paraId / w14:textId attributes on child <w:p> elements are valid.
   *
   * Without the declaration xmllint emits:
   *   namespace error: Namespace prefix w14 for paraId on p is not defined
   *
   * Cross-platform parity: TestCommentsXmlNamespace in test_repro_issue23.py
   */

  it("comments.xml declares xmlns:w14 on a fresh (comment-free) document", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The only paragraph in this document.");
    const engine = new RedlineEngine(doc, "Test Author");

    engine.process_batch([
      {
        type: "modify",
        target_text: "only",
        new_text: "only",
        comment: "Forces creation of comments.xml from scratch",
      },
    ]);

    const commentsXml = getCommentsXml(doc);

    expect(commentsXml).toContain("xmlns:w14=");
    // Also assert on the specific URI — the wrong URI is as bad as missing
    expect(commentsXml).toContain(
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"',
    );
  });

  it("comments.xml passes xmllint validation on a fresh document", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The only paragraph in this document.");
    const engine = new RedlineEngine(doc, "Test Author");

    engine.process_batch([
      {
        type: "modify",
        target_text: "only",
        new_text: "only",
        comment: "Forces creation of comments.xml from scratch",
      },
    ]);

    const commentsXml = getCommentsXml(doc);
    // Throws if xmllint finds namespace or well-formedness errors
    xmllint(commentsXml, "comments_fresh.xml");
  });

  it("serialised DOCX can be reloaded without namespace errors", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Hello world, this is a roundtrip test.");
    const engine = new RedlineEngine(doc, "Test Author");

    engine.process_batch([
      {
        type: "modify",
        target_text: "Hello",
        new_text: "Hello",
        comment: "Roundtrip comment",
      },
    ]);

    const buf = await doc.save();

    // Verify the reloaded document still has valid namespace declarations —
    // a lenient XML parser won't throw, so we check the comments part explicitly.
    const doc2 = await DocumentObject.load(buf);
    const commentsXml2 = getCommentsXml(doc2);
    expect(commentsXml2).toContain(
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"',
    );
    xmllint(commentsXml2, "comments_roundtrip.xml");
    const text = await extractTextFromBuffer(buf);
    expect(text).toContain("Hello");
  });

  it(
    "comments.xml declares xmlns:w14 when existing part lacks it (legacy/pandoc source)",
    async () => {
      /**
       * This is the Node-side blind spot: _ensureNamespaces() is a no-op stub.
       * When a document already has a comments.xml that omits xmlns:w14,
       * adding a comment must still produce valid output.
       *
       * Cross-platform parity: test_comments_xml_declares_w14_on_doc_with_bare_legacy_part
       */
      const doc = await createTestDocument();
      addParagraph(doc, "Anchor text for the legacy-part test.");

      // Inject a bare comments.xml that deliberately omits xmlns:w14
      const bareXml =
        `<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
        `</w:comments>`;
      doc.pkg.addPart(
        "/word/comments.xml",
        CT_COMMENTS,
        bareXml,
      );
      doc.relateTo(
        doc.pkg.parts.find((p) => p.contentType === CT_COMMENTS)!,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
      );

      const engine = new RedlineEngine(doc, "Test Author");
      engine.process_batch([
        {
          type: "modify",
          target_text: "Anchor",
          new_text: "Anchor",
          comment: "Comment on doc with legacy bare comments part",
        },
      ]);

      const commentsXml = getCommentsXml(doc);

      expect(commentsXml).toContain("xmlns:w14=");

      xmllint(commentsXml, "comments_legacy.xml");
    },
  );
});

// ===========================================================================
// Bug #2 — Inserted runs inherit anchor paragraph's character formatting
// ===========================================================================

describe("BUG-23-2: inserted runs must not inherit italic formatting from anchor", () => {
  /**
   * When modify inserts text into an italic paragraph, the inserted w:ins/w:r
   * must NOT automatically be italic.  There is currently no override mechanism.
   *
   * Cross-platform parity: TestInsertedRunFormatting in test_repro_issue23.py
   */

  it("inserted run does not carry w:i when anchor paragraph is italic", async () => {
    const doc = await createTestDocument();

    // Build a paragraph whose run is explicitly italic
    const xmlDoc = doc.element.ownerDocument!;
    const p = xmlDoc.createElement("w:p");
    const r = xmlDoc.createElement("w:r");
    const rPr = xmlDoc.createElement("w:rPr");
    const italic = xmlDoc.createElement("w:i");
    rPr.appendChild(italic);
    r.appendChild(rPr);
    const t = xmlDoc.createElement("w:t");
    t.setAttribute("xml:space", "preserve");
    t.textContent = "italicized anchor text here";
    r.appendChild(t);
    p.appendChild(r);
    doc.element.appendChild(p);

    const engine = new RedlineEngine(doc, "Test Author");
    engine.process_batch([
      { type: "modify", target_text: "anchor", new_text: "plain" },
    ]);

    const buf = await doc.save();

    // Re-read the saved zip to check document.xml
    const { unzipSync, strFromU8 } = await import("fflate");
    const unzipped = unzipSync(new Uint8Array(buf));
    const docXml = strFromU8(unzipped["word/document.xml"]);

    // Collect all w:ins/w:r runs and check for w:i
    const insRunPattern = /<w:ins\b[^>]*>([\s\S]*?)<\/w:ins>/g;
    const italicInInserted: string[] = [];
    let insMatch: RegExpExecArray | null;
    while ((insMatch = insRunPattern.exec(docXml)) !== null) {
      const insContent = insMatch[1];
      if (/<w:i\b/.test(insContent)) {
        italicInInserted.push(insContent.slice(0, 300));
      }
    }

    expect(italicInInserted).toHaveLength(0);
    // If this fails: BUG-23-2 — italic was inherited from surrounding paragraph
  });
});

// ===========================================================================
// Bug #3 — modify diff placement ignores new_text ordering
// ===========================================================================

describe("BUG-23-3: prefix insertion must land BEFORE the anchor, not after", () => {
  /**
   * The diff engine always appends the delta AFTER the common match
   * regardless of where it sits in new_text.
   *
   * Cross-platform parity: TestDiffPlacement in test_repro_issue23.py
   */

  it("target='fox', new_text='red fox': inserts 'red' BEFORE fox, fox is kept", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumps over the lazy dog.");

    const engine = new RedlineEngine(doc, "Test Author");
    engine.process_batch([
      { type: "modify", target_text: "fox", new_text: "red fox" },
    ]);

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf);

    // fox must NOT be struck out — it is the preserved anchor
    expect(text).not.toContain("{--fox--}");

    // The inserted prefix must appear
    const insertedMatch = text.match(/\{\+\+red\s*\+\+\}/);
    if (!insertedMatch) {
      throw new Error(
        `BUG-23-3: {++red...++} insertion not found in output.\nFull text: ${text}`,
      );
    }
    const insertedPos = text.indexOf(insertedMatch[0]);
    const foxPos = text.indexOf("fox");
    // fox must not have been silently deleted
    expect(foxPos).toBeGreaterThanOrEqual(0);
    // Must appear BEFORE fox
    expect(insertedPos).toBeLessThan(foxPos);
  });

  it(
    "new_text='Summary\\n\\nConclusion': paragraph separator is preserved before anchor",
    async () => {
      /**
       * target_text="Conclusion", new_text="Summary\\n\\nConclusion"
       * Expected: "Summary" paragraph inserted BEFORE "Conclusion", with a paragraph break.
       * Bug behaviour: "Summary" dropped, or merged into Conclusion paragraph, or appended after.
       *
       * Cross-platform parity: 'BUG-23-3b' in test_repro_issue23.py
       */
      const doc = await createTestDocument();
      addParagraph(doc, "Introduction paragraph.");
      addParagraph(doc, "Conclusion paragraph.");

      const engine = new RedlineEngine(doc, "Test Author");
      engine.process_batch([
        {
          type: "modify",
          target_text: "Conclusion",
          new_text: "Summary\n\nConclusion",
        },
      ]);

      const buf = await doc.save();
      const text = await extractTextFromBuffer(buf);

      expect(text).toContain("Summary");

      const summaryPos = text.indexOf("Summary");
      const conclusionPos = text.indexOf("Conclusion");
      expect(summaryPos).toBeLessThan(conclusionPos);

      // There must be a newline between them — they should NOT be merged
      const between = text.slice(summaryPos, conclusionPos);
      expect(between).toContain("\n");
    },
  );
});

// ===========================================================================
// Bug #4 — Multi-paragraph target_text is silently corrupt or opaque error
// ===========================================================================

describe("BUG-23-4: multi-paragraph target_text must produce actionable feedback", () => {
  /**
   * A target_text containing \\n\\n collapses the paragraph break in the token
   * stream and misaligns the diff (silent corruption), or gives an opaque
   * 'Target text not found' with no explanation.
   *
   * The correct behaviour: either support multi-paragraph targets correctly,
   * or reject them with a clear, actionable error message.
   *
   * Cross-platform parity: TestMultiParagraphTarget in test_repro_issue23.py
   */

  it("rejects multi-paragraph target_text with a clear error or handles it correctly", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph content.");
    addParagraph(doc, "Second paragraph content.");

    const engine = new RedlineEngine(doc, "Test Author");

    let raised: Error | null = null;
    try {
      engine.process_batch([
        {
          type: "modify",
          target_text: "First paragraph content.\n\nSecond paragraph content.",
          new_text: "Single replacement paragraph.",
        },
      ]);
    } catch (e: any) {
      raised = e;
    }

    if (raised === null) {
      // No error raised — verify the paragraph boundary wasn't silently collapsed.
      // Bug signature: both paragraphs are merged into a single deleted token without
      // the \n\n separator, e.g.
      //   {--First paragraph content.Second paragraph content.--}
      // A correct implementation either keeps the boundary or raises a clear error.
      const buf = await doc.save();
      const text = await extractTextFromBuffer(buf);

      const collapsed =
        "First paragraph content.Second paragraph content.";
      expect(text).not.toContain(collapsed);
      // Also catch space-separated collapse — "content. content." is equally broken
      const spaceCollapsed =
        "First paragraph content. Second paragraph content.";
      expect(text).not.toContain(spaceCollapsed);
    } else {
      // An error was raised — it must mention the multi-paragraph nature
      const msg = raised.message.toLowerCase();
      const actionableKeywords = [
        "paragraph",
        "multi",
        "boundary",
        "newline",
        "cross",
      ];
      const isActionable = actionableKeywords.some((kw) => msg.includes(kw));
      expect(isActionable).toBe(true);
    }
  });
});

// ===========================================================================
// Bug #5 — Ambiguous-match check counts text inside w:del
// ===========================================================================

describe("BUG-23-5: tracked-deleted text must not count toward ambiguity", () => {
  /**
   * After one copy of a duplicated string is tracked-deleted (sits inside
   * a w:del element), the remaining live copy must be uniquely matchable.
   * The current engine counts the dead copy as a live occurrence and reports
   * "Ambiguous match — target text appears 2 times".
   *
   * Cross-platform parity: TestAmbiguousMatchDel in test_repro_issue23.py
   */

  it("one live copy remains after tracked deletion; modify must not report ambiguous", async () => {
    const doc1 = await createTestDocument();
    addParagraph(doc1, "Context A: Dupe");
    addParagraph(doc1, "Context B: Dupe");

    const engine1 = new RedlineEngine(doc1, "Test Author");

    // Batch 1: delete the first occurrence (unique via full context)
    engine1.process_batch([
      { type: "modify", target_text: "Context A: Dupe", new_text: "" },
    ]);

    const buf1 = await doc1.save();

    // Sanity: first copy is now inside a w:del
    const text1 = await extractTextFromBuffer(buf1);
    expect(text1).toContain("{--Context A: Dupe--}");

    // Batch 2: only "Context B: Dupe" is live — must NOT throw ambiguous-match
    const doc2 = await DocumentObject.load(buf1);
    const engine2 = new RedlineEngine(doc2, "Test Author");

    let ambiguousError: Error | null = null;
    try {
      engine2.process_batch([
        { type: "modify", target_text: "Dupe", new_text: "Unique" },
      ]);
    } catch (e: any) {
      ambiguousError = e;
    }

    expect(ambiguousError).toBeNull();
    // If this fails: BUG-23-5 — the w:del copy was counted as a live match

    if (ambiguousError === null) {
      const buf2 = await doc2.save();
      const text2 = await extractTextFromBuffer(buf2);
      // 'Unique' must appear as a TRACKED INSERTION, not as a tracked deletion.
      // If {--Unique--} is present instead, the engine modified the w:del text.
      expect(text2).toContain("{++Unique++}");
      // Guard against "edited both copies" — the w:del text must not have been touched
      expect(text2).not.toContain("{--Unique--}");
    }
  });
});
