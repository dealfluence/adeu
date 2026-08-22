// FILE: node/packages/core/src/comment_dedup.test.ts
import { describe, it, expect } from "vitest";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentObject } from "./docx/bridge.js";
import { DocumentMapper } from "./mapper.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NS_W =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
const NS_W14 =
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"';
const NS_R =
  'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';

/**
 * Builds a minimal in-memory DOCX buffer from raw document body XML and a
 * raw <w:comment> element list. We hand-assemble the OPC package to control
 * exactly how runs are split inside comment ranges — python-docx-style
 * builders would coalesce runs and hide the bug.
 */
import { createTestPackageWithComments } from "./test-utils.js";

function buildDocx(bodyXml: string, commentsListXml: string = ""): Buffer {
  const commentsXml = commentsListXml.length > 0
    ? `<w:comments ${NS_W} ${NS_W14}>${commentsListXml}</w:comments>`
    : undefined;
  return createTestPackageWithComments(bodyXml, commentsXml);
}

function commentXml(
  id: string,
  author: string,
  date: string,
  text: string,
): string {
  return `<w:comment w:id="${id}" w:author="${author}" w:date="${date}" w:initials="X">
    <w:p w14:paraId="${id.padStart(8, "0")}" w14:textId="77777777">
      <w:r><w:t>${text}</w:t></w:r>
    </w:p>
  </w:comment>`;
}

// ---------------------------------------------------------------------------
// Regression tests for the multi-run comment duplication bug
// ---------------------------------------------------------------------------

