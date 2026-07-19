// FILE: node/packages/core/src/repro_qa_report_v9.test.ts
/**
 * Node-engine repro tests for the 2026-07-19 black-box QA and UX evaluation
 * of 1.27.0 (adeu 1.27.0+7a0a821). Mirrors
 * python/tests/test_repro_qa_report_v9.py for the findings that live in the
 * shared core engine:
 *
 *   ADEU-QA-001  sanitize reports CLEAN while a w:docVar secret survives in
 *                word/settings.xml
 *   ADEU-QA-002  structured diff output is not replayable for paragraph
 *                deletions/reorderings; three mechanisms: (A) word-level
 *                hunks crossing paragraph boundaries, (B) full-paragraph
 *                deletion bleeding the deleted paragraph's style onto the
 *                following one, (C) virtual meta-bubble text satisfying or
 *                ambiguating matches
 *   ADEU-QA-004  a replacement's del+ins pair reads as two independent IDs;
 *                the Node engine additionally did NOT group-resolve pairs at
 *                all (accepting the deletion left the insertion pending —
 *                an engine-parity divergence), and follow-up actions on the
 *                paired id were miscounted as applied
 *
 * Every test fails against the commit preceding its fix.
 */

import { describe, it, expect } from "vitest";
import { strFromU8, unzipSync } from "fflate";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { DocumentMapper } from "./mapper.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import {
  generate_structured_edits,
  generate_edits_via_paragraph_alignment,
} from "./diff.js";
import { finalize_document } from "./sanitize/core.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

const SECRET = "SECRET-MATTER-4711-CONFIDENTIAL";

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, true, false) as string;
}

function structured(doc: DocumentObject): { text: string; structure: any } {
  return _extractTextFromDoc(doc, true, false, false, true) as {
    text: string;
    structure: any;
  };
}

function addStyledParagraph(
  doc: DocumentObject,
  text: string,
  styleId: string,
): Element {
  const p = addParagraph(doc, text);
  const xmlDoc = p.ownerDocument!;
  const pPr = xmlDoc.createElement("w:pPr");
  const pStyle = xmlDoc.createElement("w:pStyle");
  pStyle.setAttribute("w:val", styleId);
  pPr.appendChild(pStyle);
  p.insertBefore(pPr, p.firstChild);
  return p;
}

/** A document carrying exactly one tracked modification (Chg:1 + Chg:2). */
async function buildTrackedChangeDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Payment is due in 30 days.");
  addParagraph(doc, "Second paragraph here.");
  const engine = new RedlineEngine(doc, "Editor");
  engine.apply_edits([
    { type: "modify", target_text: "30 days", new_text: "60 days" },
  ]);
  return DocumentObject.load(await doc.save());
}

// ---------------------------------------------------------------------------
// ADEU-QA-001: sanitizer must not report CLEAN while w:docVar secrets remain
// ---------------------------------------------------------------------------

describe("ADEU-QA-001: sanitize strips document variables", () => {
  async function buildDocVarDoc(): Promise<DocumentObject> {
    const doc = await createTestDocument();
    addParagraph(doc, "An ordinary contract paragraph.");
    const settings = doc.pkg.getPartByPath("word/settings.xml");
    expect(settings).toBeTruthy();
    const xmlDoc = settings!._element.ownerDocument!;
    const docVars = xmlDoc.createElement("w:docVars");
    const docVar = xmlDoc.createElement("w:docVar");
    docVar.setAttribute("w:name", "MatterRef");
    docVar.setAttribute("w:val", SECRET);
    docVars.appendChild(docVar);
    settings!._element.appendChild(docVars);
    return doc;
  }

  it("full sanitize removes w:docVar values from the saved package", async () => {
    const doc = await buildDocVarDoc();
    const pre = await doc.save();
    expect(strFromU8(unzipSync(new Uint8Array(pre))["word/settings.xml"])).toContain(
      SECRET,
    );

    const doc2 = await DocumentObject.load(pre);
    const result = await finalize_document(doc2, {
      filename: "docvar.docx",
      sanitize_mode: "full",
    });
    expect(result.outBuffer).toBeTruthy();
    const members = unzipSync(new Uint8Array(result.outBuffer!));
    for (const [name, bytes] of Object.entries(members)) {
      expect(
        strFromU8(bytes).includes(SECRET),
        `secret still present in ${name}`,
      ).toBe(false);
    }
  });

  it("report names the variable but never echoes its value", async () => {
    const doc = await buildDocVarDoc();
    const result = await finalize_document(doc, {
      filename: "docvar.docx",
      sanitize_mode: "full",
    });
    expect(result.reportText.toLowerCase()).toContain("document variable");
    expect(result.reportText).toContain("MatterRef");
    expect(result.reportText).not.toContain(SECRET);
  });
});

// ---------------------------------------------------------------------------
// ADEU-QA-002: structural diffs must replay; deletions must not restyle
// ---------------------------------------------------------------------------

