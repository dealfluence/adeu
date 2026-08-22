// FILE: node/packages/core/src/repro.stranded-comment-anchor.test.ts
//
// Demo run "Asteria v Northstar" (2026-08-12), defect B: the processed
// protective order contained the fragment
//
//     ... shall not be disclosed Attorney's Eyes Only ;
//
// i.e. a reviewer's commented phrase left standing alone in a sentence whose
// surrounding words had been deleted around it.
//
// This is NOT an engine bug: the XML shows two SEPARATE tracked deletions
// (w:id=4 and w:id=6) bracketing the comment anchor, so the agent deliberately
// made two edits on either side of "Attorney's Eyes Only" and — presumably to
// preserve Sarah Chen's comment anchor — kept the phrase itself. Each deletion
// is individually legal and each was individually reported as applied. Nothing
// ever cross-checked the two against each other, so the agent got no signal
// that the sentence it left behind reads as gibberish once accepted.
//
// The gap is therefore advisory, and the fix is an ADVISORY: a batch-level
// warning, never a rejection. Keeping a foreign comment's anchor alive while
// editing around it is a legitimate (if delicate) thing to do, so the engine
// must still apply the batch — it just has to say what it noticed.
//
// The Python twin is python/tests/test_repro_stranded_comment_anchor.py.

import { describe, it, expect } from "vitest";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { createTestPackageWithComments } from "./test-utils.js";

// ---------------------------------------------------------------------------
// Fixture builder (same shape as repro.comment-range-modify.test.ts)
// ---------------------------------------------------------------------------

const NS_W =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
const NS_W14 =
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"';

function buildDocx(bodyXml: string, commentsListXml: string = ""): Buffer {
  const commentsXml = commentsListXml.length > 0
    ? `<w:comments ${NS_W} ${NS_W14}>${commentsListXml}</w:comments>`
    : undefined;
  return createTestPackageWithComments(bodyXml, commentsXml);
}

const SARAH_COMMENT = `<w:comment w:id="1" w:author="Sarah Chen" w:date="2026-08-01T09:00:00Z" w:initials="SC">
    <w:p><w:r><w:t>Should this tier survive the meet-and-confer?</w:t></w:r></w:p>
  </w:comment>`;

/**
 * The demo's shape: one clause, with a foreign comment anchored to the middle
 * phrase and ordinary body text on both sides of it.
 */
function buildProtectiveOrder(): Buffer {
  const body = `
    <w:p><w:r><w:t>PROTECTIVE ORDER</w:t></w:r></w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">Protected Material shall not be disclosed, unless such disclosure is for </w:t></w:r>
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Attorney's Eyes Only</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      <w:r><w:t xml:space="preserve"> and is so designated by the Producing Party;</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Nothing in this Order abridges any party's rights.</w:t></w:r></w:p>`;
  return buildDocx(body, SARAH_COMMENT);
}

/**
 * Same clause, but the text on the LEFT of the anchor was already deleted by
 * another author in an earlier round (w:id 900 is below any id this engine
 * will mint, exactly like a document arriving with history).
 */
function buildProtectiveOrderWithPriorLeftDeletion(): Buffer {
  const body = `
    <w:p><w:r><w:t>PROTECTIVE ORDER</w:t></w:r></w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">Protected Material shall not be disclosed</w:t></w:r>
      <w:del w:id="900" w:author="Opposing Counsel" w:date="2026-08-02T09:00:00Z">
        <w:r><w:delText xml:space="preserve">, unless such disclosure is for </w:delText></w:r>
      </w:del>
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Attorney's Eyes Only</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      <w:r><w:t xml:space="preserve"> and is so designated by the Producing Party;</w:t></w:r>
    </w:p>`;
  return buildDocx(body, SARAH_COMMENT);
}

/** Both sides already deleted by someone else; this batch touches elsewhere. */
function buildProtectiveOrderAlreadyStranded(): Buffer {
  const body = `
    <w:p><w:r><w:t>PROTECTIVE ORDER</w:t></w:r></w:p>
    <w:p>
      <w:del w:id="900" w:author="Opposing Counsel" w:date="2026-08-02T09:00:00Z">
        <w:r><w:delText xml:space="preserve">Protected Material shall not be disclosed, unless such disclosure is for </w:delText></w:r>
      </w:del>
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Attorney's Eyes Only</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
      <w:del w:id="901" w:author="Opposing Counsel" w:date="2026-08-02T09:00:00Z">
        <w:r><w:delText xml:space="preserve"> and is so designated by the Producing Party;</w:delText></w:r>
      </w:del>
    </w:p>
    <w:p><w:r><w:t>Nothing in this Order abridges any party's rights.</w:t></w:r></w:p>`;
  return buildDocx(body, SARAH_COMMENT);
}

/** All batch-level advisory lines mentioning a stranded anchor. */
function strandedWarnings(stats: any): string[] {
  return (stats.skipped_details || []).filter((d: string) =>
    /left .*anchored text|stands alone/i.test(d),
  );
}