describe("Multi-run comment deduplication (BUG: comment text repeated per run)", () => {
  it("emits a single comment block when one comment spans many runs (the reported bug)", async () => {
    // 8 runs (Word naturally fragments around numbers, punctuation, formatting).
    // Before the fix this produced 8 duplicate {>>...<<} blocks.
    const body = `
      <w:p>
        <w:commentRangeStart w:id="1"/>
        <w:r><w:t xml:space="preserve">Party A shall pay </w:t></w:r>
        <w:r><w:t>100</w:t></w:r>
        <w:r><w:t>%</w:t></w:r>
        <w:r><w:t xml:space="preserve"> of the total</w:t></w:r>
        <w:r><w:t xml:space="preserve"> amount</w:t></w:r>
        <w:r><w:t xml:space="preserve"> on </w:t></w:r>
        <w:r><w:t>time</w:t></w:r>
        <w:r><w:t>.</w:t></w:r>
        <w:commentRangeEnd w:id="1"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      </w:p>`;

    const comments = commentXml(
      "1",
      "Reviewer",
      "2026-06-15T10:00:00Z",
      "Risk note: please confirm the payment percentage and timing.",
    );

    const buf = buildDocx(body, comments);
    const md = await extractTextFromBuffer(buf, false);

    // The full highlighted range must be a single contiguous {==...==} block.
    expect(md).toContain(
      "{==Party A shall pay 100% of the total amount on time.==}",
    );

    // Exactly one comment meta block.
    expect((md.match(/\[Com:1\]/g) || []).length).toBe(1);
    expect((md.match(/\{==/g) || []).length).toBe(1);
    expect((md.match(/\{>>/g) || []).length).toBe(1);

    // And the comment payload appears exactly once.
    const payload =
      "Risk note: please confirm the payment percentage and timing.";
    expect(md.split(payload).length - 1).toBe(1);
  });

  it("keeps two separate comments on adjacent runs independent (must NOT merge)", async () => {
    // Comment 1 wraps "First part", comment 2 wraps "second part".
    // These are distinct ranges with no overlap and must stay distinct in output.
    const body = `
      <w:p>
        <w:commentRangeStart w:id="1"/>
        <w:r><w:t xml:space="preserve">First </w:t></w:r>
        <w:r><w:t>part</w:t></w:r>
        <w:commentRangeEnd w:id="1"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
        <w:r><w:t xml:space="preserve"> middle </w:t></w:r>
        <w:commentRangeStart w:id="2"/>
        <w:r><w:t xml:space="preserve">second </w:t></w:r>
        <w:r><w:t>part</w:t></w:r>
        <w:commentRangeEnd w:id="2"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="2"/></w:r>
      </w:p>`;

    const comments =
      commentXml("1", "A", "2026-06-15T10:00:00Z", "First annotation") +
      commentXml("2", "B", "2026-06-15T11:00:00Z", "Second annotation");

    const buf = buildDocx(body, comments);
    const md = await extractTextFromBuffer(buf, false);

    // Each comment appears exactly once, and they are distinct blocks.
    expect((md.match(/\[Com:1\]/g) || []).length).toBe(1);
    expect((md.match(/\[Com:2\]/g) || []).length).toBe(1);
    expect((md.match(/\{>>/g) || []).length).toBe(2);
    expect((md.match(/\{==/g) || []).length).toBe(2);

    // Highlighted ranges must contain the right text on each side of the middle.
    expect(md).toContain("{==First part==}");
    expect(md).toContain("{==second part==}");
    // The un-commented "middle" text must NOT be inside a highlight wrapper.
    expect(md).toContain(" middle ");
  });

  it("merges overlapping comments (same range, two comment IDs) into a single meta block", async () => {
    // Both comment 1 and comment 2 span the same exact text. Per the existing
    // state-machine semantics, both IDs are active simultaneously, so the meta
    // block should list both — but only once.
    const body = `
      <w:p>
        <w:commentRangeStart w:id="1"/>
        <w:commentRangeStart w:id="2"/>
        <w:r><w:t xml:space="preserve">Shared </w:t></w:r>
        <w:r><w:t>highlighted</w:t></w:r>
        <w:r><w:t xml:space="preserve"> text</w:t></w:r>
        <w:commentRangeEnd w:id="1"/>
        <w:commentRangeEnd w:id="2"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="2"/></w:r>
      </w:p>`;

    const comments =
      commentXml("1", "A", "2026-06-15T10:00:00Z", "Comment one") +
      commentXml("2", "B", "2026-06-15T11:00:00Z", "Comment two");

    const buf = buildDocx(body, comments);
    const md = await extractTextFromBuffer(buf, false);

    // Single highlight wrapper covering the full range.
    expect(md).toContain("{==Shared highlighted text==}");
    expect((md.match(/\{==/g) || []).length).toBe(1);

    // Single meta block that mentions both comments.
    expect((md.match(/\{>>/g) || []).length).toBe(1);
    expect((md.match(/\[Com:1\]/g) || []).length).toBe(1);
    expect((md.match(/\[Com:2\]/g) || []).length).toBe(1);
  });

  it("handles a tracked insertion nested inside a comment range without duplication", async () => {
    // Comment 1 spans 3 runs; one of those runs is wrapped in <w:ins>.
    // The redline must produce its own {++...++} marker but the comment
    // payload must still appear only once.
    const body = `
      <w:p>
        <w:commentRangeStart w:id="1"/>
        <w:r><w:t xml:space="preserve">Plain </w:t></w:r>
        <w:ins w:id="100" w:author="Editor" w:date="2026-06-15T12:00:00Z">
          <w:r><w:t>inserted</w:t></w:r>
        </w:ins>
        <w:r><w:t xml:space="preserve"> tail</w:t></w:r>
        <w:commentRangeEnd w:id="1"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      </w:p>`;

    const comments = commentXml(
      "1",
      "Reviewer",
      "2026-06-15T10:00:00Z",
      "Mixed comment over a redline",
    );

    const buf = buildDocx(body, comments);
    const md = await extractTextFromBuffer(buf, false);

    // The insertion is still surfaced as a redline.
    expect(md).toContain("{++inserted++}");

    // The comment payload appears exactly once.
    expect((md.match(/\[Com:1\]/g) || []).length).toBe(1);
    expect((md.match(/Mixed comment over a redline/g) || []).length).toBe(1);

    // There should be the comment highlight pieces around the plain text,
    // plus the {++ wrapper for the insertion — but the comment text itself
    // must not be repeated.
  });

  it("DocumentMapper.full_text mirrors the projected output (parity with ingest)", async () => {
    // If the mapper produces a different number of comment blocks than the
    // projection layer, the engine's find/replace anchoring breaks. This guards
    // mapper.ts against regressing independently of ingest.ts.
    const body = `
      <w:p>
        <w:commentRangeStart w:id="1"/>
        <w:r><w:t xml:space="preserve">A </w:t></w:r>
        <w:r><w:t>B</w:t></w:r>
        <w:r><w:t xml:space="preserve"> C</w:t></w:r>
        <w:commentRangeEnd w:id="1"/>
        <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      </w:p>`;

    const comments = commentXml(
      "1",
      "X",
      "2026-06-15T10:00:00Z",
      "Mapper parity check",
    );

    const buf = buildDocx(body, comments);
    const doc = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(doc);

    expect((mapper.full_text.match(/\[Com:1\]/g) || []).length).toBe(1);
    expect((mapper.full_text.match(/\{>>/g) || []).length).toBe(1);
    expect((mapper.full_text.match(/\{==/g) || []).length).toBe(1);
    expect(mapper.full_text).toContain("{==A B C==}");
  });
});
