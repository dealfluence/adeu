// FILE: node/packages/core/src/repro.rejected-batch-action-leak.test.ts
/**
 * BUG 2026-08-12 — a REJECTED batch left its review actions in the document.
 *
 * Observed over MCP on a protective order: the agent sent
 * `[reply Com:1, modify …]` three times; batches 1 and 2 were rejected with
 * "Batch rejected. Some edits failed validation:", batch 3 succeeded — and the
 * saved file carried THREE replies from the agent on Sarah Chen's single
 * comment, in reverse order.
 *
 * Root cause: `_process_batch_internal` split the batch into `actions`
 * (accept/reject/reply) and `edits`, applied the actions FIRST, and only then
 * took the transactional snapshot the edit loop rolls back to. The snapshot
 * therefore already contained the reply, so "rolled back and nothing was
 * saved" was true of the edits and false of the actions. The MCP layer then
 * re-pinned that DOM in its hot slot for the retry, so every rejected attempt
 * accumulated one more reply on the SAME in-memory document.
 *
 * The contract under test is the one the rejection message states: a batch is
 * ONE transaction. If it is rejected, the document is byte-for-byte what it
 * was before the call — actions included.
 *
 * Written test-first: every case here fails on pre-fix main.
 */

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants } from "./docx/dom.js";
import { extract_comments_data } from "./comments.js";
import { extractTextFromBuffer } from "./ingest.js";
import { BatchValidationError, RedlineEngine } from "./engine.js";

const REVIEWER = "Sarah Chen";
const AGENT = "Adeu AI (TS)";
const BODY =
  "Discovery Material may be disclosed to outside counsel of record and to " +
  "any person to whom disclosure is reasonably necessary for this litigation.";
const ANCHOR = "reasonably necessary";
const REVIEW_NOTE = "Please add an attorneys'-eyes-only tier.";
const REPLY = "Updated — added the AEO tier per your 28 July note.";
const NO_SUCH_TEXT = "TEXT THAT IS NOT ANYWHERE IN THIS DOCUMENT";

/** A document carrying exactly one reviewer comment and nothing else. */
async function reviewedDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, BODY);
  new RedlineEngine(doc, REVIEWER).apply_edits([
    {
      type: "modify",
      target_text: ANCHOR,
      new_text: ANCHOR,
      comment: REVIEW_NOTE,
    } as any,
  ]);
  return DocumentObject.load(await doc.save());
}

/** A document carrying one pending tracked change by the REVIEWER. */
async function revisedDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, BODY);
  new RedlineEngine(doc, REVIEWER).apply_edits([
    { type: "modify", target_text: "outside counsel", new_text: "outside trial counsel" } as any,
  ]);
  return DocumentObject.load(await doc.save());
}

/**
 * A document with TWO reviewer comments, the second of which cannot be
 * replied to: `EG_BlockLevelElts` is `minOccurs="0"`, so a `<w:comment>` with
 * no paragraph is schema-legal and has no paragraph identity to thread onto —
 * `apply_review_actions` SKIPS such a reply (CommentThreadingError) instead of
 * silently rooting a new top-level comment.
 */
async function twoCommentsSecondUnthreadable(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, BODY);
  new RedlineEngine(doc, REVIEWER).apply_edits([
    { type: "modify", target_text: ANCHOR, new_text: ANCHOR, comment: REVIEW_NOTE } as any,
    {
      type: "modify",
      target_text: "outside counsel",
      new_text: "outside counsel",
      comment: "And define who counts as counsel of record.",
    } as any,
  ]);
  const reloaded = await DocumentObject.load(await doc.save());
  const ids = commentIds(reloaded);
  expect(ids.length, "fixture precondition: two reviewer comments").toBe(2);

  const part = reloaded.pkg.parts.find((p) =>
    p.contentType.endsWith("comments+xml"),
  )!;
  const victim = findAllDescendants(part._element, "w:comment").find(
    (c) => c.getAttribute("w:id") === ids[1],
  )!;
  for (const child of Array.from(victim.childNodes)) victim.removeChild(child);
  return reloaded;
}

function commentIds(doc: DocumentObject): string[] {
  return Object.keys(extract_comments_data(doc.pkg)).sort();
}

function commentTexts(doc: DocumentObject): string[] {
  return Object.values(extract_comments_data(doc.pkg)).map((c: any) =>
    String(c.text ?? "").trim(),
  );
}

/** How many times `text` appears as a comment body in the SAVED package. */
async function savedReplyCount(doc: DocumentObject, text: string): Promise<number> {
  const saved = await DocumentObject.load(await doc.save());
  return commentTexts(saved).filter((t) => t === text).length;
}

