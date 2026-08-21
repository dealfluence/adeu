// FILE: src/repro_issue_114_cross_part_revisions.test.ts
//
// Regression tests for GitHub issue #114: RedlineEngine WRITES tracked
// changes across the whole package (the mapper projects headers/footers/
// notes, apply edits them, accept_all/reject_all traverse every
// wordprocessingml part), but every path that READS revision state is
// rooted at `this.doc.element` — the main part's w:body only:
//
//   _scan_existing_ids            engine.ts:1228   (id-mint seed)
//   validation existence check    engine.ts:3543   (accept/reject pre-check)
//   _resolution_group_ids         engine.ts:4768
//   _buildRevisionIndex root      engine.ts:4861   (targeted accept/reject
//                                                   and the :5340 guard)
//
// Revision ids are numbered PER PART (Word restarts numbering in each
// header/footer), so cross-part id collisions are ordinary, and the engine
// itself recreates them because its mint counter is seeded body-only.
//
// The file has two halves:
//
//   1. "invariants" — behavior that is CORRECT today and must survive any
//      fix. Plain regression tests; never flip these.
//
//   2. "pinned defects" — each test asserts the BROKEN behavior exactly as
//      it stands, so the suite keeps proving the issue until it is fixed.
//      Every buggy expectation is marked `BUG(#114)` with the desired
//      outcome beside it. When the fix lands these tests FAIL at those
//      lines — flip each marked expectation to its DESIRED counterpart and
//      move the test up into the invariants block.

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { serializeXml } from "./docx/dom.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const CT_HEADER =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml";

/** Adds word/header1.xml holding "HEADER MARKER" plus optional extra OOXML
 *  inside the same w:p (raw node injection, as the 2026-07-18 builders do). */
