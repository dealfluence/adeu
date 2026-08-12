// FILE: node/packages/core/src/repro.comment-threading-anchoring-typography.test.ts
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants } from "./docx/dom.js";
import { CommentsManager, extract_comments_data } from "./comments.js";
import { extractTextFromBuffer } from "./ingest.js";
import { BatchValidationError, RedlineEngine } from "./engine.js";
import { DocumentMapper } from "./mapper.js";
import {
  SMART_QUOTE_MAP,
  normalize_smart_quotes,
  restore_document_typography,
} from "./utils/text.js";

/**
 * Node mirror of BUG_comment_threading_anchoring_and_typography.md
 * (reported 2026-08-11 against Adeu 2.1.0 / 56a97cf; B1 was OBSERVED on the
 * Node engine over MCP, B3 was Word-verified, B4 observed in the artifact).
 *
 *  B1  `reply` silently creates a new TOP-LEVEL comment when
 *      `_findThreadRootParaId` resolves nothing (the parent comment carries no
 *      w14:paraId — the ordinary legacy / third-party comment shape).
 *      `w15:paraIdParent` is never written and apply_review_actions still
 *      reports 1 applied, so the agent retries and makes the document worse.
 *  B2  accept_all_revisions unconditionally ejected every comment with no way
 *      to keep them (the Python API defaults to remove_comments=False), and a
 *      comment deleted because its anchor was consumed is never attributed.
 *  B3  `w16cid:durableId` came from the general-purpose `_generateHexId()`
 *      across the full 32-bit range. Word parses ST_LongHexNumber as a SIGNED
 *      32-bit integer, so ~half of all Adeu comments open anchored to nothing.
 *  B4  The matcher is smart-quote-insensitive while the writer word-diffs the
 *      document's real slice against the caller's literal new_text, so every
 *      typographic difference lands as a real tracked change.
 */

const CURLY_BODY =
  "All Discovery Material designated as \u201cConfidential\u201d under the " +
  "parties\u2019 Master Agreement shall be produced within thirty days.";

const CT = {
  COMMENTS:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
  EXTENDED:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
  IDS: "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
};

function partXml(doc: DocumentObject, contentType: string): string {
  const part = doc.pkg.parts.find((p) => p.contentType === contentType);
  if (!part) throw new Error(`no part with content type ${contentType}`);
  return part._element.toString();
}

/** A document carrying one comment by `author`, anchored to `target`. */
async function annotatedDoc(
  body: string,
  target: string,
  comment: string,
  author: string,
): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, body);
  const engine = new RedlineEngine(doc, author);
  engine.apply_edits([
    { type: "modify", target_text: target, new_text: target, comment } as any,
  ]);
  return await DocumentObject.load(await doc.save());
}

/**
 * Drops `w14:paraId` from every comment paragraph — the ordinary LEGACY comment
 * shape (pre-2013 Word, and every generator that skips the modern-comments
 * extensions). The commentsExtended / commentsIds parts stay, so the document
 * is still on Word's modern-comments path.
 */
function stripCommentParaIds(doc: DocumentObject): void {
  const part = doc.pkg.parts.find((p) => p.contentType === CT.COMMENTS)!;
  let stripped = 0;
  for (const c of findAllDescendants(part._element, "w:comment")) {
    for (const p of findAllDescendants(c, "w:p")) {
      if (p.getAttribute("w14:paraId")) {
        p.removeAttribute("w14:paraId");
        stripped++;
      }
    }
  }
  expect(stripped, "fixture precondition: no w14:paraId to strip").toBeGreaterThan(0);
}

/**
 * Strips every block-level child from each `<w:comment>`, leaving a comment with
 * NO paragraph at all. `EG_BlockLevelElts` is `minOccurs="0"` so this is
 * schema-legal, and it is the one shape where a paragraph identity genuinely
 * cannot be minted — i.e. where threading is truly impossible. What must NOT
 * happen is a silent top-level comment.
 */
function emptyCommentBodies(doc: DocumentObject): void {
  const part = doc.pkg.parts.find((p) => p.contentType === CT.COMMENTS)!;
  let emptied = 0;
  for (const c of findAllDescendants(part._element, "w:comment")) {
    for (const child of Array.from(c.childNodes)) {
      c.removeChild(child);
      emptied++;
    }
  }
  expect(emptied, "fixture precondition: no comment body to strip").toBeGreaterThan(0);
}