describe("a rejected batch leaves the document exactly as it was", () => {
  it("rolls back a reply when a later edit fails validation", async () => {
    const doc = await reviewedDoc();
    const before = commentIds(doc);
    expect(before.length, "fixture precondition: one reviewer comment").toBe(1);

    const engine = new RedlineEngine(doc, AGENT);
    expect(() =>
      engine.process_batch([
        { type: "reply", target_id: `Com:${before[0]}`, text: REPLY } as any,
        { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" } as any,
      ]),
    ).toThrow(BatchValidationError);

    expect(
      commentIds(doc),
      "the rejected batch's reply survived the rollback in the live DOM",
    ).toEqual(before);
    expect(
      await savedReplyCount(doc, REPLY),
      "the rejected batch's reply survived into the saved package",
    ).toBe(0);
  });

  it("rolls back an accept when a later edit fails validation", async () => {
    const doc = await revisedDoc();
    const pending = await extractTextFromBuffer(await doc.save());
    const chg = pending.match(/\[Chg:(\d+)/);
    expect(chg, "fixture precondition: one pending tracked change").not.toBeNull();

    const engine = new RedlineEngine(doc, AGENT);
    expect(() =>
      engine.process_batch([
        { type: "accept", target_id: `Chg:${chg![1]}` } as any,
        { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" } as any,
      ]),
    ).toThrow(BatchValidationError);

    const after = await extractTextFromBuffer(await doc.save());
    expect(
      after,
      "the rejected batch committed the reviewer's tracked change anyway",
    ).toBe(pending);
  });

  it("rolls back applied actions when a LATER ACTION in the same batch fails", async () => {
    // No edits at all: the pre-fix code path threw on skipped_actions before a
    // snapshot even existed, so there was nothing to roll back to. Action 2
    // gets past validation (its target comment exists) and only fails at apply
    // time, which is the only shape that can strand action 1.
    const doc = await twoCommentsSecondUnthreadable();
    const before = commentIds(doc);

    const engine = new RedlineEngine(doc, AGENT);
    expect(() =>
      engine.process_batch([
        { type: "reply", target_id: `Com:${before[0]}`, text: REPLY } as any,
        { type: "reply", target_id: `Com:${before[1]}`, text: "Noted." } as any,
      ]),
    ).toThrow(BatchValidationError);

    expect(
      commentIds(doc),
      "action 1 stayed applied after action 2 rejected the batch",
    ).toEqual(before);
    expect(await savedReplyCount(doc, REPLY)).toBe(0);
  });

  it("reproduces the reported run: two rejected retries then a success leave ONE reply", async () => {
    // The MCP shape exactly: one DOM reused across calls (hot slot), a fresh
    // engine per call, the same reply re-sent every time.
    const doc = await reviewedDoc();
    const cid = commentIds(doc)[0];
    const reply = { type: "reply", target_id: `Com:${cid}`, text: REPLY };

    for (const attempt of [1, 2]) {
      expect(
        () =>
          new RedlineEngine(doc, AGENT).process_batch([
            { ...reply } as any,
            { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" } as any,
          ]),
        `attempt ${attempt} must be rejected`,
      ).toThrow(BatchValidationError);
    }

    const stats = new RedlineEngine(doc, AGENT).process_batch([
      { ...reply } as any,
      { type: "modify", target_text: ANCHOR, new_text: "strictly necessary" } as any,
    ]);
    expect(stats.actions_applied).toBe(1);
    expect(stats.edits_applied).toBe(1);

    expect(
      await savedReplyCount(doc, REPLY),
      "the two rejected retries each left a duplicate reply behind",
    ).toBe(1);
    const saved = await DocumentObject.load(await doc.save());
    expect(commentIds(saved).length, "one reviewer comment + one reply").toBe(2);
  });

  it("keeps a successful action+edit batch working (no over-rollback)", async () => {
    const doc = await reviewedDoc();
    const cid = commentIds(doc)[0];

    const stats = new RedlineEngine(doc, AGENT).process_batch([
      { type: "reply", target_id: `Com:${cid}`, text: REPLY } as any,
      { type: "modify", target_text: ANCHOR, new_text: "strictly necessary" } as any,
    ]);

    expect(stats.actions_applied).toBe(1);
    expect(stats.edits_applied).toBe(1);
    expect(await savedReplyCount(doc, REPLY)).toBe(1);
    const text = await extractTextFromBuffer(await doc.save());
    expect(text).toContain("{--reasonably--}");
    expect(text).toContain("{++strictly++}");
  });
});

// ---------------------------------------------------------------------------
// The invariant behind the fix
// ---------------------------------------------------------------------------

describe("rollback_verified: the engine checks its own rollback", () => {
  it("is true after a batch that rejected and rolled back cleanly", async () => {
    const doc = await reviewedDoc();
    const cid = commentIds(doc)[0];
    const engine = new RedlineEngine(doc, AGENT);

    expect(engine.rollback_verified, "starts clean").toBe(true);
    expect(() =>
      engine.process_batch([
        { type: "reply", target_id: `Com:${cid}`, text: REPLY } as any,
        { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" } as any,
      ]),
    ).toThrow(BatchValidationError);
    expect(engine.rollback_verified).toBe(true);
  });

  it("goes false when the rollback does not actually restore the document", async () => {
    // Defeat the restore to simulate any future regression in it. The point of
    // the invariant is that a leak is DETECTED rather than handed to a caller
    // as a document "identical to disk" — the MCP layer keys its hot-DOM reuse
    // off this flag.
    const doc = await reviewedDoc();
    const cid = commentIds(doc)[0];
    const engine = new RedlineEngine(doc, AGENT);
    (engine as any)._restore_batch_snapshot = () => {
      /* rollback regression */
    };

    expect(() =>
      engine.process_batch([
        { type: "reply", target_id: `Com:${cid}`, text: REPLY } as any,
        { type: "modify", target_text: NO_SUCH_TEXT, new_text: "x" } as any,
      ]),
    ).toThrow(BatchValidationError);

    expect(
      engine.rollback_verified,
      "a document mutated by a rejected batch must not report a verified rollback",
    ).toBe(false);
  });

  it("resets per batch rather than latching", async () => {
    const doc = await reviewedDoc();
    const cid = commentIds(doc)[0];
    const engine = new RedlineEngine(doc, AGENT);
    (engine as any).rollback_verified = false;

    engine.process_batch([
      { type: "reply", target_id: `Com:${cid}`, text: REPLY } as any,
    ]);
    expect(engine.rollback_verified).toBe(true);
  });
});
