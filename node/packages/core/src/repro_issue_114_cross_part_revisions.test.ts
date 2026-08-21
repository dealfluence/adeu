// FILE: src/repro_issue_114_cross_part_revisions.test.ts
//
// Regression tests for GitHub issue #114: RedlineEngine WROTE tracked
// changes across the whole package (the mapper projects headers/footers/
// notes, apply edits them, accept_all/reject_all traverse every
// wordprocessingml part) while every path that READ revision state was
// rooted at the main part's w:body only. Four defects followed:
//
//   F1  a body/header id collision silently resolved the body's revision
//       and reported plain success
//   F2  an id existing only in a header was advertised by the projection
//       yet untargetable — the error even claimed no tracked changes exist
//   F3  a modify anchored on header text minted revisions that targeted
//       accept could never resolve
//   F4  the body-only id scan minted duplicate w:ids inside a header part
//   F5  two ordinary engine sessions created a full cross-part collision
//       unaided, then a third mis-resolved it
//
// Fixed by reading revision state across every story part (body, headers,
// footers, footnotes, endnotes): the id scan spans every wordprocessingml
// part, targeted accept/reject resolve a bare id wherever it uniquely
// lives, a bare id matching several parts is REFUSED (ids are numbered per
// part, so it cannot name one change), and the optional `part` field on
// accept/reject picks the part explicitly.

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

/** Text content of `xml` with tags stripped — replacements may land split
 *  across several runs, so raw-XML substring checks are unreliable. */
const textOf = (xml: string) => xml.replace(/<[^>]+>/g, "");

