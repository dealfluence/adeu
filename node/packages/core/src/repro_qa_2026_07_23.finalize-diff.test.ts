// FILE: node/packages/core/src/repro_qa_2026_07_23.finalize-diff.test.ts
/**
 * Node-engine repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box QA
 * of v1.29.0+4bb70f9). Findings covered here:
 *
 *   F12 (medium)  Finalization report inconsistencies:
 *     (a) the "Tracked changes auto-accepted: N" headline counts revision
 *         ELEMENTS (w:ins + w:del — the deliberate unit, see AI_CONTEXT.md
 *         § "Accept-All Counts Are Revision MARKS (2026-07-22)"), but the
 *         per-change list below it only renders elements with visible text
 *         (sanitize/transforms.ts accept_all_tracked_changes): a
 *         multi-paragraph insertion's paragraph-mark w:ins elements are
 *         counted yet never listed, so the report says e.g. "9" over a list
 *         of 7 items with no reconciliation. A fix may list every element,
 *         annotate the difference (mention fragments/elements next to the
 *         headline), or make the counts agree — all three pass here.
 *     (b) a comment attached to a tracked change is deleted by
 *         RedlineEngine.accept_all_revisions() (own-authored comment wrapping
 *         a resolved revision) INSIDE accept_all_tracked_changes, whose
 *         removed_comments result is discarded; get_comments_summary /
 *         remove_all_comments then run on an already-comment-free document
 *         (sanitize/core.ts), so the report never mentions the removal and
 *         even prints "0 comments removed" while the comment is gone from
 *         the output package.
 *
 *   F15 (low)  compare_clean=false diffs split CriticMarkup tokens mid-tag
 *         across hunks: create_word_patch_diff (diff.ts) hunk boundaries fall
 *         inside {== ... ==}{>> ... <<} tokens, producing a "+ {==" line with
 *         nothing else on it and a later hunk line starting with the orphaned
 *         "==}" closer. Delimiters must be atomic within diff output.
 *
 * (F9 from the same report lives at the MCP layer — see
 * node/packages/mcp-server/src/repro.qa_2026_07_23.finalize.test.ts.)
 *
 * Every test in this file is written test-first: it fails on current main
 * and passes once the finding is fixed.
 */

import { describe, it, expect } from "vitest";
import { strFromU8, unzipSync } from "fflate";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { finalize_document } from "./sanitize/core.js";
import { extractTextFromBuffer } from "./ingest.js";
import { create_word_patch_diff } from "./diff.js";

// ---------------------------------------------------------------------------
// F12a: headline auto-accepted count must reconcile with the listed items
// ---------------------------------------------------------------------------

