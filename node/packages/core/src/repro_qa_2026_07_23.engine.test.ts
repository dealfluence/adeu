// FILE: node/packages/core/src/repro_qa_2026_07_23.engine.test.ts
/**
 * Node-engine repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box QA
 * of v1.29.0+4bb70f9). Covers the findings that live in the shared core
 * engine:
 *
 *   F1 (high)   Reject of a multi-paragraph replacement corrupts the document:
 *               the original paragraph is restyled (e.g. to Heading 2) WITHOUT
 *               a tracked w:pPrChange, and one insert id is spread across
 *               three paragraphs. Rejecting the change then (a) leaves the
 *               restored sentence styled as a heading and (b) unwinds only the
 *               sibling-contiguous portion of the insert, leaving the other
 *               inserted paragraphs pending — including a duplicate of the
 *               sentence just restored.
 *   F2 (high)   Batch transactionality hole: edits that pass validation but
 *               fail at APPLY time (an insert_row with no row locator, a
 *               modify with empty target_text) are reported as "skipped"
 *               while the rest of the batch stays applied — the documented
 *               contract is all-or-nothing (BatchValidationError, nothing
 *               applied).
 *   F8 (medium) When a later edit fails validation after earlier edits
 *               validated, the error note reads "1 earlier edit(s) in this
 *               batch were already applied" — implying partial persistence
 *               even though the batch was rolled back and nothing was saved.
 *   F20 (low)   Change ids number in reverse document order when one logical
 *               edit fans out across several occurrences (match_mode "all"):
 *               the first occurrence gets Chg:5/6, the second 3/4, the third
 *               1/2. (Discovery note: a batch of three modifies with UNIQUE
 *               targets numbers ascending on current main — the reversal is
 *               produced by the bottom-up apply sweep of one edit's resolved
 *               sub-edits, which matches the QA report's observed 5/6, 3/4,
 *               1/2 exactly.)
 *
 * Every test in this file is written test-first: it fails on current main
 * and passes once the finding is fixed.
 */

import { describe, it, expect } from "vitest";
import { strFromU8, unzipSync } from "fflate";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { findAllDescendants } from "./docx/dom.js";

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, true, false) as string;
}

function rawMarkup(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, false, false) as string;
}

async function documentXml(doc: DocumentObject): Promise<string> {
  const buf = await doc.save();
  return strFromU8(unzipSync(new Uint8Array(buf))["word/document.xml"]);
}

// ---------------------------------------------------------------------------
// F1: reject of a multi-paragraph replacement corrupts the document
// ---------------------------------------------------------------------------

const F1_SENTENCE =
  "This agreement shall be governed by the laws of the State of Delaware.";
const F1_NEW_TEXT =
  "## Governing Law\n\n" +
  F1_SENTENCE +
  "\n\nAny dispute shall be resolved exclusively in the courts of Delaware.";

/**
 * Builds the QA scenario: one modify whose new_text is
 * "## Governing Law\n\n<sentence>\n\n<sentence 2>" replacing a body sentence.
 * Returns the saved-and-reloaded document plus the pre-edit clean text.
 */
async function buildF1AppliedDoc(): Promise<{
  doc: DocumentObject;
  original_clean: string;
}> {
  const doc = await createTestDocument();
  addParagraph(doc, "The parties agree to the terms set out below.");
  addParagraph(doc, F1_SENTENCE);
  addParagraph(doc, "Closing paragraph of the contract.");
  const original_clean = cleanText(doc);

  const engine = new RedlineEngine(doc, "Editor");
  const stats = engine.process_batch([
    { type: "modify", target_text: F1_SENTENCE, new_text: F1_NEW_TEXT },
  ]);
  expect(stats.edits_applied).toBe(1); // setup sanity, not the finding

  return {
    doc: await DocumentObject.load(await doc.save()),
    original_clean,
  };
}

/** Resolves the tracked deletion's id from the markup projection bubbles. */
async function resolveDeletionId(doc: DocumentObject): Promise<string> {
  const raw = await extractTextFromBuffer(await doc.save());
  const m = raw.match(/\[Chg:(\d+) delete\]/);
  expect(m, "expected a [Chg:N delete] bubble in the projection").toBeTruthy();
  return m![1];
}