describe("Stranded comment anchor advisory — Node engine", () => {
  // ─────────────────────────────────────────────────────────────────────────
  // RED — the demo defect. Two legal deletions, one stranded phrase, silence.
  // ─────────────────────────────────────────────────────────────────────────
  it("warns when a batch deletes on BOTH sides of a comment anchor and leaves its text standing", async () => {
    const doc = await DocumentObject.load(buildProtectiveOrder());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      { type: "delete", target_text: ", unless such disclosure is for " },
      { type: "delete", target_text: " and is so designated by the Producing Party" },
    ] as any[]);

    // An advisory, NEVER a rejection: both edits still apply.
    expect(stats.edits_applied).toBe(2);
    expect(stats.edits_skipped).toBe(0);

    const warnings = strandedWarnings(stats);
    expect(warnings.length).toBe(1);

    // The message has to be actionable: which comment, whose, and what text
    // was left behind. An anonymous "a comment was stranded" reads like engine
    // bookkeeping and is exactly what the demo run rationalised away.
    expect(warnings[0]).toContain("Com:1");
    expect(warnings[0]).toContain("Sarah Chen");
    expect(warnings[0]).toContain("Attorney's Eyes Only");
    expect(warnings[0]).toMatch(/^- Warning:/);
  });

  it("does not turn the advisory into a skip or a failed edit report", async () => {
    const doc = await DocumentObject.load(buildProtectiveOrder());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      { type: "delete", target_text: ", unless such disclosure is for " },
      { type: "delete", target_text: " and is so designated by the Producing Party" },
    ] as any[]);

    for (const report of stats.edits) {
      expect(report.status).toBe("applied");
      expect(report.error).toBeNull();
    }
  });

  // ─────────────────────────────────────────────────────────────────────────
  // GREEN controls — the boundary of the advisory. False positives here would
  // train the agent to ignore the warning, which is worse than not having it.
  // ─────────────────────────────────────────────────────────────────────────
  it("stays silent when one edit removes the whole clause INCLUDING the anchored text", async () => {
    const doc = await DocumentObject.load(buildProtectiveOrder());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      {
        type: "delete",
        target_text:
          ", unless such disclosure is for Attorney's Eyes Only and is so designated by the Producing Party",
      },
    ] as any[]);

    expect(stats.edits_applied).toBe(1);
    // The anchored text goes away WITH the sentence, so nothing is stranded.
    expect(strandedWarnings(stats)).toEqual([]);
  });

  it("stays silent when the batch deletes on only ONE side of the anchor", async () => {
    const doc = await DocumentObject.load(buildProtectiveOrder());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      { type: "delete", target_text: " and is so designated by the Producing Party" },
    ] as any[]);

    expect(stats.edits_applied).toBe(1);
    expect(strandedWarnings(stats)).toEqual([]);
  });

  it("stays silent for an ordinary modify under a foreign comment", async () => {
    const doc = await DocumentObject.load(buildProtectiveOrder());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "Attorney's Eyes Only",
        new_text: "Outside Counsel Only",
      },
    ] as any[]);

    expect(stats.edits_applied).toBe(1);
    expect(strandedWarnings(stats)).toEqual([]);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // Attribution — the advisory is about what THIS batch caused.
  // ─────────────────────────────────────────────────────────────────────────
  it("warns when this batch supplies the SECOND bracket to a pre-existing deletion", async () => {
    // The left side was already gone when the document arrived; deleting the
    // right side is the act that strands the phrase, so this batch owns it.
    const doc = await DocumentObject.load(
      buildProtectiveOrderWithPriorLeftDeletion(),
    );
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      { type: "delete", target_text: " and is so designated by the Producing Party" },
    ] as any[]);

    expect(stats.edits_applied).toBe(1);
    const warnings = strandedWarnings(stats);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain("Attorney's Eyes Only");
  });

  it("stays silent about an anchor that was ALREADY stranded before the batch", async () => {
    // Nagging about a condition the caller did not cause — and may not be
    // allowed to fix — is noise on every subsequent batch.
    const doc = await DocumentObject.load(buildProtectiveOrderAlreadyStranded());
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "Nothing in this Order abridges any party's rights.",
        new_text: "Nothing in this Order abridges any party's appellate rights.",
      },
    ] as any[]);

    expect(stats.edits_applied).toBe(1);
    expect(strandedWarnings(stats)).toEqual([]);
  });

  it("stays silent on a document with no comments at all", async () => {
    const doc = await DocumentObject.load(
      buildDocx(`
        <w:p><w:r><w:t xml:space="preserve">Protected Material shall not be disclosed, unless such disclosure is for Attorney's Eyes Only and is so designated by the Producing Party;</w:t></w:r></w:p>`),
    );
    const engine = new RedlineEngine(doc, "Agent");

    const stats = engine.process_batch([
      { type: "delete", target_text: ", unless such disclosure is for " },
      { type: "delete", target_text: " and is so designated by the Producing Party" },
    ] as any[]);

    expect(stats.edits_applied).toBe(2);
    expect(strandedWarnings(stats)).toEqual([]);
  });
});