// ---------------------------------------------------------------------------
// B3 — durableId high bit silently unanchors comments
// ---------------------------------------------------------------------------

describe("B3: w16cid:durableId must be a positive signed int32", () => {
  // 256 samples: a generator over the full 32-bit range fails with
  // probability 1 - 2^-256, i.e. deterministically in practice.
  const SAMPLES = 256;

  it("never mints a high-bit-set durableId", async () => {
    const doc = await createTestDocument();
    const mgr = new CommentsManager(doc) as any;
    const negatives: string[] = [];
    for (let i = 0; i < SAMPLES; i++) {
      const value = mgr._generateDurableId
        ? mgr._generateDurableId()
        : mgr._generateHexId();
      expect(value).toMatch(/^[0-9A-F]{8}$/);
      if (parseInt(value, 16) > 0x7fffffff) negatives.push(value);
    }
    expect(
      negatives.length,
      `${negatives.length}/${SAMPLES} durableIds have the high bit set (e.g. ` +
        `${negatives.slice(0, 4).join(", ")}). Word reads w16cid:durableId as a ` +
        `SIGNED 32-bit integer: a negative id silently collapses the comment anchor.`,
    ).toBe(0);
  });

  it("writes only Word-readable durableIds into commentsIds.xml", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Alpha. Beta. Gamma. Delta. Epsilon.");
    const mgr = new CommentsManager(doc);
    for (let i = 0; i < 40; i++) mgr.addComment("Adeu AI (TS)", `note ${i}`);

    const negatives: string[] = [];
    for (const el of findAllDescendants(
      doc.pkg.parts.find((p) => p.contentType === CT.IDS)!._element,
      "w16cid:commentId",
    )) {
      const value = el.getAttribute("w16cid:durableId")!;
      if (parseInt(value, 16) > 0x7fffffff) negatives.push(value);
    }
    expect(
      negatives.length,
      `commentsIds.xml carries ${negatives.length} negative durableIds ` +
        `(${negatives.slice(0, 4).join(", ")}); Word drops the anchor for each`,
    ).toBe(0);
  });

  it("masks paraId and rsid exactly like durableId", async () => {
    // RETRACTION. This used to assert the opposite — that the shared hex
    // generator "keeps the full 32-bit range" because only durableId carried
    // the signed-int32 constraint. That belief was wrong, it was pinned here,
    // and it is why the same bug shipped again three weeks later: an
    // out-of-range w14:paraId is discarded by Word exactly like a durableId,
    // dangling every w15:paraIdParent that pointed at it
    // (BUG_paraId_signed_int32_thread_collapse.md, B5, Word-verified).
    // Full coverage in repro.para-id-signed-int32.test.ts.
    const doc = await createTestDocument();
    const mgr = new CommentsManager(doc) as any;
    const values: string[] = [];
    for (let i = 0; i < 512; i++) {
      values.push(mgr._generateHexId(), mgr._generateDurableId());
    }
    expect(
      values.every((v) => parseInt(v, 16) > 0 && parseInt(v, 16) <= 0x7fffffff),
      "paraId, rsid and durableId are all ST_LongHexNumber and Word reads all " +
        "three as signed 32-bit integers: they share one generator and one range",
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// B1 — reply silently produces a new top-level comment
// ---------------------------------------------------------------------------

describe("B1: reply must thread or fail loudly", () => {
  it("threads onto a parent that carries no w14:paraId", async () => {
    const doc = await annotatedDoc(
      "The parties shall confer in good faith before moving to compel.",
      "confer in good faith",
      "This should reference the protective order.",
      "Sarah Chen",
    );
    stripCommentParaIds(doc);

    const engine = new RedlineEngine(doc, "Agent");
    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];
    const [applied, skipped] = engine.apply_review_actions([
      {
        type: "reply",
        target_id: `Com:${parentId}`,
        text: "Addressed in the revised clause.",
      },
    ]);
    expect([applied, skipped]).toEqual([1, 0]);

    const data = extract_comments_data(doc.pkg);
    const replies = Object.entries(data).filter(([cid]) => cid !== parentId);
    expect(replies.length).toBe(1);
    expect(
      (replies[0][1] as any).parent_id,
      `the reply became a separate TOP-LEVEL comment instead of threading under ` +
        `Com:${parentId} — the failure an agent cannot detect: ${JSON.stringify(data)}`,
    ).toBe(parentId);

    expect(partXml(doc, CT.EXTENDED)).toContain("w15:paraIdParent");
  });

  it("never silently degrades a reply into a thread root", async () => {
    // "If only one thing gets fixed: make B1 loud." Whatever the reason
    // threading cannot resolve, a reply must never quietly root a new thread.
    const doc = await annotatedDoc(
      "Discovery Material shall be produced within thirty days.",
      "within thirty days",
      "Confirm this matches the scheduling order.",
      "Sarah Chen",
    );
    emptyCommentBodies(doc);
    const engine = new RedlineEngine(doc, "Agent");
    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];

    const [applied, skipped] = engine.apply_review_actions([
      { type: "reply", target_id: `Com:${parentId}`, text: "Addressed." },
    ]);

    const data = extract_comments_data(doc.pkg);
    const orphans = Object.entries(data)
      .filter(([cid, c]) => cid !== parentId && (c as any).parent_id === null)
      .map(([cid]) => cid);
    expect(
      orphans,
      `reply on Com:${parentId} silently became top-level comment(s) ` +
        `${orphans.join(", ")} and reported ${applied} applied / ${skipped} skipped`,
    ).toEqual([]);
    expect([applied, skipped]).toEqual([0, 1]);
    const details = engine.skipped_details.join("\n").toLowerCase();
    expect(details).toMatch(/thread|repl/);
  });

  it("rejects the whole batch when a reply cannot be threaded", async () => {
    const doc = await annotatedDoc(
      "The receiving party shall bear the cost of production.",
      "bear the cost",
      "Whose cost is this really?",
      "Sarah Chen",
    );
    emptyCommentBodies(doc);
    const engine = new RedlineEngine(doc, "Agent");
    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];

    const run = () =>
      engine.process_batch([
        { type: "reply", target_id: `Com:${parentId}`, text: "Addressed." } as any,
      ]);
    expect(run).toThrow(BatchValidationError);
    expect(run).toThrow(/thread/i);

    // And the document must be untouched: no stray comment was written.
    expect(Object.keys(extract_comments_data(doc.pkg)).length).toBe(1);
  });

  it("flattens a reply-to-a-reply onto the thread root", async () => {
    const doc = await annotatedDoc(
      "Root anchor text here.",
      "anchor",
      "Root topic",
      "Alice",
    );
    const engine = new RedlineEngine(doc, "Bob");
    const rootId = Object.keys(extract_comments_data(doc.pkg))[0];
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${rootId}`, text: "First reply" },
    ]);
    const firstReply = Object.keys(extract_comments_data(doc.pkg)).find(
      (cid) => cid !== rootId,
    )!;
    engine.author = "Carol";
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${firstReply}`, text: "Second reply" },
    ]);

    const data = extract_comments_data(doc.pkg);
    expect(Object.keys(data).length).toBe(3);
    for (const [cid, c] of Object.entries(data)) {
      if (cid === rootId) expect((c as any).parent_id).toBeNull();
      else expect((c as any).parent_id).toBe(rootId);
    }
  });

  it("backfills a parent missing from the auxiliary parts", async () => {
    // The parent HAS a w14:paraId but no w15:commentEx / w16cid:commentId entry
    // — a shape hand-built and third-party documents produce. Word consults all
    // three parts, so a paraIdParent pointing at an unregistered paragraph
    // drops the reply out of its thread just as surely as a missing attribute.
    const doc = await annotatedDoc(
      "Discovery shall proceed under the model order.",
      "the model order",
      "Which model order?",
      "Sarah Chen",
    );
    for (const ct of [CT.EXTENDED, CT.IDS]) {
      const part = doc.pkg.parts.find((p) => p.contentType === ct)!;
      for (const child of Array.from(part._element.childNodes)) {
        part._element.removeChild(child);
      }
    }

    const engine = new RedlineEngine(doc, "Agent");
    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];
    const [applied, skipped] = engine.apply_review_actions([
      { type: "reply", target_id: `Com:${parentId}`, text: "The WAWD model order." },
    ]);
    expect([applied, skipped]).toEqual([1, 0]);

    const data = extract_comments_data(doc.pkg);
    const replyId = Object.keys(data).find((cid) => cid !== parentId)!;
    expect((data[replyId] as any).parent_id).toBe(parentId);

    const ids = (xml: string, attr: string) =>
      new Set(
        Array.from(xml.matchAll(new RegExp(`${attr}="([0-9A-Fa-f]{8})"`, "g"))).map(
          (m) => m[1],
        ),
      );
    const paraIds = ids(partXml(doc, CT.COMMENTS), "w14:paraId");
    expect(paraIds.size).toBe(2);
    const exIds = ids(partXml(doc, CT.EXTENDED), "w15:paraId");
    const cidIds = ids(partXml(doc, CT.IDS), "w16cid:paraId");
    for (const pid of paraIds) {
      expect(exIds.has(pid), `commentsExtended missing ${pid}`).toBe(true);
      expect(cidIds.has(pid), `commentsIds missing ${pid}`).toBe(true);
    }
  });

  it("registers a repaired parent in every modern comment part", async () => {
    const doc = await annotatedDoc(
      "Attorney's Eyes Only material stays with outside counsel.",
      "Attorney's Eyes Only",
      "Is this tier defined?",
      "Sarah Chen",
    );
    stripCommentParaIds(doc);
    const engine = new RedlineEngine(doc, "Agent");
    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${parentId}`, text: "Defined in section 2." },
    ]);

    const ids = (xml: string, attr: string) =>
      new Set(
        Array.from(xml.matchAll(new RegExp(`${attr}="([0-9A-Fa-f]{8})"`, "g"))).map(
          (m) => m[1],
        ),
      );
    const paraIds = ids(partXml(doc, CT.COMMENTS), "w14:paraId");
    expect(paraIds.size).toBe(2);
    const exIds = ids(partXml(doc, CT.EXTENDED), "w15:paraId");
    const cidIds = ids(partXml(doc, CT.IDS), "w16cid:paraId");
    for (const pid of paraIds) {
      expect(exIds.has(pid), `commentsExtended missing ${pid}`).toBe(true);
      expect(cidIds.has(pid), `commentsIds missing ${pid}`).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// B2 — accepting changes destroys the human's comment
// ---------------------------------------------------------------------------

describe("B2: comment destruction is opt-in and disclosed", () => {
  it("accept_all_revisions keeps comments unless removal is requested", async () => {
    const doc = await annotatedDoc(
      "The parties shall meet and confer. A second clause stands alone.",
      "A second clause",
      "Standalone reviewer note.",
      "Sarah Chen",
    );
    const engine = new RedlineEngine(doc, "Agent");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "meet and confer",
        new_text: "confer in good faith",
      } as any,
    ]);

    const counts = engine.accept_all_revisions();
    expect(counts.removed_comments).toBe(0);
    const raw = await extractTextFromBuffer(await doc.save());
    expect(
      raw,
      "accept_all_revisions destroyed a comment nobody asked it to remove",
    ).toContain("Standalone reviewer note.");
  });

  it("accept_all_revisions(true) still ejects every comment part", async () => {
    const doc = await annotatedDoc(
      "The parties shall meet and confer. A second clause stands alone.",
      "A second clause",
      "Standalone reviewer note.",
      "Sarah Chen",
    );
    const engine = new RedlineEngine(doc, "Agent");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "meet and confer",
        new_text: "confer in good faith",
      } as any,
    ]);

    const counts = engine.accept_all_revisions(true);
    expect(counts.removed_comments).toBe(1);
    const raw = await extractTextFromBuffer(await doc.save());
    expect(raw).not.toContain("Standalone reviewer note.");
    expect(
      doc.pkg.parts.filter((p) => p.partname.toLowerCase().includes("comments"))
        .length,
    ).toBe(0);
  });

  it("reports comments it deletes because their anchor was consumed", async () => {
    const doc = await annotatedDoc(
      "Producing party may designate material Attorney's Eyes Only at its discretion.",
      "Attorney's Eyes Only",
      "Strike this tier.",
      "Sarah Chen",
    );
    const engine = new RedlineEngine(doc, "Agent");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "Attorney's Eyes Only",
        new_text: "",
      } as any,
    ]);

    const before = Object.keys(extract_comments_data(doc.pkg));
    expect(before.length).toBeGreaterThan(0);
    const counts = engine.accept_all_revisions();
    const after = Object.keys(extract_comments_data(doc.pkg));
    expect(
      after.length,
      "fixture precondition: accepting the deletion should consume the anchor",
    ).toBeLessThan(before.length);
    expect(
      counts.removed_comments,
      "a human's comment vanished while the books said nothing happened",
    ).toBe(before.length - after.length);
  });

  it("names the author of a comment an accept action destroys", async () => {
    const doc = await annotatedDoc(
      "Producing party may designate material Attorney's Eyes Only at its discretion.",
      "Attorney's Eyes Only",
      "Strike this tier.",
      "Sarah Chen",
    );
    const engine = new RedlineEngine(doc, "Agent");
    engine.apply_edits([
      { type: "modify", target_text: "Attorney's Eyes Only", new_text: "" } as any,
    ]);
    const buf = await doc.save();

    const reloaded = await DocumentObject.load(buf);
    const reviewer = new RedlineEngine(reloaded, "Agent");
    const raw = await extractTextFromBuffer(buf);
    const chg = /\[Chg:(\d+) delete\]/.exec(raw);
    expect(chg, `no tracked deletion to accept:\n${raw}`).not.toBeNull();

    reviewer.apply_review_actions([
      { type: "accept", target_id: `Chg:${chg![1]}` },
    ]);
    const details = reviewer.skipped_details.join("\n");
    expect(details).toContain("Com:");
    expect(
      details,
      "the disclosure must name the comment's AUTHOR so the caller can see it " +
        "destroyed someone else's review content:\n" + details,
    ).toContain("Sarah Chen");
  });
});

// ---------------------------------------------------------------------------
// B4 — edits rewrite curly quotes in untargeted text
// ---------------------------------------------------------------------------

describe("B4: the writer is as typography-forgiving as the matcher", () => {
  async function applied(target: string, replacement: string) {
    const doc = await createTestDocument();
    addParagraph(doc, CURLY_BODY);
    const engine = new RedlineEngine(doc, "Agent");
    engine.apply_edits([
      { type: "modify", target_text: target, new_text: replacement } as any,
    ]);
    const buf = await doc.save();
    return {
      raw: await extractTextFromBuffer(buf),
      clean: await extractTextFromBuffer(buf, true),
    };
  }

  it("does not redline untargeted curly quotes", async () => {
    const { raw } = await applied(
      'designated as "Confidential" under the parties\' Master Agreement ' +
        "shall be produced within thirty days",
      'designated as "Confidential" under the parties\' Master Agreement ' +
        "shall be produced within sixty days",
    );
    const deletions = Array.from(raw.matchAll(/\{--([\s\S]*?)--\}/g)).map((m) => m[1]);
    const insertions = Array.from(raw.matchAll(/\{\+\+([\s\S]*?)\+\+\}/g)).map((m) => m[1]);
    expect(
      deletions,
      `only the word the caller changed may be deleted; the rest are pure ` +
        `punctuation rewrites:\n${raw}`,
    ).toEqual(["thirty"]);
    expect(insertions).toEqual(["sixty"]);
  });

  it("keeps the document's own typography in the saved text", async () => {
    const { clean } = await applied(
      "parties' Master Agreement shall be produced within thirty days",
      "parties' Master Agreement shall be produced within sixty days",
    );
    expect(clean).toContain("\u2019");
    expect(clean).toContain("\u201c");
    expect(clean).toContain("\u201d");
    expect(clean.replace(/\u2019/g, "")).not.toContain("'");
  });

  it("treats a punctuation-only round-trip as a no-op", async () => {
    const { raw } = await applied(
      'designated as "Confidential" under the parties\' Master Agreement',
      'designated as "Confidential" under the parties\' Master Agreement',
    );
    expect(raw).not.toContain("{--");
    expect(raw).not.toContain("{++");
  });

  it("still applies a typography change the caller explicitly asks for", async () => {
    const { raw } = await applied("\u201cConfidential\u201d", '"Confidential"');
    expect(raw).toContain("{--");
    expect(raw).toContain("{++");
  });

  it("honours a real edit inside a smart-quoted phrase", async () => {
    const { raw, clean } = await applied(
      'designated as "Confidential" under',
      'designated as "Highly Confidential" under',
    );
    expect(clean).toContain("\u201cHighly Confidential\u201d");
    expect(clean).not.toContain('"Highly Confidential"');
    // Adding a word before "Confidential" is a pure insertion: no quote
    // character is deleted, and the edit does not fragment.
    expect((raw.match(/\{--/g) || []).length).toBe(0);
    expect((raw.match(/\{\+\+/g) || []).length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Invariants behind the fixes (twins of Python's P6/P7 property tests)
// ---------------------------------------------------------------------------

describe("typographic restoration invariants", () => {
  const SAMPLES: [string, string][] = [
    ["\u201cA\u201d", '"A"'],
    ["parties\u2019 Master", "parties' Master"],
    ["parties\u2019 Master", "parties' Master Services"],
    ["\u2018x\u2019 and \u201cy\u201d", "'x' and \"z\""],
    ["plain text", "plain text"],
    ["\u201cA\u201d \u201cB\u201d", '"A" "C"'],
    ["a\u2019b\u2019c", "a'b'"],
    ["", '"A"'],
    ["\u201cA\u201d", ""],
  ];

  it("only ever swaps typographic variants", () => {
    // Whatever the repair returns must be indistinguishable from the caller's
    // own new_text once quotes are folded — otherwise "preserve the document's
    // characters" would have quietly changed what the caller asked to write.
    for (const [doc_text, new_text] of SAMPLES) {
      const restored = restore_document_typography(doc_text, new_text);
      expect(
        normalize_smart_quotes(restored),
        `restoration changed the text beyond quote typography: ${doc_text} + ${new_text} -> ${restored}`,
      ).toBe(normalize_smart_quotes(new_text));
      expect(restored.length).toBe(new_text.length);
      // Idempotent.
      expect(restore_document_typography(doc_text, restored)).toBe(restored);
    }
  });

  it("treats a straightened copy of the document as a no-op", () => {
    for (const [doc_text] of SAMPLES) {
      expect(
        restore_document_typography(doc_text, normalize_smart_quotes(doc_text)),
      ).toBe(doc_text);
    }
  });

  it("forgives exactly what the matcher forgives", () => {
    // Structural invariant, not a data check: the WRITER's normalization table
    // must be the SAME table the MATCHER uses. B4 was precisely this asymmetry.
    // Extending one side alone silently reintroduces the defect for the newly
    // forgiven characters.
    const probe =
      Object.keys(SMART_QUOTE_MAP).join("") +
      Object.values(SMART_QUOTE_MAP).join("") +
      "abc \u2013\u2014\u2026";
    const mapperView = (DocumentMapper.prototype as any)._replace_smart_quotes.call(
      null,
      probe,
    );
    expect(
      normalize_smart_quotes(probe),
      "DocumentMapper._replace_smart_quotes and utils/text.normalize_smart_quotes " +
        "disagree; the writer would not restore a character the matcher forgave",
    ).toBe(mapperView);
  });

  it("uses the same normalization table as the Python engine", () => {
    // Dual-engine parity (AI_CONTEXT §4 "Make Both Perfect"): the forgiven set
    // is part of the cross-engine contract, so pin it literally.
    expect(SMART_QUOTE_MAP).toEqual({
      "\u201c": '"',
      "\u201d": '"',
      "\u2018": "'",
      "\u2019": "'",
    });
  });
});

/**
 * Package-level invariants that hold for ANY document Adeu writes comments into,
 * independent of the specific defects above. Each of B1/B3 was a violation of
 * one of them, and each is cheap to check on a saved package — which is what
 * makes them worth stating separately from the repros.
 */
describe("modern comment part invariants", () => {
  async function threadedDoc(): Promise<DocumentObject> {
    const doc = await createTestDocument();
    addParagraph(doc, "Clause one text. Clause two text.");
    const engine = new RedlineEngine(doc, "Sarah Chen");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "Clause one",
        new_text: "Clause one",
        comment: "Root topic",
      } as any,
    ]);
    const root = Object.keys(extract_comments_data(doc.pkg))[0];
    engine.author = "Agent";
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${root}`, text: "Reply one" },
    ]);
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${root}`, text: "Reply two" },
    ]);
    engine.apply_edits([
      {
        type: "modify",
        target_text: "Clause two",
        new_text: "Clause two",
        comment: "Second root",
      } as any,
    ]);
    return doc;
  }

  function threadMap(doc: DocumentObject) {
    const paraIds = new Set(
      Array.from(
        partXml(doc, CT.COMMENTS).matchAll(/w14:paraId="([0-9A-Fa-f]{8})"/g),
      ).map((m) => m[1]),
    );
    const exEntries = Array.from(
      partXml(doc, CT.EXTENDED).matchAll(
        /<w15:commentEx[^>]*w15:paraId="([0-9A-Fa-f]{8})"(?:[^>]*w15:paraIdParent="([0-9A-Fa-f]{8})")?[^>]*\/?>/g,
      ),
    ).map((m) => [m[1], m[2]] as [string, string | undefined]);
    const idParaIds = new Set(
      Array.from(
        partXml(doc, CT.IDS).matchAll(/w16cid:paraId="([0-9A-Fa-f]{8})"/g),
      ).map((m) => m[1]),
    );
    return { paraIds, exEntries, idParaIds };
  }

  it("registers every comment exactly once in every part", async () => {
    const { paraIds, exEntries, idParaIds } = threadMap(await threadedDoc());
    const exParaIds = exEntries.map(([pid]) => pid);

    expect(paraIds.size).toBe(4);
    expect(exParaIds.sort()).toEqual(Array.from(paraIds).sort());
    expect(new Set(exParaIds).size).toBe(exParaIds.length);
    expect(Array.from(idParaIds).sort()).toEqual(Array.from(paraIds).sort());
  });

  it("never points a thread at a paragraph that does not exist", async () => {
    const { paraIds, exEntries } = threadMap(await threadedDoc());
    const parents = new Set(
      exEntries.map(([, parent]) => parent).filter((p): p is string => !!p),
    );
    expect(parents.size).toBeGreaterThan(0);
    const dangling = Array.from(parents).filter((p) => !paraIds.has(p));
    expect(
      dangling,
      `w15:paraIdParent references ${dangling.join(", ")}, which no comment paragraph ` +
        `carries — Word drops such a reply out of its thread`,
    ).toEqual([]);
  });

  it("never makes a thread its own parent", async () => {
    const { exEntries } = threadMap(await threadedDoc());
    for (const [pid, parent] of exEntries) expect(parent).not.toBe(pid);
  });

  it("mints unique, Word-readable durableIds", async () => {
    const durableIds = Array.from(
      partXml(await threadedDoc(), CT.IDS).matchAll(
        /w16cid:durableId="([0-9A-Fa-f]+)"/g,
      ),
    ).map((m) => m[1]);
    expect(durableIds.length).toBe(4);
    expect(new Set(durableIds).size).toBe(durableIds.length);
    for (const d of durableIds) expect(parseInt(d, 16)).toBeLessThanOrEqual(0x7fffffff);
  });

  it("keeps accept-all's books matching the document in both modes", async () => {
    // Under-reporting (0 while a human's comment is gone) and over-reporting
    // (claiming removals that never happened) are the same class of defect:
    // books that do not match the document.
    for (const remove_comments of [false, true]) {
      const doc = await annotatedDoc(
        "Producing party may designate material Attorney's Eyes Only. " +
          "A second clause stands alone.",
        "A second clause",
        "Standalone reviewer note.",
        "Sarah Chen",
      );
      const engine = new RedlineEngine(doc, "Agent");
      engine.apply_edits([
        { type: "modify", target_text: "Attorney's Eyes Only", new_text: "" } as any,
      ]);
      const before = Object.keys(extract_comments_data(doc.pkg));

      const counts = engine.accept_all_revisions(remove_comments);

      const after = new Set(Object.keys(extract_comments_data(doc.pkg)));
      const actually_removed = before.filter((cid) => !after.has(cid)).length;
      expect(
        counts.removed_comments,
        `remove_comments=${remove_comments}: reported ${counts.removed_comments} removals ` +
          `but ${actually_removed} comment bodies actually disappeared`,
      ).toBe(actually_removed);
      expect(engine.removed_comment_notes.length).toBe(counts.removed_comments);
      for (const note of engine.removed_comment_notes) {
        expect(note).toMatch(/^Com:.+\(by .+\)$/);
      }
    }
  });
});