describe("F12a: finalize report headline count vs listed change items", () => {
  it("the auto-accepted headline equals the listed item count (or explains the difference)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Payment is due in 30 days.");
    addParagraph(doc, "The agreement continues in force.");
    addParagraph(doc, "Third paragraph stands here.");

    const engine = new RedlineEngine(doc, "Editor");
    engine.apply_edits([
      {
        // One LOGICAL change fragmented across multiple w:ins elements:
        // a multi-paragraph replacement spreads one insertion over three
        // paragraphs and additionally inserts tracked paragraph marks —
        // w:ins elements with no visible text, which the headline counts
        // but the list below it silently omits.
        type: "modify",
        target_text: "The agreement continues in force.",
        new_text:
          "First replacement paragraph.\n\nSecond replacement paragraph.\n\nThird replacement paragraph.",
      },
      // ...plus a couple of normal changes.
      { type: "modify", target_text: "30 days", new_text: "60 days" },
      {
        type: "modify",
        target_text: "Third paragraph stands here.",
        new_text: "Third paragraph stands here, amended.",
      },
    ]);
    const doc2 = await DocumentObject.load(await doc.save());

    const result = await finalize_document(doc2, {
      filename: "f12a.docx",
      sanitize_mode: "full",
      accept_all: true,
    });
    const report = result.reportText;

    const headlineMatch = report.match(/Tracked changes auto-accepted:\s*(\d+)/);
    expect(headlineMatch, `report has no auto-accepted headline:\n${report}`).toBeTruthy();
    const headlineCount = parseInt(headlineMatch![1], 10);

    // The TRACKED CHANGES section: headline plus the per-change item lines.
    const sectionMatch = report.match(/TRACKED CHANGES\n([\s\S]*?)(?:\n\s*\n|$)/);
    expect(sectionMatch, `report has no TRACKED CHANGES section:\n${report}`).toBeTruthy();
    const section = sectionMatch![1];

    const listedCount = (section.match(/^\s*Accepted /gm) || []).length;
    expect(listedCount).toBeGreaterThan(0);

    // A legitimate fix either makes the counts agree (listing every counted
    // element, or counting only listed items) or reconciles the difference
    // in the section itself (e.g. "9 revision elements (7 with visible
    // text)" / a note about fragments). The current silent mismatch fails.
    const reconciled =
      headlineCount === listedCount || /fragment|element/i.test(section);
    expect(
      reconciled,
      `Headline claims ${headlineCount} tracked changes auto-accepted but only ` +
        `${listedCount} items are listed below it, with no reconciliation.\n` +
        `TRACKED CHANGES section:\n${section}`,
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// F12b: a removed comment must be mentioned by the finalization report
// ---------------------------------------------------------------------------

describe("F12b: finalize report discloses comment removal", () => {
  it("removing a change-attached comment is reported, not silent", async () => {
    const COMMENT_TEXT = "Handled during the July review cycle.";

    const doc = await createTestDocument();
    addParagraph(
      doc,
      "Confidential information must be protected at all times.",
    );
    const engine = new RedlineEngine(doc, "Editor");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "protected",
        new_text: "safeguarded",
        comment: COMMENT_TEXT,
      },
    ]);
    const preBuffer = await doc.save();

    // Sanity: the document really carries the comment before finalize.
    const rawBefore = (await extractTextFromBuffer(
      preBuffer,
      false,
      false,
    )) as string;
    expect(rawBefore).toContain("[Com:");
    expect(rawBefore).toContain(COMMENT_TEXT);

    const doc2 = await DocumentObject.load(preBuffer);
    const result = await finalize_document(doc2, {
      filename: "f12b.docx",
      sanitize_mode: "full",
      accept_all: true,
    });
    expect(result.outBuffer).toBeTruthy();

    // Sanity: the comment WAS removed — no trace of it survives in any
    // member of the saved package.
    const members = unzipSync(new Uint8Array(result.outBuffer!));
    for (const [name, bytes] of Object.entries(members)) {
      expect(
        strFromU8(bytes).includes(COMMENT_TEXT),
        `comment text still present in ${name}`,
      ).toBe(false);
    }

    // The report must acknowledge that removal. Currently it does not
    // mention the comment anywhere and even claims "0 comments removed":
    // accept_all_revisions() deletes the change-attached comment and its
    // removed_comments count is discarded before get_comments_summary runs.
    const report = result.reportText;
    const mentionsRemoval =
      /Comments removed:\s*[1-9]/i.test(report) ||
      /[1-9]\d*\s+comments? removed/i.test(report) ||
      /COMMENTS \(stripped\)/.test(report) ||
      report.includes(COMMENT_TEXT);
    expect(
      mentionsRemoval,
      `The comment was removed from the package but the report never says so:\n${report}`,
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// F15: CriticMarkup delimiters must be atomic in word-patch diff output
// ---------------------------------------------------------------------------

describe("F15: raw (compare_clean=false) diff keeps CriticMarkup tokens whole", () => {
  const OPENER_FRAGMENTS = /^\{([=+\-><]{1,2})?$/; // "{", "{=", "{==", "{+", "{++", ...
  const BARE_FRAGMENTS = new Set([
    "==",
    "++",
    "--",
    ">>",
    "<<",
    "==}",
    "++}",
    "--}",
    "<<}",
  ]);
  const ORPHAN_CLOSER_START = /^(==\}|\+\+\}|--\}|<<\})/;
  const SPLIT_OPENER_END = /\{([=+\-><]{1,2})?$/;

  function brokenDelimiterLines(diff: string): string[] {
    const broken: string[] = [];
    for (const line of diff.split("\n")) {
      const m = line.match(/^([+-]) (.*)$/);
      if (!m) continue; // context/header/continuation lines
      const content = m[2];
      const trimmed = content.trim();
      if (
        // (1) a hunk line that IS nothing but a fragment of a delimiter,
        //     e.g. "+ {==" alone on a line (verbatim QA symptom)
        OPENER_FRAGMENTS.test(trimmed) ||
        BARE_FRAGMENTS.has(trimmed) ||
        // (2) a hunk line starting with an orphaned closing fragment whose
        //     opener sits in another hunk, e.g. "+ ==}{>>[Com:1] ..."
        ORPHAN_CLOSER_START.test(content) ||
        // (3) a hunk line ending mid-opener, e.g. "... {" / "... {=="
        SPLIT_OPENER_END.test(content)
      ) {
        broken.push(line);
      }
    }
    return broken;
  }

  it("no diff hunk line consists of or starts/ends with a bare delimiter fragment", async () => {
    // Plain baseline document.
    const docA = await createTestDocument();
    addParagraph(
      docA,
      "Confidential information must be protected at all times by the receiving party without exception.",
    );
    addParagraph(docA, "This agreement is governed by the laws of Finland.");
    const bufA = await docA.save();

    // Same document carrying a tracked change and a comment, so the raw
    // projection contains {--..--}{++..++} and {==..==}{>>..<<} tokens. The
    // comment anchors a long phrase: the shared anchor text survives
    // diff_cleanupSemantic, so the {== opener and the ==}{>>..<<} tail end
    // up in different hunks on current main.
    const docB = await DocumentObject.load(bufA);
    const engineB = new RedlineEngine(docB, "Editor");
    engineB.apply_edits([
      {
        type: "modify",
        target_text:
          "Confidential information must be protected at all times by the receiving party without exception",
        new_text:
          "Confidential information must be protected at all times by the receiving party without exception",
        comment: "Confirm this survives the 2026 revision cycle.",
      },
      {
        type: "modify",
        target_text: "laws of Finland",
        new_text: "laws of Sweden",
      },
    ]);
    const bufB = await docB.save();

    // Exactly what the diff_docx_files tool does for compare_clean=false.
    const rawA = (await extractTextFromBuffer(bufA, false, false)) as string;
    const rawB = (await extractTextFromBuffer(bufB, false, false)) as string;
    expect(rawB).toContain("{=="); // the projection really carries the tokens
    expect(rawB).toContain("<<}");

    const diff = create_word_patch_diff(rawA, rawB, "a.docx", "b.docx");
    expect(diff).not.toBe(""); // there are differences

    const broken = brokenDelimiterLines(diff);
    expect(
      broken,
      `CriticMarkup delimiters were split mid-token across diff hunk lines:\n` +
        broken.map((l) => JSON.stringify(l)).join("\n") +
        `\n\nFull diff:\n${diff}`,
    ).toEqual([]);
  });
});
