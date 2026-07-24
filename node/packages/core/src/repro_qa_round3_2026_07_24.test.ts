// FILE: node/packages/core/src/repro_qa_round3_2026_07_24.test.ts
/**
 * Node-engine repro tests for the Adeu MCP QA Report — Round 3 (2026-07-24,
 * black-box QA of v1.30.0+1fd5285). Node-side findings verified to reproduce:
 *
 *   1.1 (S1)  Accepting a tracked change deletes the comment anchored on it —
 *             silently. The engine detaches the commentRangeStart/End and the
 *             commentReference run for ANY wrapping comment; a foreign-
 *             authored body is kept in word/comments.xml but is orphaned
 *             (invisible in Word and in the projection). Word semantics —
 *             and the Python engine — keep the comment anchored on the
 *             surviving text as a highlight.
 *   1.3 (S1)  A modify targeting the {#cell:...} anchor of a NON-empty cell
 *             silently prepends new_text at cell start, garbling the cell to
 *             "By: /s/ Test SignerBy: ".
 *   2.1 (S2)  Rejecting every id a read enumerated hard-fails on chained
 *             pair groups: partners resolved by an earlier action mostly get
 *             the graceful already_resolved treatment, but one group member
 *             falls through to "no tracked change with that id exists"
 *             (reproduces on BOTH engines — shared algorithm).
 *   2.2 (S2)  Format-only tracked changes render as "[Chg:N format]" but the
 *             id is not actionable: read and write disagree about what
 *             exists (accept_all_changes CAN resolve them, so only per-id
 *             targeting is missing).
 *   2.3 (S2)  regex modify with Python-style "\1" backreferences in new_text
 *             writes the literal "\1" into the document with NO warning.
 *             Python has the exact mirror guard (it warns on "$1"); port it.
 *   3.4 (S3)  Comment-removal reporting gaps: (a) resolving a change that
 *             makes a wrapping comment disappear produces NO informational
 *             note (the note diffing only counts deleted BODIES, and foreign
 *             bodies are kept-but-orphaned); (b) accept_all_revisions
 *             reports removed_comments=0 while every comment in the
 *             document is in fact ejected.
 *   3.5 (S3)  The keep-markup finalize report renders "Result: CLEAN
 *             (N changes resolved, …)" while the same report says N tracked
 *             changes are still visible — nothing was resolved.
 *   3.6 (S3)  The keep-markup finalize report omits the open-comments
 *             listing ("Open comments: N" + per-comment lines) that the
 *             Python report includes — exactly what a lawyer needs before
 *             sending. (Root cause: the keep-markup branch of
 *             finalize_document never populates comments_kept.)
 *
 * Every test is written test-first: it fails on current main and passes once
 * the finding is fixed.
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
import { RedlineEngine } from "./engine.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { findAllDescendants } from "./docx/dom.js";
import { finalize_document } from "./sanitize/core.js";

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, true, false) as string;
}

async function reload(doc: DocumentObject): Promise<DocumentObject> {
  return DocumentObject.load(await doc.save());
}

async function rawProjection(doc: DocumentObject): Promise<string> {
  return extractTextFromBuffer(await doc.save());
}

const PARTY_SENTENCE =
  "Agreement between NordicTech and Adeu.ai, a Delaware corporation.";
const PARTY_COMMENT = "Updated party name and jurisdiction";

/** Builds the QA scenario: a tracked replacement carrying an anchored
 *  comment, both authored by "Claude" (a foreign author from the reviewing
 *  engine's point of view — the exact Cloud-MSA fixture shape). */
async function buildCommentedChangeDoc(): Promise<{
  doc: DocumentObject;
  del_id: string;
}> {
  const doc = await createTestDocument();
  addParagraph(doc, PARTY_SENTENCE);
  const engine = new RedlineEngine(doc, "Claude");
  const stats = engine.process_batch([
    {
      type: "modify",
      target_text: "Adeu.ai, a Delaware corporation",
      new_text: "Dealfluence Oy, a Finnish corporation",
      comment: PARTY_COMMENT,
    },
  ]);
  expect(stats.edits_applied).toBe(1); // setup sanity

  const reloaded = await reload(doc);
  const raw = await rawProjection(reloaded);
  expect(raw, "setup: the comment must be anchored").toContain(PARTY_COMMENT);
  const m = raw.match(/\[Chg:(\d+) delete\]/);
  expect(m, "setup: expected a tracked deletion bubble").toBeTruthy();
  return { doc: reloaded, del_id: m![1] };
}

// ---------------------------------------------------------------------------
// 1.1: accepting a tracked change silently deletes the anchored comment
// ---------------------------------------------------------------------------