describe("ADEU-QA-002 B: full-paragraph deletion keeps the following style", () => {
  it("deleting a styled paragraph does not restyle the following one", async () => {
    const doc = await createTestDocument();
    addStyledParagraph(doc, "Definitions", "Heading2");
    addParagraph(doc, "The first body paragraph after the heading.");
    addParagraph(doc, "A second body paragraph.");

    const text_o = cleanText(doc);
    // The projection may or may not render "## " depending on the fixture's
    // styles.xml; delete the whole first paragraph block as it projects.
    const first_block = text_o.split("\n\n")[0];
    const text_m = text_o.replace(first_block + "\n\n", "");

    const edits = generate_edits_via_paragraph_alignment(text_o, text_m);
    const engine = new RedlineEngine(doc, "QA");
    engine.process_batch(edits);

    expect(cleanText(doc).trim()).toBe(text_m.trim());

    // The merged container must carry the FOLLOWING paragraph's properties:
    // no Heading2 pStyle may survive on a paragraph whose visible text is
    // the body paragraph's.
    for (const p of findAllDescendants(doc.element, "w:p")) {
      const pPr = findChild(p, "w:pPr");
      const pStyle = pPr ? findChild(pPr, "w:pStyle") : null;
      if (pStyle && pStyle.getAttribute("w:val") === "Heading2") {
        throw new Error(
          "the deleted heading's style bled onto the surviving paragraph",
        );
      }
    }
  });

  it("structured diff replays a first-paragraph deletion exactly", async () => {
    const orig = await createTestDocument();
    addStyledParagraph(orig, "Definitions", "Heading2");
    addParagraph(orig, "The first body paragraph after the heading.");
    addParagraph(orig, "A second body paragraph.");

    const mod = await createTestDocument();
    addParagraph(mod, "The first body paragraph after the heading.");
    addParagraph(mod, "A second body paragraph.");

    const { text: text_o, structure: struct_o } = structured(orig);
    const { text: text_m, structure: struct_m } = structured(mod);
    const { edits } = generate_structured_edits(
      text_o,
      struct_o,
      text_m,
      struct_m,
    );

    // Node diff output keeps pins through JSON (plain properties).
    const replayed = JSON.parse(JSON.stringify(edits));
    const engine = new RedlineEngine(orig, "QA");
    engine.process_batch(replayed);
    expect(cleanText(orig).trim()).toBe(text_m.trim());
  });

  it("structured diff replays an adjacent paragraph swap exactly", async () => {
    const paras = [
      "Clause one sets the scene for the agreement.",
      "Clause two lists the payment obligations of the parties.",
      "Clause three covers termination and remedies at law.",
      "Clause four is the confidentiality clause.",
    ];
    const orig = await createTestDocument();
    for (const p of paras) addParagraph(orig, p);
    const swapped = [paras[0], paras[2], paras[1], paras[3]];
    const mod = await createTestDocument();
    for (const p of swapped) addParagraph(mod, p);

    const { text: text_o, structure: struct_o } = structured(orig);
    const { text: text_m, structure: struct_m } = structured(mod);
    const { edits } = generate_structured_edits(
      text_o,
      struct_o,
      text_m,
      struct_m,
    );

    const engine = new RedlineEngine(orig, "QA");
    engine.process_batch(JSON.parse(JSON.stringify(edits)));
    expect(cleanText(orig).trim()).toBe(text_m.trim());
  });
});

describe("ADEU-QA-002 C: virtual projection text never matches", () => {
  it("a target existing only in a meta bubble is not found", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc, "Reviewer");
    // "Editor" appears ONLY inside the {>>[Chg:1 delete] Editor ...<<}
    // bubble — never in document text.
    expect(() =>
      engine.process_batch([
        { type: "modify", target_text: "Editor", new_text: "X" },
      ]),
    ).toThrow(/not found/i);
  });

  it("a digit unique in body text resolves despite bubble timestamps", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Clause 7 applies to this agreement.");
    addParagraph(doc, "Another paragraph.");
    const engine = new RedlineEngine(doc, "Editor");
    engine.process_batch([
      {
        type: "modify",
        target_text: "Another",
        new_text: "A different",
        comment: "Reviewed on 2026-07-19",
      },
    ]);
    const doc2 = await DocumentObject.load(await doc.save());

    const engine2 = new RedlineEngine(doc2, "Reviewer");
    const stats = engine2.process_batch([
      { type: "modify", target_text: "7", new_text: "9" },
    ]);
    expect(stats.edits_applied).toBe(1);
    expect(cleanText(doc2)).toContain("Clause 9 applies");
  });
});

// ---------------------------------------------------------------------------
// ADEU-QA-004: replacement del+ins pairs — parity, linkage, counts
// ---------------------------------------------------------------------------