describe("QA-2026-07-23 F1: reject of a multi-paragraph replacement", () => {
  it("records the paragraph restyle as a tracked w:pPrChange", async () => {
    const { doc } = await buildF1AppliedDoc();
    const xml = await documentXml(doc);

    // The engine restyled the original paragraph to Heading 2 — that part of
    // the mechanism must be present for this test to be meaningful.
    expect(xml).toContain('<w:pStyle w:val="Heading2"');

    // Word records a tracked style change as <w:pPrChange> carrying the
    // ORIGINAL paragraph properties, so reject can restore them. The QA run
    // verified 0 occurrences in the output XML.
    const pPrChangeCount = (xml.match(/<w:pPrChange[\s/>]/g) || []).length;
    expect(
      pPrChangeCount,
      "the Heading2 restyle was applied untracked: no w:pPrChange was " +
        "emitted, so rejecting the change cannot restore the original style",
    ).toBeGreaterThan(0);
  });

  it("rejecting the change (by its deletion id) restores the pre-edit document with no pending changes", async () => {
    const { doc, original_clean } = await buildF1AppliedDoc();
    const del_id = await resolveDeletionId(doc);

    // Both engines group-resolve a replacement's del+ins pair as ONE unit
    // (AI_CONTEXT.md §6), so rejecting the deletion side is the realistic
    // agent action for undoing the whole replacement.
    const engine = new RedlineEngine(doc, "Reviewer");
    const stats = engine.process_batch([
      { type: "reject", target_id: `Chg:${del_id}` },
    ]);
    expect(stats.actions_applied).toBe(1); // setup sanity

    // Reject must return the document to its prior state. On current main
    // the clean view instead shows the restored sentence AS A HEADING plus
    // two still-pending inserted paragraphs — one of which duplicates the
    // sentence just restored.
    expect(
      cleanText(doc).trim(),
      "reject did not restore the pre-edit document text",
    ).toBe(original_clean.trim());

    // ... and no pending tracked changes may remain (the insert id was
    // spread across three paragraphs; only the paired portion was unwound).
    const raw = rawMarkup(doc);
    expect(
      raw,
      "pending insertions survived the reject (orphaned portion of the " +
        "multi-paragraph insert id)",
    ).not.toContain("{++");
    expect(raw).not.toContain("{--");
    expect(findAllDescendants(doc.element, "w:ins").length).toBe(0);
    expect(findAllDescendants(doc.element, "w:del").length).toBe(0);
  });

  it("rejecting the change restores the original paragraph style (no Heading2 survives)", async () => {
    const { doc } = await buildF1AppliedDoc();

    // Precondition: the restyle exists while the change is pending.
    expect(await documentXml(doc)).toContain('<w:pStyle w:val="Heading2"');

    const del_id = await resolveDeletionId(doc);
    const engine = new RedlineEngine(doc, "Reviewer");
    engine.process_batch([{ type: "reject", target_id: `Chg:${del_id}` }]);

    // The original paragraphs carried no pStyle at all, so after a full
    // reject no paragraph may still claim Heading2.
    const surviving = findAllDescendants(doc.element, "w:pStyle").filter(
      (el) => el.getAttribute("w:val") === "Heading2",
    );
    expect(
      surviving.length,
      "the Heading2 restyle survived rejection of the change that " +
        "introduced it (style change was applied untracked)",
    ).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// F2: batch transactionality hole — apply-stage failures "skip" while the
// rest of the batch stays applied
// ---------------------------------------------------------------------------

describe("QA-2026-07-23 F2: apply-stage failures must reject the batch transactionally", () => {
  it("a mixed batch [valid modify, insert_row with no row locator] throws and leaves the document unchanged", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumps over the lazy dog.");
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "Item");
    setCellText(tbl, 0, 1, "Price");
    setCellText(tbl, 1, 0, "Widget");
    setCellText(tbl, 1, 1, "10");

    const clean_before = cleanText(doc);
    const markup_before = rawMarkup(doc);

    const engine = new RedlineEngine(doc, "Editor");
    let error: any = null;
    let result: any = null;
    try {
      result = engine.process_batch([
        {
          type: "modify",
          target_text: "lazy dog",
          new_text: "partial-apply canary",
        },
        // The malformed row op from the QA report: no target_text (row
        // locator) at all. It passes validation (validate_edits skips edits
        // without target_text) and only fails at apply time — which on
        // current main is a "skip", not a batch rejection.
        { type: "insert_row", cells: ["Gadget", "20"] } as any,
      ]);
    } catch (e) {
      error = e;
    }

    // The documented contract is transactional: if any edit fails, nothing
    // is applied. On current main the modify IS in the document ("1 applied,
    // 1 skipped") while the row op silently failed.
    expect(
      cleanText(doc),
      "the batch partially applied: the valid modify mutated the document " +
        "although the malformed insert_row failed",
    ).toBe(clean_before);
    expect(rawMarkup(doc)).toBe(markup_before);
    expect(
      error,
      `expected a transactional BatchValidationError, but process_batch ` +
        `returned normally (edits_applied=${result?.edits_applied}, ` +
        `edits_skipped=${result?.edits_skipped}, skipped_details=` +
        `${JSON.stringify(result?.skipped_details)})`,
    ).toBeInstanceOf(BatchValidationError);
  });

  it("a modify with empty target_text is rejected as a validation error, not reported as a skip", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The quick brown fox jumps over the lazy dog.");
    const clean_before = cleanText(doc);

    const engine = new RedlineEngine(doc, "Editor");
    let error: any = null;
    let result: any = null;
    try {
      result = engine.process_batch([
        { type: "modify", target_text: "", new_text: "Inserted clause." },
      ]);
    } catch (e) {
      error = e;
    }

    // The document must be untouched either way.
    expect(cleanText(doc)).toBe(clean_before);

    // On current main this "completes" as 0 applied / 1 skipped with the
    // cryptic detail "- Failed to apply edit targeting: 'insertion...'"
    // instead of rejecting the batch.
    expect(
      error,
      `expected a transactional BatchValidationError for the empty ` +
        `target_text, but process_batch returned normally ` +
        `(edits_applied=${result?.edits_applied}, edits_skipped=` +
        `${result?.edits_skipped}, skipped_details=` +
        `${JSON.stringify(result?.skipped_details)})`,
    ).toBeInstanceOf(BatchValidationError);
  });
});

// ---------------------------------------------------------------------------
// F8: misleading "already applied" note on rejected batches
// ---------------------------------------------------------------------------

describe("QA-2026-07-23 F8: rejected-batch note must not imply partial persistence", () => {
  it("the sequential-context note states that the batch was rolled back / nothing was saved", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Payment is due in 30 days.");
    addParagraph(doc, "Second paragraph here.");
    const clean_before = cleanText(doc);

    const engine = new RedlineEngine(doc, "Editor");
    let error: any = null;
    try {
      engine.process_batch([
        { type: "modify", target_text: "30 days", new_text: "60 days" },
        {
          type: "modify",
          target_text: "THIS TEXT DOES NOT EXIST ANYWHERE",
          new_text: "irrelevant",
        },
      ]);
    } catch (e) {
      error = e;
    }

    // Setup sanity: the batch was rejected and rolled back — the note's
    // claim that earlier edits "were already applied" is about a transient
    // in-memory validation pass, not the document.
    expect(error).toBeInstanceOf(BatchValidationError);
    expect(cleanText(doc)).toBe(clean_before);

    // Current wording: "Note: 1 earlier edit(s) in this batch were already
    // applied. Batches apply sequentially — …". As phrased it reads as
    // partial application. It must state that nothing was persisted.
    expect(
      error.message,
      "the error note implies earlier edits were persisted; it must say " +
        "the batch was rolled back / nothing was saved",
    ).toMatch(
      /nothing (was |has been )?(saved|written)|rolled back|not (been )?saved/i,
    );
  });
});

