// FILE: node/packages/core/src/repro_qa_report_v8.test.ts
/**
 * Node-engine repro tests for the 2026-07-19 black-box QA and UX report on
 * 1.26.0 (adeu 1.26.0+0741eaf). Mirrors python/tests/test_repro_qa_report_v8.py
 * for the findings that live in the shared core engine:
 *
 *   F-04  replacing a full sentence that crosses bold/italic formatting runs
 *         leaves a partial word bold (`**The Suppli** must perform ...`):
 *         the word-diff hunk absorbs the closing `**` marker, the resolved
 *         range starts on a virtual span, and the run-local split offset
 *         underflows into the preceding run's kept text
 *   F-07  review-action validation permits blank replies and duplicate /
 *         conflicting accept-reject pairs on one target_id
 *
 * Every test fails against the commit preceding its fix.
 */

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";

function addStyledRun(
  p: Element,
  text: string,
  style: { bold?: boolean; italic?: boolean; underline?: boolean } = {},
): Element {
  const xmlDoc = p.ownerDocument!;
  const r = xmlDoc.createElement("w:r");
  if (style.bold || style.italic || style.underline) {
    const rPr = xmlDoc.createElement("w:rPr");
    if (style.bold) rPr.appendChild(xmlDoc.createElement("w:b"));
    if (style.italic) rPr.appendChild(xmlDoc.createElement("w:i"));
    if (style.underline) {
      const u = xmlDoc.createElement("w:u");
      u.setAttribute("w:val", "single");
      rPr.appendChild(u);
    }
    r.appendChild(rPr);
  }
  const t = xmlDoc.createElement("w:t");
  t.textContent = text;
  if (text !== text.trim()) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  p.appendChild(r);
  return r;
}

function addEmptyParagraph(doc: DocumentObject): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  doc.element.appendChild(p);
  return p;
}

/** The QA report's F-04 fixture: bold "The Supplier ", italic
 * "shall provide ", underlined remainder — one sentence, three runs. */
async function buildCrossRunDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  const p = addEmptyParagraph(doc);
  addStyledRun(p, "The Supplier ", { bold: true });
  addStyledRun(p, "shall provide ", { italic: true });
  addStyledRun(p, "the Services with reasonable skill and care.", {
    underline: true,
  });
  return doc;
}

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, {
    cleanView: true,
    includeAppendix: false,
  }) as string;
}

/** A document carrying exactly one tracked modification (Chg:1 + Chg:2). */
async function buildTrackedChangeDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Payment is due in 30 days.");
  addParagraph(doc, "Second paragraph here.");
  const engine = new RedlineEngine(doc);
  engine.apply_edits([
    { type: "modify", target_text: "30 days", new_text: "60 days" },
  ]);
  return DocumentObject.load(await doc.save());
}

/** A document carrying exactly one comment; returns [doc, "Com:<id>"]. */
async function buildCommentDoc(): Promise<[DocumentObject, string]> {
  const doc = await createTestDocument();
  addParagraph(doc, "Alpha beta gamma.");
  addParagraph(doc, "Delta epsilon.");
  const engine = new RedlineEngine(doc);
  engine.process_batch([
    {
      type: "modify",
      target_text: "Alpha beta gamma.",
      new_text: "Alpha beta gamma.",
      comment: "Please review.",
    },
  ]);
  const buf = await doc.save();
  const reloaded = await DocumentObject.load(buf);
  const projected = await extractTextFromBuffer(buf);
  const m = projected.match(/\[Com:(\d+)/);
  expect(m, `expected a comment id in ${projected}`).toBeTruthy();
  return [reloaded, `Com:${m![1]}`];
}

// ---------------------------------------------------------------------------
// F-04: full-span replacements across formatting runs keep words whole
// ---------------------------------------------------------------------------

describe("QA v8 F-04: cross-run full-span replacement fidelity", () => {
  const TARGET =
    "The Supplier shall provide the Services with reasonable skill and care.";
  const NEW = "The Supplier must perform the Services professionally.";

  it("leaves no partial-word bold after the replacement", async () => {
    const doc = await buildCrossRunDoc();
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      { type: "modify", target_text: TARGET, new_text: NEW },
    ]);
    expect(stats.edits_applied).toBe(1);

    const clean = cleanText(doc);
    expect(clean).not.toContain("**The Suppli**");
    expect(clean).not.toContain("Suppli**");
    expect(clean.trim()).toBe(
      "**The Supplier** must perform the Services professionally.",
    );
  });

  it("accepted document matches the clean view", async () => {
    const doc = await buildCrossRunDoc();
    const engine = new RedlineEngine(doc);
    engine.process_batch([
      { type: "modify", target_text: TARGET, new_text: NEW },
    ]);
    engine.accept_all_revisions();
    const accepted = _extractTextFromDoc(doc, {
      cleanView: false,
      includeAppendix: false,
    }) as string;
    expect(accepted.trim()).toBe(
      "**The Supplier** must perform the Services professionally.",
    );
  });
});