function addHeader(doc: DocumentObject, extraInnerXml = "") {
  doc.pkg.addPart(
    "/word/header1.xml",
    CT_HEADER,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:hdr xmlns:w="${W_NS}"><w:p><w:r><w:t xml:space="preserve">HEADER MARKER</w:t></w:r>${extraInnerXml}</w:p></w:hdr>`,
  );
}

const insXml = (id: string, author: string, text: string) =>
  `<w:ins w:id="${id}" w:author="${author}" w:date="2026-01-01T00:00:00Z">` +
  `<w:r><w:t>${text}</w:t></w:r></w:ins>`;

/** Appends a tracked insertion into the first w:p under `root`. */
function injectIns(root: Element, id: string, author: string, text: string) {
  const od = root.ownerDocument!;
  const ins = od.createElement("w:ins");
  ins.setAttribute("w:id", id);
  ins.setAttribute("w:author", author);
  ins.setAttribute("w:date", "2026-01-01T00:00:00Z");
  const r = od.createElement("w:r");
  const t = od.createElement("w:t");
  t.textContent = text;
  r.appendChild(t);
  ins.appendChild(r);
  root.getElementsByTagName("w:p")[0].appendChild(ins);
}

const headerXml = (doc: DocumentObject) =>
  serializeXml(doc.pkg.getPartByPath("word/header1.xml")!._element);
const bodyXml = (doc: DocumentObject) =>
  serializeXml(doc.pkg.mainDocumentPart._element);

/** w:id values of every w:ins/w:del in `xml`, in document order. */
const revIdsIn = (xml: string) =>
  [...xml.matchAll(/<w:(?:ins|del)\b[^>]*w:id="(\d+)"/g)].map((m) => m[1]);

// ---------------------------------------------------------------------------
// 1. Invariants — correct today, must survive the #114 fix. Never flip.
// ---------------------------------------------------------------------------

describe("issue #114 invariants (must hold before AND after the fix)", () => {
  it("accept_all_revisions resolves revisions in headers as well as the body", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));
    injectIns(doc.element, "5", "Alice", "BodyInserted");

    new RedlineEngine(doc, "Reviewer").accept_all_revisions();

    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    // Accepted insertions keep their text.
    expect(headerXml(doc)).toContain("HeaderInserted");
    expect(bodyXml(doc)).toContain("BodyInserted");
  });

  it("reject_all_revisions reverts revisions in headers as well as the body", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));
    injectIns(doc.element, "5", "Alice", "BodyInserted");

    new RedlineEngine(doc, "Reviewer").reject_all_revisions();

    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    // Rejected insertions lose their text.
    expect(headerXml(doc)).not.toContain("HeaderInserted");
    expect(bodyXml(doc)).not.toContain("BodyInserted");
  });

  it("a targeted accept on an unambiguous body id applies and reports ok", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc);
    injectIns(doc.element, "5", "Alice", "BodyInserted");

    const res: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "accept", target_id: "5" },
    ]);

    expect(res.status).toBe("ok");
    expect(res.actions_applied).toBe(1);
    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(bodyXml(doc)).toContain("BodyInserted");
  });

  it("the same-part different-author guard (engine.ts:5340) still refuses a body-internal id collision", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    injectIns(doc.element, "7", "Alice", "first");
    injectIns(doc.element, "7", "Bob", "second");

    const engine = new RedlineEngine(doc, "Reviewer");
    let caught: any = null;
    try {
      engine.process_batch([{ type: "accept", target_id: "7" }]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(String(caught.message)).toContain("different authors");
    // Refused means untouched.
    expect(revIdsIn(bodyXml(doc))).toEqual(["7", "7"]);
  });

  it("the projection advertises header revisions with [Chg:N] labels", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));

    const projection = await extractTextFromBuffer(
      Buffer.from(await doc.save()),
    );
    // The header change is presented to callers exactly like a body change —
    // which is what makes it a legitimate accept/reject target.
    expect(projection).toContain("Chg:900");
    expect(projection).toContain("HeaderInserted");
  });
});

// ---------------------------------------------------------------------------
// 2. Pinned defects — these assert the BROKEN behavior of #114 so the suite
//    repeats the issue's verification. On fixing #114, flip every line
//    marked BUG(#114) to its DESIRED expectation.
// ---------------------------------------------------------------------------

describe("issue #114 pinned defects (flip marked expectations when fixed)", () => {
  it("F1: a body/header id collision resolves the body and reports plain success", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    // DIFFERENT authors share w:id=0 across parts — the exact shape the
    // :5340 guard refuses when both nodes are in the body.
    addHeader(doc, insXml("0", "Bob", "HeaderInserted"));
    injectIns(doc.element, "0", "Alice", "BodyInserted");
    expect(revIdsIn(bodyXml(doc))).toEqual(["0"]);
    expect(revIdsIn(headerXml(doc))).toEqual(["0"]);

    const engine = new RedlineEngine(doc, "Reviewer");
    const res: any = engine.process_batch([
      { type: "accept", target_id: "0" },
    ]);

    // BUG(#114): the batch reports unqualified success and resolves only the
    // body's revision; nothing in the result names the header's, or that an
    // ambiguity existed. DESIRED: refuse the bare id naming both parts (as
    // the same-author guard does), or resolve exactly the revision an
    // explicit part selector names.
    expect(res.status).toBe("ok"); // BUG(#114) — DESIRED: BatchValidationError naming word/header1.xml
    expect(res.actions_applied).toBe(1); // BUG(#114)
    expect(res.skipped_details).toEqual([]); // BUG(#114) — silence is the defect
    expect(revIdsIn(bodyXml(doc))).toEqual([]); // body resolved
    expect(revIdsIn(headerXml(doc))).toEqual(["0"]); // BUG(#114) — header revision still pending
  });

  it("F2: an id that exists only in a header is advertised yet untargetable, and the error denies it exists", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));

    // The projection labels it Chg:900 (see the invariant above), so callers
    // are invited to act on it…
    const engine = new RedlineEngine(doc, "Reviewer");
    let caught: any = null;
    try {
      engine.process_batch([{ type: "accept", target_id: "900" }]);
    } catch (e) {
      caught = e;
    }

    // BUG(#114): …but the body-rooted existence check throws, failing the
    // whole (transactional) batch. DESIRED: the accept resolves the header
    // revision.
    expect(caught).toBeInstanceOf(BatchValidationError); // BUG(#114) — DESIRED: no throw
    // BUG(#114): the guidance is actively wrong — the document visibly HAS a
    // tracked change; only the body-rooted id list is empty. DESIRED: an
    // error (if any) that names where the id actually lives.
    expect(String(caught.message)).toContain(
      "This document has no tracked changes.",
    ); // BUG(#114)
    expect(revIdsIn(headerXml(doc))).toEqual(["900"]); // untouched either way
  });

  it("F3: a modify anchored on header text mints revisions that targeted accept can never resolve", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc);

    const res: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    expect(res.edits_applied).toBe(1);

    // The engine authored a del+ins pair inside word/header1.xml…
    const minted = revIdsIn(headerXml(doc));
    expect(minted.length).toBeGreaterThan(0);

    // …which a fresh engine (the normal act-later flow) cannot address.
    const engine2 = new RedlineEngine(doc, "Reviewer");
    let caught: any = null;
    try {
      engine2.process_batch([{ type: "accept", target_id: minted[0] }]);
    } catch (e) {
      caught = e;
    }
    // BUG(#114): the engine's own output is unactionable — only accept_all/
    // reject_all can ever clear it. DESIRED: no throw, revision resolved.
    expect(caught).toBeInstanceOf(BatchValidationError); // BUG(#114)
    expect(revIdsIn(headerXml(doc))).toEqual(minted); // still pending
  });

  it("F4: the body-only id scan mints a duplicate w:id INSIDE the header part, different authors", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    // Header already holds id 2 (Bob); body max is 1, so the engine's next
    // mint is 2.
    addHeader(doc, insXml("2", "Bob", "HeaderInserted"));
    injectIns(doc.element, "1", "Alice", "BodyInserted");

    const engine = new RedlineEngine(doc, "Reviewer");
    // BUG(#114): _scan_existing_ids saw only the body ("a fresh engine must
    // never mint a duplicate" — engine.ts:1226). DESIRED: 2.
    expect(engine.current_id).toBe(1); // BUG(#114)

    const res: any = engine.process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    expect(res.edits_applied).toBe(1);

    const authorsOfId2 = [
      ...headerXml(doc).matchAll(
        /<w:(?:ins|del)\b[^>]*w:id="2"[^>]*w:author="([^"]*)"/g,
      ),
    ].map((m) => m[1]);
    // BUG(#114): two revision elements share w:id=2 within ONE part, from
    // different authors — the exact state the :5340 guard exists to refuse,
    // manufactured by the engine itself. DESIRED: one element, one author
    // (fresh ids minted above the package-wide max).
    expect(authorsOfId2.length).toBe(2); // BUG(#114) — DESIRED: 1
    expect(new Set(authorsOfId2).size).toBe(2); // BUG(#114)
  });

  it("F5: two ordinary engine sessions create a full cross-part collision unaided, then mis-resolve it", async () => {
    // No foreign/injected revisions anywhere — pure product usage.
    let doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc);

    // Session 1: redline the header. Mints del=1/ins=2 in word/header1.xml.
    new RedlineEngine(doc, "Session One").process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    doc = await DocumentObject.load(Buffer.from(await doc.save()));
    expect(revIdsIn(headerXml(doc))).toEqual(["1", "2"]);

    // Session 2: redline the body. The id scan is body-only, so the counter
    // restarts at 0 and mints del=1/ins=2 AGAIN — now in the body.
    new RedlineEngine(doc, "Session Two").process_batch([
      { type: "modify", target_text: "Body paragraph one.", new_text: "Body paragraph two." },
    ]);
    doc = await DocumentObject.load(Buffer.from(await doc.save()));
    // BUG(#114): every id is now ambiguous across parts. DESIRED: session 2
    // mints 3/4, no collision.
    expect(revIdsIn(bodyXml(doc)).sort()).toEqual(["1", "2"]); // BUG(#114)
    expect(revIdsIn(headerXml(doc)).sort()).toEqual(["1", "2"]); // BUG(#114)

    // Session 3: accept "Chg:1". Two unrelated revisions carry id 1; the
    // body one wins silently (its replacement pair resolves ids 1 AND 2).
    const res: any = new RedlineEngine(doc, "Session Three").process_batch([
      { type: "accept", target_id: "1" },
    ]);
    // BUG(#114): plain success; the header's revisions are untouched and
    // unreported. DESIRED: refusal naming both parts, or explicit part
    // targeting.
    expect(res.status).toBe("ok"); // BUG(#114)
    expect(res.actions_applied).toBe(1); // BUG(#114)
    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(revIdsIn(headerXml(doc)).sort()).toEqual(["1", "2"]); // BUG(#114) — still pending
  });
});