// ---------------------------------------------------------------------------
// F20: change ids number in reverse document order
// ---------------------------------------------------------------------------

describe("QA-2026-07-23 F20: change ids ascend in document order", () => {
  it("a match_mode 'all' fan-out numbers its occurrences top-to-bottom", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "alpha apple one");
    addParagraph(doc, "beta apple two");
    addParagraph(doc, "gamma apple three");

    const engine = new RedlineEngine(doc, "Editor");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "apple",
        new_text: "pear",
        match_mode: "all",
      },
    ]);
    expect(stats.occurrences_modified).toBe(3); // setup sanity

    const raw = await extractTextFromBuffer(await doc.save());

    // Chg:N ids as their bubbles appear top-to-bottom in the projection.
    const del_ids = [...raw.matchAll(/\[Chg:(\d+) delete\]/g)].map((m) =>
      parseInt(m[1], 10),
    );
    const ins_ids = [...raw.matchAll(/\[Chg:(\d+) insert\]/g)].map((m) =>
      parseInt(m[1], 10),
    );
    expect(del_ids.length).toBe(3); // setup sanity

    // On current main the first occurrence carries Chg:5/6, the second 3/4,
    // the third 1/2 — ids descend because the apply sweep runs bottom-up.
    expect(
      del_ids,
      `deletion ids must ascend in document order (projection order was ` +
        `${del_ids.join(", ")})`,
    ).toEqual([...del_ids].sort((a, b) => a - b));
    expect(
      ins_ids,
      `insertion ids must ascend in document order (projection order was ` +
        `${ins_ids.join(", ")})`,
    ).toEqual([...ins_ids].sort((a, b) => a - b));
  });
});