// ---------------------------------------------------------------------------
// Hunt-profile counterexample (python property suite, 2026-07-19): a
// paragraph-splitting insertion at paragraph START must relocate the host
// paragraph's content into the LAST new paragraph. Deterministic pin,
// mirrored here because both engines shared the suffix-relocation logic.
// ---------------------------------------------------------------------------

describe("QA v8: paragraph-start splitting insertion", () => {
  it("relocates the host paragraph content into the last new paragraph", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "00.");
    const engine = new RedlineEngine(doc);
    const edit: any = {
      type: "modify",
      target_text: "",
      new_text: "0.\n\n0 ",
      _match_start_index: 0,
    };
    engine.apply_edits([edit]);
    engine.accept_all_revisions();
    const final = _extractTextFromDoc(doc, {
      cleanView: true,
      includeAppendix: false,
    }) as string;
    expect(final.trim()).toBe("0.\n\n0 00.");
  });
});

// ---------------------------------------------------------------------------
// F-07: blank replies and duplicate review actions are rejected
// ---------------------------------------------------------------------------

describe("QA v8 F-07: review-action validation", () => {
  it("rejects a blank reply", async () => {
    const [doc, comId] = await buildCommentDoc();
    const engine = new RedlineEngine(doc);
    expect(() =>
      engine.process_batch([{ type: "reply", target_id: comId, text: "   " }]),
    ).toThrowError(BatchValidationError);
    try {
      engine.process_batch([{ type: "reply", target_id: comId, text: "   " }]);
    } catch (e: any) {
      expect(e.errors.join("\n").toLowerCase()).toContain("empty");
    }
  });

  it("rejects a duplicate accept of the same target_id", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc);
    try {
      engine.process_batch([
        { type: "accept", target_id: "Chg:1" },
        { type: "accept", target_id: "Chg:1" },
      ]);
      expect.unreachable("duplicate accept must be rejected");
    } catch (e: any) {
      expect(e).toBeInstanceOf(BatchValidationError);
      expect(e.errors.join("\n").toLowerCase()).toContain("duplicate");
    }
  });

  it("rejects conflicting accept + reject on one target_id", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc);
    try {
      engine.process_batch([
        { type: "accept", target_id: "Chg:1" },
        { type: "reject", target_id: "Chg:1" },
      ]);
      expect.unreachable("conflicting actions must be rejected");
    } catch (e: any) {
      expect(e).toBeInstanceOf(BatchValidationError);
      expect(e.errors.join("\n").toLowerCase()).toContain("conflict");
    }
  });

  it("rejects a duplicated identical reply", async () => {
    const [doc, comId] = await buildCommentDoc();
    const engine = new RedlineEngine(doc);
    try {
      engine.process_batch([
        { type: "reply", target_id: comId, text: "Same reply." },
        { type: "reply", target_id: comId, text: "Same reply." },
      ]);
      expect.unreachable("duplicate identical reply must be rejected");
    } catch (e: any) {
      expect(e).toBeInstanceOf(BatchValidationError);
      expect(e.errors.join("\n").toLowerCase()).toContain("duplicate");
    }
  });

  it("still allows distinct replies to the same comment", async () => {
    const [doc, comId] = await buildCommentDoc();
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      { type: "reply", target_id: comId, text: "First reply." },
      { type: "reply", target_id: comId, text: "Second reply." },
    ]);
    expect(stats.actions_applied).toBe(2);
    expect(stats.actions_skipped).toBe(0);
  });

  it("still allows accepting the del+ins pair of one modification", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      { type: "accept", target_id: "Chg:1" },
      { type: "accept", target_id: "Chg:2" },
    ]);
    expect(stats.actions_skipped).toBe(0);
    expect(cleanText(doc)).toContain("60 days");
  });
});