describe("issue #114 — bulk paths and projection (unchanged by the fix)", () => {
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

  it("the same-part different-author guard still refuses a body-internal id collision", async () => {
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

describe("issue #114 — targeted resolution across parts (fixed behavior)", () => {
  it("F1: a bare id matching body AND header is refused, naming both parts and the part selector", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    // DIFFERENT authors share w:id=0 across parts — Word numbers revision
    // ids per part, so this is an ordinary document, not a corrupt one.
    addHeader(doc, insXml("0", "Bob", "HeaderInserted"));
    injectIns(doc.element, "0", "Alice", "BodyInserted");

    const engine = new RedlineEngine(doc, "Reviewer");
    let caught: any = null;
    try {
      engine.process_batch([{ type: "accept", target_id: "0" }]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(String(caught.message)).toContain("ambiguous");
    expect(String(caught.message)).toContain("word/document.xml");
    expect(String(caught.message)).toContain("word/header1.xml");
    expect(String(caught.message)).toContain('"part"');
    // Refused means NOTHING was resolved — no silent body-wins.
    expect(revIdsIn(bodyXml(doc))).toEqual(["0"]);
    expect(revIdsIn(headerXml(doc))).toEqual(["0"]);
  });

  it("F1: the part selector resolves exactly the named side of a collision", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("0", "Bob", "HeaderInserted"));
    injectIns(doc.element, "0", "Alice", "BodyInserted");

    const engine = new RedlineEngine(doc, "Reviewer");
    const res1: any = engine.process_batch([
      { type: "accept", target_id: "0", part: "word/header1.xml" },
    ]);
    expect(res1.status).toBe("ok");
    expect(res1.actions_applied).toBe(1);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    expect(headerXml(doc)).toContain("HeaderInserted"); // accepted, text kept
    expect(revIdsIn(bodyXml(doc))).toEqual(["0"]); // untouched

    // With the header's resolved, the bare id is unique again.
    const res2: any = engine.process_batch([
      { type: "accept", target_id: "0" },
    ]);
    expect(res2.status).toBe("ok");
    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(bodyXml(doc)).toContain("BodyInserted");
  });

  it("F2: an id that exists only in a header resolves through a bare targeted accept", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));

    const res: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "accept", target_id: "900" },
    ]);
    expect(res.status).toBe("ok");
    expect(res.actions_applied).toBe(1);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    expect(headerXml(doc)).toContain("HeaderInserted");
  });

  it("F2: the not-found hint lists header ids instead of denying tracked changes exist", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));

    let caught: any = null;
    try {
      new RedlineEngine(doc, "Reviewer").process_batch([
        { type: "accept", target_id: "555" },
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(String(caught.message)).toContain("Chg:900");
    expect(String(caught.message)).not.toContain(
      "This document has no tracked changes.",
    );
  });

  it("F3: revisions the engine authors in a header are resolvable by targeted accept", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc);

    const res: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    expect(res.edits_applied).toBe(1);
    const minted = revIdsIn(headerXml(doc));
    expect(minted.length).toBeGreaterThan(0);

    // A fresh engine (the normal act-later flow) resolves it; the del+ins
    // pair resolves as one unit.
    const res2: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "accept", target_id: minted[0] },
    ]);
    expect(res2.status).toBe("ok");
    expect(res2.actions_applied).toBe(1);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    expect(headerXml(doc)).toContain("Amended Header");
    expect(headerXml(doc)).not.toContain("HEADER MARKER");
  });

  it("F4: the id scan spans parts, so header edits never mint a duplicate id", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    // Header already holds id 2 (Bob); body max is 1. The scan must seed
    // from the package-wide max, not the body's.
    addHeader(doc, insXml("2", "Bob", "HeaderInserted"));
    injectIns(doc.element, "1", "Alice", "BodyInserted");

    const engine = new RedlineEngine(doc, "Reviewer");
    expect(engine.current_id).toBe(2);

    const res: any = engine.process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    expect(res.edits_applied).toBe(1);

    const authorsOfId2 = [
      ...headerXml(doc).matchAll(
        /<w:(?:ins|del)\b[^>]*w:id="2"[^>]*w:author="([^"]*)"/g,
      ),
    ].map((m) => m[1]);
    expect(authorsOfId2).toEqual(["Bob"]); // only the pre-existing revision
    // The minted pair took fresh ids above the package-wide max.
    const ids = revIdsIn(headerXml(doc)).map(Number);
    expect(Math.max(...ids)).toBeGreaterThan(2);
    expect(new Set(revIdsIn(headerXml(doc))).size).toBe(
      revIdsIn(headerXml(doc)).length,
    );
  });

  it("F5: consecutive sessions mint distinct ids across parts and each side resolves independently", async () => {
    // No foreign/injected revisions anywhere — pure product usage.
    let doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc);

    // Session 1: redline the header (mints a del+ins pair there).
    new RedlineEngine(doc, "Session One").process_batch([
      { type: "modify", target_text: "HEADER MARKER", new_text: "Amended Header" },
    ]);
    doc = await DocumentObject.load(Buffer.from(await doc.save()));
    const header_ids = revIdsIn(headerXml(doc));
    expect(header_ids.length).toBeGreaterThan(0);

    // Session 2: redline the body. The scan sees the header's ids, so the
    // new pair takes fresh numbers — no cross-part collision forms.
    new RedlineEngine(doc, "Session Two").process_batch([
      { type: "modify", target_text: "Body paragraph one.", new_text: "Body paragraph two." },
    ]);
    doc = await DocumentObject.load(Buffer.from(await doc.save()));
    const body_ids = revIdsIn(bodyXml(doc));
    expect(body_ids.length).toBeGreaterThan(0);
    expect(body_ids.filter((id) => header_ids.includes(id))).toEqual([]);

    // Session 3: both bare ids are unique, so each resolves its own pair.
    const res: any = new RedlineEngine(doc, "Session Three").process_batch([
      { type: "accept", target_id: header_ids[0] },
      { type: "accept", target_id: body_ids[0] },
    ]);
    expect(res.status).toBe("ok");
    expect(res.actions_applied).toBe(2);
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    expect(revIdsIn(bodyXml(doc))).toEqual([]);
    expect(textOf(headerXml(doc))).toContain("Amended Header");
    expect(textOf(bodyXml(doc))).toContain("Body paragraph two.");
  });

  it("part selectors: leading slash normalizes, and same-id actions in different parts don't collide", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("1", "Bob", "HeaderInserted"));
    injectIns(doc.element, "1", "Alice", "BodyInserted");

    // accept header's Chg:1 but reject the body's Chg:1 — with per-part
    // pairing keys this is NOT a contradiction.
    const res: any = new RedlineEngine(doc, "Reviewer").process_batch([
      { type: "accept", target_id: "1", part: "/word/header1.xml" },
      { type: "reject", target_id: "1", part: "word/document.xml" },
    ]);
    expect(res.status).toBe("ok");
    expect(res.actions_applied).toBe(2);
    expect(headerXml(doc)).toContain("HeaderInserted"); // accepted
    expect(bodyXml(doc)).not.toContain("BodyInserted"); // rejected
    expect(revIdsIn(headerXml(doc))).toEqual([]);
    expect(revIdsIn(bodyXml(doc))).toEqual([]);
  });

  it("part selectors: an unknown part and a wrong part fail with actionable errors", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body paragraph one.");
    addHeader(doc, insXml("900", "Bob", "HeaderInserted"));

    let caught: any = null;
    try {
      new RedlineEngine(doc, "Reviewer").process_batch([
        { type: "accept", target_id: "900", part: "word/nope.xml" },
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(String(caught.message)).toContain("not a package part");
    expect(String(caught.message)).toContain("word/header1.xml");

    caught = null;
    try {
      new RedlineEngine(doc, "Reviewer").process_batch([
        { type: "accept", target_id: "900", part: "word/document.xml" },
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(String(caught.message)).toContain("word/document.xml");
    // The error says where the id actually lives.
    expect(String(caught.message)).toContain("word/header1.xml");
    expect(revIdsIn(headerXml(doc))).toEqual(["900"]); // untouched either way
  });
});