describe("QA round 3, 1.1: accept must not delete the anchored comment", () => {
  it("keeps the comment visible after the change is accepted", async () => {
    const { doc, del_id } = await buildCommentedChangeDoc();

    const reviewer = new RedlineEngine(doc, "Reviewer");
    const [applied] = reviewer.apply_review_actions([
      { type: "accept", target_id: `Chg:${del_id}` },
    ]);
    expect(applied).toBe(1); // setup sanity

    // The surviving comment renders as a {==highlight==} over the accepted
    // text (Python parity), so the plain substring is asserted on the CLEAN
    // view and the comment on the raw projection.
    expect(cleanText(doc), "the accepted text must survive").toContain(
      "Dealfluence Oy, a Finnish corporation",
    );
    const raw_after = await rawProjection(doc);
    // Word semantics (and the Python engine): accepting a change keeps the
    // comment anchored on the surviving text. On current main the anchors
    // are detached and the foreign body is orphaned in word/comments.xml —
    // in legal review, accepting a redline destroys the negotiation record
    // attached to it.
    expect(
      raw_after,
      "accepting the change silently deleted the anchored comment " +
        "(QA round 3, finding 1.1 — hard parity break with Python)",
    ).toContain(PARTY_COMMENT);
  });

  it("keeps the comment anchors in the document body XML", async () => {
    const { doc, del_id } = await buildCommentedChangeDoc();
    const reviewer = new RedlineEngine(doc, "Reviewer");
    reviewer.apply_review_actions([
      { type: "accept", target_id: `Chg:${del_id}` },
    ]);

    const xml = strFromU8(
      unzipSync(new Uint8Array(await doc.save()))["word/document.xml"],
    );
    expect(
      xml.includes("w:commentReference"),
      "the comment reference was stripped from the body: the comment body " +
        "left in word/comments.xml is orphaned and invisible in Word",
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3.4a: a comment silently disappearing on reject must at least be REPORTED
// ---------------------------------------------------------------------------

describe("QA round 3, 3.4: comment removal must be visible or reported", () => {
  it("reject either keeps the comment or emits a note naming it", async () => {
    const { doc, del_id } = await buildCommentedChangeDoc();
    const reviewer = new RedlineEngine(doc, "Reviewer");
    const [applied] = reviewer.apply_review_actions([
      { type: "reject", target_id: `Chg:${del_id}` },
    ]);
    expect(applied).toBe(1); // setup sanity

    const raw_after = await rawProjection(doc);
    const commentStillVisible = raw_after.includes(PARTY_COMMENT);
    const notes = reviewer.skipped_details.join("\n");
    const removalReported = /also removed comment Com:\d/.test(notes);

    // Python reports "reject on Chg:N also removed comment Com:A, Com:B…".
    // Node's diff counts only deleted comment BODIES, and foreign bodies are
    // kept-but-orphaned, so the removal is completely silent.
    expect(
      commentStillVisible || removalReported,
      "rejecting the change made the anchored comment disappear from the " +
        "projection with no informational note (QA round 3, finding 3.4; " +
        `skipped_details: ${JSON.stringify(reviewer.skipped_details)})`,
    ).toBe(true);
  });

  it("accept_all_revisions reports how many comments it actually removed", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, PARTY_SENTENCE);
    addParagraph(doc, "The fee is ten dollars, payable net thirty.");
    const author = new RedlineEngine(doc, "Claude");
    author.process_batch([
      {
        type: "modify",
        target_text: "Adeu.ai, a Delaware corporation",
        new_text: "Dealfluence Oy, a Finnish corporation",
        comment: PARTY_COMMENT,
      },
      {
        type: "modify",
        target_text: "ten dollars",
        new_text: "twenty dollars",
        comment: "Price adjustment for 2026",
      },
    ]);

    const reloaded = await reload(doc);
    const before = await rawProjection(reloaded);
    expect(before).toContain(PARTY_COMMENT); // setup sanity
    expect(before).toContain("Price adjustment for 2026");

    const reviewer = new RedlineEngine(reloaded, "Reviewer");
    const res = reviewer.accept_all_revisions();

    const after = await rawProjection(reloaded);
    expect(after).not.toContain(PARTY_COMMENT); // both comments ARE ejected
    expect(after).not.toContain("Price adjustment for 2026");

    // …but the stats claim 0 removals, so the MCP summary says "Accepted:
    // N insertion(s), N deletion(s)" and never mentions the comments it
    // deleted (Python reports "Comments removed: N").
    expect(
      res.removed_comments,
      "accept_all_revisions ejected every comment in the document but " +
        "reported removed_comments=0 (QA round 3, finding 3.4)",
    ).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// 1.3: cell-anchor fill on a NON-empty cell garbles the cell text
// ---------------------------------------------------------------------------

describe("QA round 3, 1.3: anchor-write to a non-empty cell", () => {
  it("must not silently interleave new text with the existing cell text", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Signature block below.");
    const table = addTable(doc, 1, 2);
    setCellText(table, 0, 0, "By: ");
    setCellText(table, 0, 1, "Date: ");
    // Cell anchors render only for paragraphs carrying w14:paraId (real
    // Word documents always have them).
    const cellParas = findAllDescendants(table, "w:p");
    cellParas[0].setAttribute("w14:paraId", "1A26F0BF");
    cellParas[1].setAttribute("w14:paraId", "62EEA09B");

    const raw = await rawProjection(doc);
    expect(raw, "setup: cell anchor must render").toContain("{#cell:1A26F0BF}");

    const engine = new RedlineEngine(doc, "Adeu AI");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "{#cell:1A26F0BF}",
        new_text: "By: /s/ Test Signer",
      },
    ]);

    if (stats.edits_applied > 0) {
      const clean = cleanText(doc);
      expect(
        clean.includes("By: /s/ Test SignerBy:"),
        "anchor-write to a non-empty cell was applied as a silent PREPEND, " +
          `interleaving the existing cell text (QA round 3, finding 1.3):\n${clean}`,
      ).toBe(false);
    } else {
      const details = engine.skipped_details.join("\n");
      expect(
        details.toLowerCase(),
        "the anchor-write was skipped without explaining the non-empty-cell " +
          "situation to the agent",
      ).toContain("cell");
    }
  });
});

// ---------------------------------------------------------------------------
// 2.1: rejecting every enumerated id must not hard-fail on pair groups
// ---------------------------------------------------------------------------

async function buildChainedEditDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(
    doc,
    "Unless otherwise stated in the Order Form, invoiced charges are due " +
      "net ten (10) days from the invoice date.",
  );
  const engine = new RedlineEngine(doc, "Adeu AI");
  const stats = engine.process_batch([
    { type: "modify", target_text: "ten (10)", new_text: "twenty (20)" },
    {
      type: "modify",
      target_text: "twenty (20)",
      new_text: "twenty-one (21)",
    },
    {
      type: "modify",
      target_text: "from the invoice date.",
      new_text: "from the invoice date. Final decision noted.",
    },
  ]);
  expect(stats.edits_applied).toBe(3); // setup sanity
  return reload(doc);
}

describe("QA round 3, 2.1: reject of every enumerated id", () => {
  it("gives every consumed pair-group member the already_resolved treatment", async () => {
    const doc = await buildChainedEditDoc();
    const raw = await rawProjection(doc);
    const ids = Array.from(
      new Set(Array.from(raw.matchAll(/\[Chg:(\d+)/g), (m) => parseInt(m[1], 10))),
    ).sort((a, b) => a - b);
    expect(ids.length, "setup: expected the QA topology's 8 ids").toBe(8);

    const engine = new RedlineEngine(doc, "Counterparty");
    const [applied, skipped, already_resolved] = engine.apply_review_actions(
      ids.map((i) => ({ type: "reject", target_id: `Chg:${i}` })),
    );

    const failures = engine.skipped_details.filter((d) =>
      d.includes("Failed to apply action"),
    );
    expect(
      skipped === 0 && failures.length === 0,
      "rejecting the full enumerated id list hard-failed on a pair-group " +
        `member (applied=${applied}, skipped=${skipped}, ` +
        `already_resolved=${already_resolved}): ${failures.join(" | ")} — ` +
        "the natural read-then-reject-all loop is nondeterministically fatal " +
        "depending on group topology (QA round 3, finding 2.1)",
    ).toBe(true);
    expect(applied + already_resolved).toBe(ids.length);
  });
});

// ---------------------------------------------------------------------------
// 2.2: format-only change ids are readable but not actionable
// ---------------------------------------------------------------------------

describe("QA round 3, 2.2: format-only tracked changes", () => {
  it("an advertised [Chg:N format] id is actionable (or marked view-only)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Plain intro paragraph.");
    const p = addParagraph(doc, "3.2 Invoicing and Payment.");
    const xmlDoc = doc.element.ownerDocument!;
    const r = findAllDescendants(p, "w:r")[0];
    const rPr = xmlDoc.createElement("w:rPr");
    const rpc = xmlDoc.createElement("w:rPrChange");
    rpc.setAttribute("w:id", "901");
    rpc.setAttribute("w:author", "Mikko Korpela");
    rpc.setAttribute("w:date", "2026-01-22T16:16:00Z");
    rpc.appendChild(xmlDoc.createElement("w:rPr"));
    rPr.appendChild(rpc);
    r.insertBefore(rPr, r.firstChild);

    const raw = await rawProjection(doc);
    const m = raw.match(/\[Chg:(\d+) format([^\]]*)\]/);
    expect(m, `setup: expected a format-change bubble in:\n${raw}`).toBeTruthy();
    const [, chg_id, qualifier] = m!;

    const engine = new RedlineEngine(doc, "Reviewer");
    const [applied, skipped] = engine.apply_review_actions([
      { type: "accept", target_id: `Chg:${chg_id}` },
    ]);

    const markedViewOnly = /view.?only|not actionable/i.test(qualifier);
    expect(
      applied === 1 || markedViewOnly,
      `read_docx advertises [Chg:${chg_id} format] with no non-actionable ` +
        "marker, but accepting that id fails with 'no tracked change with " +
        "that id exists' — read and write disagree about what exists " +
        `(applied=${applied}, skipped=${skipped}; accept_all_changes CAN ` +
        "resolve formatting changes, only per-id targeting is missing) " +
        `details: ${engine.skipped_details.join(" | ")}`,
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2.3: literal \1 backreferences written silently
// ---------------------------------------------------------------------------

describe("QA round 3, 2.3: Python-style backreferences in regex new_text", () => {
  it("warns when new_text contains \\1 (the mirror of Python's $1 guard)", async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      "Unless otherwise stated, invoiced charges are due net ten (10) days " +
        "from the invoice date.",
    );
    const engine = new RedlineEngine(doc, "Adeu AI");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "net (ten) \\((10)\\) days",
        new_text: "\\1 (\\2) calendar days",
        regex: true,
      },
    ]);
    expect(stats.edits_applied).toBe(1); // setup sanity — the edit applies

    // JS String.replace does not expand \1, so the literal "\1 (\2)" landed
    // in the document. Python's engine warns on the mirror-image trap ($1 in
    // Python) — Node must warn on \1/\g<1> the same way, or an agent moving
    // between the two servers will corrupt documents silently.
    const report = stats.edits[0];
    const warning = (report && report.warning) || "";
    expect(
      /\\1|\$1|backreference/i.test(warning),
      "a literal '\\1 (\\2)' was written into the document as a tracked " +
        "change with NO warning (QA round 3, finding 2.3); report.warning=" +
        JSON.stringify(report && report.warning),
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3.5 / 3.6: keep-markup finalize report wording and omissions
// ---------------------------------------------------------------------------

async function buildKeepMarkupReport(): Promise<string> {
  const doc = await createTestDocument();
  addParagraph(doc, PARTY_SENTENCE);
  const engine = new RedlineEngine(doc, "Claude");
  const stats = engine.process_batch([
    {
      type: "modify",
      target_text: "Adeu.ai, a Delaware corporation",
      new_text: "Dealfluence Oy, a Finnish corporation",
      comment: PARTY_COMMENT,
    },
  ]);
  expect(stats.edits_applied).toBe(1); // setup sanity

  const reloaded = await reload(doc);
  const res = await finalize_document(reloaded, {
    filename: "keepmarkup_fixture.docx",
    sanitize_mode: "keep-markup",
    author: "Redacted Reviewer",
  } as any);
  return res.reportText;
}

describe("QA round 3, 3.5/3.6: keep-markup finalize report", () => {
  it("3.6: lists the open comments visible to the counterparty", async () => {
    const report = await buildKeepMarkupReport();
    expect(report, "setup: tracked changes are visible").toContain(
      "Tracked changes:",
    );
    // Python's report enumerates each open comment with its author under
    // VISIBLE TO COUNTERPARTY — exactly what a lawyer needs before sending.
    // Node's keep-markup branch never populates comments_kept, so the
    // listing is silently absent.
    expect(
      report,
      "the keep-markup report omits the open-comments listing " +
        `(QA round 3, finding 3.6):\n${report}`,
    ).toContain("Open comments: 1");
  });

  it("3.5: does not claim changes were 'resolved' when markup was kept", async () => {
    const report = await buildKeepMarkupReport();
    expect(
      /Result: CLEAN \(\d+ changes resolved/.test(report),
      "the report says 'Result: CLEAN (N changes resolved…)' while the same " +
        "report shows N tracked changes still visible to the counterparty — " +
        `in keep-markup mode nothing was resolved (QA round 3, finding 3.5):\n${report}`,
    ).toBe(false);
  });
});