describe("ADEU-QA-004: replacement pair semantics", () => {
  it("projection annotates paired changes in both ingest and mapper", async () => {
    const doc = await buildTrackedChangeDoc();
    const raw = await extractTextFromBuffer(await doc.save());
    expect(raw).toContain("[Chg:1 delete] Editor (pairs with Chg:2)");
    expect(raw).toContain("[Chg:2 insert] Editor (pairs with Chg:1)");

    const doc2 = await DocumentObject.load(await doc.save());
    const mapper = new DocumentMapper(doc2);
    const rawNoAppendix = _extractTextFromDoc(doc2, false, false) as string;
    expect(mapper.full_text).toBe(rawNoAppendix);
  });

  it("standalone changes carry no pairing annotation", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Payment is due in 30 days.");
    const engine = new RedlineEngine(doc, "Editor");
    engine.apply_edits([
      { type: "modify", target_text: "Payment is due in 30 days.", new_text: "" },
    ]);
    const raw = await extractTextFromBuffer(await doc.save());
    expect(raw).not.toContain("pairs with");
  });

  it("accepting the deletion side resolves the paired insertion (engine parity)", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc, "Reviewer");
    const stats = engine.process_batch([
      { type: "accept", target_id: "Chg:1" },
    ]);
    expect(stats.actions_applied).toBe(1);

    const final = cleanText(doc);
    expect(final).toContain("60 days");
    expect(final).not.toContain("30 days");
    // The paired insertion must be fully resolved, not left pending.
    expect(findAllDescendants(doc.element, "w:ins").length).toBe(0);
    expect(findAllDescendants(doc.element, "w:del").length).toBe(0);
  });

  it("accepting both sides counts one state transition", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc, "Reviewer");
    const stats = engine.process_batch([
      { type: "accept", target_id: "Chg:1" },
      { type: "accept", target_id: "Chg:2" },
    ]);
    expect(stats.actions_applied).toBe(1);
    expect(stats.actions_already_resolved).toBe(1);
    expect(stats.actions_skipped).toBe(0);
    const details = (stats.skipped_details || []).join("\n").toLowerCase();
    expect(details).toContain("already resolved");

    const final = cleanText(doc);
    expect(final).toContain("60 days");
    expect(final).not.toContain("30 days");
  });

  it("rejecting both sides counts one state transition", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc, "Reviewer");
    const stats = engine.process_batch([
      { type: "reject", target_id: "Chg:1" },
      { type: "reject", target_id: "Chg:2" },
    ]);
    expect(stats.actions_applied).toBe(1);
    expect(stats.actions_already_resolved).toBe(1);

    const final = cleanText(doc);
    expect(final).toContain("30 days");
    expect(final).not.toContain("60 days");
  });

  it("contradictory actions on one pair are rejected up front", async () => {
    const doc = await buildTrackedChangeDoc();
    const engine = new RedlineEngine(doc, "Reviewer");
    let error: any = null;
    try {
      engine.process_batch([
        { type: "accept", target_id: "Chg:1" },
        { type: "reject", target_id: "Chg:2" },
      ]);
    } catch (e) {
      error = e;
    }
    expect(error).toBeInstanceOf(BatchValidationError);
    const msg = (error.errors || []).join("\n");
    expect(msg).toContain("Chg:1");
    expect(msg).toContain("Chg:2");
    expect(msg.toLowerCase()).toContain("pair");
    // Transactional: nothing may have been resolved.
    expect(findAllDescendants(doc.element, "w:ins").length).toBeGreaterThan(0);
    expect(findAllDescendants(doc.element, "w:del").length).toBeGreaterThan(0);
  });

  it("independent changes still resolve independently", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph mentioning alpha.");
    addParagraph(doc, "Second paragraph mentioning beta.");
    const engine = new RedlineEngine(doc, "Editor");
    engine.apply_edits([
      { type: "modify", target_text: "alpha", new_text: "ALPHA" },
    ]);
    engine.apply_edits([
      { type: "modify", target_text: "beta", new_text: "BETA" },
    ]);
    const doc2 = await DocumentObject.load(await doc.save());

    const raw = await extractTextFromBuffer(await doc2.save());
    const del_ids = [...raw.matchAll(/\[Chg:(\d+) delete\]/g)].map(
      (m) => m[1],
    );
    expect(del_ids.length).toBe(2);

    const engine2 = new RedlineEngine(doc2, "Reviewer");
    const stats = engine2.process_batch([
      { type: "accept", target_id: `Chg:${del_ids[0]}` },
      { type: "reject", target_id: `Chg:${del_ids[1]}` },
    ]);
    expect(stats.actions_applied).toBe(2);
    expect(stats.actions_already_resolved || 0).toBe(0);

    const final = cleanText(doc2);
    expect(final).toContain("ALPHA");
    expect(final).toContain("beta");
    expect(final).not.toContain("BETA");
  });
});
