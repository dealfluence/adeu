// FILE: node/packages/core/src/repro.feedback.test.ts
//
// Regression repros for the field observations against the Node engine.
//
// STYLE: these assert the DESIRED (correct) behaviour, so a test is RED while
// the bug is present and turns GREEN once the engine is fixed. (This is the
// "isolate the bug before fixing" pattern from AI_CONTEXT.md > Testing.)
//
//   Issue 1 — accept + modify the same span in one batch  -> currently RED (bug)
//   Issue 2 — comment-only run-shredding                  -> NOT a bug; see note
//
// (Issue 3 lives in node/packages/mcp-server/src/repro.feedback.test.ts.)

import { describe, it, expect } from "vitest";
import { RedlineEngine } from "./engine.js";
import { createTestDocument, addParagraph } from "./test-utils.js";

/** Append a paragraph that already contains a tracked insertion by `author`. */
function addParagraphWithForeignInsertion(
  doc: any,
  prefix: string,
  insertedText: string,
  insId: string,
  author: string,
): Element {
  const x = doc.element.ownerDocument!;
  const p = x.createElement("w:p");
  const r0 = x.createElement("w:r");
  const t0 = x.createElement("w:t");
  t0.textContent = prefix;
  t0.setAttribute("xml:space", "preserve");
  r0.appendChild(t0);
  p.appendChild(r0);

  const ins = x.createElement("w:ins");
  ins.setAttribute("w:id", insId);
  ins.setAttribute("w:author", author);
  ins.setAttribute("w:date", "2024-01-01T00:00:00Z");
  const r = x.createElement("w:r");
  const t = x.createElement("w:t");
  t.textContent = insertedText;
  r.appendChild(t);
  ins.appendChild(r);
  p.appendChild(ins);

  doc.element.appendChild(p);
  return p;
}

function countTag(el: any, tag: string): number {
  let n = 0;
  const walk = (node: any) => {
    if (node.tagName === tag) n++;
    let c = node.firstChild;
    while (c) {
      if (c.nodeType === 1) walk(c);
      c = c.nextSibling;
    }
  };
  walk(el);
  return n;
}

describe("Field feedback repro — Node engine", () => {
  // ───────────────────────────────────────────────────────────────────────
  // ISSUE 1 — RED until fixed.
  //
  // Accepting a foreign author's insertion and then editing that now-accepted
  // text, in a SINGLE batch, MUST succeed: by the time the modify runs the span
  // has been accepted and is no longer foreign. Node rejects it because the
  // unified up-front validation pass (engine.ts `_process_batch_internal`, the
  // `validate_edits(edits)` call BEFORE `apply_review_actions`) validates the
  // modify against the PRE-accept document. This test fails today and will pass
  // once validation runs after the accepts are applied.
  // ───────────────────────────────────────────────────────────────────────
  it("Issue 1: accept + modify of the SAME foreign insertion in one batch should succeed", async () => {
    const doc = await createTestDocument();
    addParagraphWithForeignInsertion(
      doc,
      "The term is ",
      "24 months",
      "5",
      "Supplier's Counsel",
    );

    const engine = new RedlineEngine(doc, "Acme's Counsel");
    const batch = [
      { type: "accept", target_id: "Chg:5" },
      { type: "modify", target_text: "24 months", new_text: "36 months" },
    ] as any[];

    // DESIRED behaviour — RED on current Node (throws "active insertion from
    // another author"), GREEN after the fix.
    const stats = engine.process_batch(batch, false);
    expect(stats.actions_applied).toBe(1);
    expect(stats.edits_applied).toBe(1);
    expect(stats.edits_skipped).toBe(0);
  });

  it("Issue 1 (control, GREEN): the same accept + modify succeeds across two batches", async () => {
    // Proves the rejection above is purely a single-round-trip batching
    // artifact — the content and intent are valid.
    const doc = await createTestDocument();
    addParagraphWithForeignInsertion(
      doc,
      "The term is ",
      "24 months",
      "5",
      "Supplier's Counsel",
    );

    const engine = new RedlineEngine(doc, "Acme's Counsel");
    const r1 = engine.process_batch([{ type: "accept", target_id: "Chg:5" }], false);
    expect(r1.actions_applied).toBe(1);
    const r2 = engine.process_batch(
      [{ type: "modify", target_text: "24 months", new_text: "36 months" }],
      false,
    );
    expect(r2.edits_applied).toBe(1);
  });

  // ───────────────────────────────────────────────────────────────────────
  // ISSUE 2 — NOT a bug in the current engine, so there is nothing to fail on.
  // The described mechanism (one match whose physical runs are shredded →
  // comment id duplicated per run segment) does not occur: COMMENT_ONLY anchors
  // a single range across the first→last run regardless of fragmentation. These
  // assertions are GREEN BY DESIGN and exist as evidence for the "not
  // replicable" conclusion. (The real gap — no dedicated comment-only change
  // type, so comments must ride a self-replacing ModifyText — is a feature
  // request, not a failing-test-able defect.)
  // ───────────────────────────────────────────────────────────────────────
  it("Issue 2 (GREEN, evidence): self-replacement comment over 9 fragmented runs emits ONE comment", async () => {
    const doc = await createTestDocument();
    const x = doc.element.ownerDocument!;
    const p = x.createElement("w:p");
    const words = [
      "The ", "Purchase ", "Price ", "is ", "[   ] ",
      "dollars ", "per ", "unit ", "total.",
    ];
    words.forEach((w, i) => {
      if (i % 2 === 0) p.appendChild(x.createElement("w:proofErr"));
      const r = x.createElement("w:r");
      const t = x.createElement("w:t");
      t.textContent = w;
      t.setAttribute("xml:space", "preserve");
      r.appendChild(t);
      p.appendChild(r);
    });
    doc.element.appendChild(p);

    const engine = new RedlineEngine(doc, "Reviewer");
    const sentence = words.join("").trim();
    engine.process_batch(
      [{ type: "modify", target_text: sentence, new_text: sentence, comment: "Missing value" }],
      false,
    );

    expect(countTag(doc.element, "w:commentReference")).toBe(1);
  });
});
