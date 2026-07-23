// FILE: node/packages/core/src/repro_qa_2026_07_23.markdown.test.ts
/**
 * Node-engine repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box QA
 * of v1.29.0+4bb70f9). Findings covered:
 *
 *   F5  Unsupported markdown handled inconsistently: hyperlink insertion is
 *       rejected with guidance pointing at a nonexistent "dedicated
 *       structural operation", while "- item" / "* item" paragraphs silently
 *       half-apply (ListParagraph style with NO resolvable w:numPr — Word
 *       renders indented text with no bullet).
 *   F6  Batch report previews are not faithful to the saved document:
 *       (1) match_mode "all" previews show only the first occurrence changed,
 *       (2) a pending insertion from a previous batch renders as accepted
 *           plain text in a later edit's preview,
 *       (3) a same-author re-edit of a pending insertion previews as nested
 *           CriticMarkup ({++ inside {++), which is invalid notation.
 *   F7  Dry-run report omits comments entirely — an edit carrying a comment
 *       leaves no trace of it in the dry-run report.
 *
 * Every test in this file is written test-first: it fails on current main
 * and passes once the finding is fixed.
 */

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { _extractTextFromDoc } from "./ingest.js";
import { findAllDescendants, findChild } from "./docx/dom.js";

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, true, false) as string;
}

function paragraphText(p: Element): string {
  return findAllDescendants(p, "w:t")
    .map((t) => t.textContent || "")
    .join("");
}

/** The paragraph whose visible text contains `needle` (there must be one). */
function findParagraphContaining(doc: DocumentObject, needle: string): Element {
  const hit = findAllDescendants(doc.element, "w:p").find((p) =>
    paragraphText(p).includes(needle),
  );
  expect(hit, `no paragraph containing ${JSON.stringify(needle)}`).toBeTruthy();
  return hit!;
}

function paragraphStyleId(p: Element): string | null {
  const pPr = findChild(p, "w:pPr");
  const pStyle = pPr ? findChild(pPr, "w:pStyle") : null;
  return pStyle ? pStyle.getAttribute("w:val") : null;
}

/**
 * True when the paragraph's numbering resolves to something Word will render
 * a list marker for: a direct <w:numPr> with numId != 0, or one inherited
 * from its paragraph style chain in styles.xml.
 */
function hasResolvableNumbering(doc: DocumentObject, p: Element): boolean {
  const pPr = findChild(p, "w:pPr");
  const direct = pPr ? findChild(pPr, "w:numPr") : null;
  if (direct) {
    const numId = findChild(direct, "w:numId");
    const val = numId ? numId.getAttribute("w:val") : null;
    if (val && val !== "0") return true;
  }

  let styleId = paragraphStyleId(p);
  const stylesPart = doc.pkg.getPartByPath("word/styles.xml");
  if (!styleId || !stylesPart) return false;
  const byId = new Map<string, Element>();
  for (const s of findAllDescendants(stylesPart._element, "w:style")) {
    const id = s.getAttribute("w:styleId");
    if (id) byId.set(id, s);
  }
  const visited = new Set<string>();
  while (styleId && !visited.has(styleId)) {
    visited.add(styleId);
    const style = byId.get(styleId);
    if (!style) break;
    const sPPr = findChild(style, "w:pPr");
    const numPr = sPPr ? findChild(sPPr, "w:numPr") : null;
    if (numPr) {
      const numId = findChild(numPr, "w:numId");
      const val = numId ? numId.getAttribute("w:val") : null;
      if (val && val !== "0") return true;
    }
    const basedOn = findChild(style, "w:basedOn");
    styleId = basedOn ? basedOn.getAttribute("w:val") : null;
  }
  return false;
}

async function buildContractDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Introduction paragraph of the agreement.");
  addParagraph(doc, "Terms and conditions apply.");
  return doc;
}

// ---------------------------------------------------------------------------
// F5: unsupported markdown — silent list degradation, dead-end link guidance
// ---------------------------------------------------------------------------

describe("F5: unsupported markdown must not half-apply or dead-end", () => {
  it("F5a: inserting a '- item' paragraph never yields a List style without numbering", async () => {
    const doc = await buildContractDoc();
    const engine = new RedlineEngine(doc, "Editor");
    let rejected = false;
    try {
      engine.process_batch([
        {
          type: "modify",
          target_text: "Terms and conditions apply.",
          new_text: "Terms and conditions apply.\n\n- item",
        },
      ]);
    } catch (e) {
      if (!(e instanceof BatchValidationError)) throw e;
      // A clean up-front rejection (the policy hyperlinks get) is acceptable.
      rejected = true;
    }
    if (rejected) return;

    const doc2 = await DocumentObject.load(await doc.save());
    const p = findParagraphContaining(doc2, "item");
    const styleId = paragraphStyleId(p);

    if (styleId && /list/i.test(styleId)) {
      // The engine decided this IS a list: then it must be a real one that
      // Word renders with a marker — not the half-applied ListParagraph
      // (indent, no bullet) state.
      expect(
        hasResolvableNumbering(doc2, p),
        `paragraph got list style '${styleId}' but no resolvable w:numPr — ` +
          "Word renders indented text with NO bullet (silent half-apply)",
      ).toBe(true);
    } else {
      // The engine decided this is NOT a list: then the text must round-trip
      // literally so nothing was silently dropped.
      expect(cleanText(doc2)).toContain("- item");
    }
  });

  it("F5b: inserting '* item' (the projection's own bullet syntax) produces a real bullet", async () => {
    const doc = await buildContractDoc();
    const engine = new RedlineEngine(doc, "Editor");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "Terms and conditions apply.",
        new_text: "Terms and conditions apply.\n\n* item",
      },
    ]);
    expect(stats.edits_applied).toBe(1);

    const doc2 = await DocumentObject.load(await doc.save());
    const p = findParagraphContaining(doc2, "item");
    expect(
      hasResolvableNumbering(doc2, p),
      "the inserted bullet paragraph carries no resolvable w:numPr " +
        "(direct or via its style) — Word renders no bullet",
    ).toBe(true);

    // Round-trip: the clean view must project the paragraph back as the very
    // bullet syntax the projection documents ("* item").
    const lines = cleanText(doc2).split("\n");
    expect(
      lines.some((l) => l.trim() === "* item"),
      `clean view lost the bullet marker; lines: ${JSON.stringify(lines)}`,
    ).toBe(true);
  });

  it("F5c: hyperlink rejection must not point at a nonexistent 'dedicated structural operation'", async () => {
    const doc = await buildContractDoc();
    const engine = new RedlineEngine(doc, "Editor");
    let error: any = null;
    try {
      engine.process_batch([
        {
          type: "modify",
          target_text: "Terms and conditions apply.",
          new_text: "See [the portal](https://example.com) for details.",
        },
      ]);
    } catch (e) {
      error = e;
    }
    // The hard error itself is fine (and stays): only its guidance is wrong.
    expect(error).toBeInstanceOf(BatchValidationError);
    const msg = (error.errors || [error.message]).join("\n");
    expect(msg.toLowerCase()).toContain("hyperlink");
    // No tool in the MCP toolset inserts hyperlinks; recommending a
    // "dedicated structural operation" is a dead end for agents.
    expect(msg).not.toContain("dedicated structural operation");
  });
});

// ---------------------------------------------------------------------------
// F6: batch report previews must be faithful to the saved document
// ---------------------------------------------------------------------------

describe("F6: batch report previews are faithful", () => {
  it("F6.1: match_mode 'all' previews show every occurrence modified", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "apple apple apple.");
    const engine = new RedlineEngine(doc, "Editor");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "apple",
        new_text: "pear",
        match_mode: "all",
      },
    ]);
    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    expect(report.occurrences_modified).toBe(3);
    // The saved document is correct — all three occurrences are modified —
    // so both previews must reflect that, not just the first occurrence.
    expect(report.clean_text).toContain("pear pear pear");
    const criticWithoutDeletions = (report.critic_markup || "").replace(
      /\{--[\s\S]*?--\}/g,
      "",
    );
    expect(
      criticWithoutDeletions,
      "CriticMarkup preview leaves occurrences looking unmodified: " +
        report.critic_markup,
    ).not.toContain("apple");
  });

  it("F6.2: a pending insertion from a previous batch keeps its CriticMarkup in later previews", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Payment is due in 30 days after the invoice date.");
    const engine1 = new RedlineEngine(doc, "Editor");
    engine1.process_batch([
      {
        type: "modify",
        target_text: "30 days",
        new_text: "30 business days",
      },
    ]);

    // New session over the saved file: "business " is still PENDING.
    const doc2 = await DocumentObject.load(await doc.save());
    const engine2 = new RedlineEngine(doc2, "Editor");
    const stats = engine2.process_batch([
      {
        type: "modify",
        target_text: "business days after",
        new_text: "business days following",
      },
    ]);
    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    // The pending insertion is inside the preview window. Rendering it as
    // plain "business" makes it look silently accepted (it was not) — it
    // must keep its {++business ++} markup.
    expect(report.critic_markup || "").toMatch(/\{\+\+business ?\+\+\}/);
  });

  it("F6.3: previews never nest {++ inside {++ (invalid CriticMarkup)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The parties shall resolve disputes amicably.");
    const engine1 = new RedlineEngine(doc, "Editor");
    engine1.process_batch([
      {
        type: "modify",
        target_text: "resolve disputes amicably",
        new_text: "first negotiate in good faith",
      },
    ]);

    // Same-author re-edit of the still-pending insertion (allowed; merges).
    const doc2 = await DocumentObject.load(await doc.save());
    const engine2 = new RedlineEngine(doc2, "Editor");
    const stats = engine2.process_batch([
      {
        type: "modify",
        target_text: "negotiate in good faith",
        new_text: "negotiate in utmost good faith",
      },
    ]);
    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    expect(
      report.critic_markup || "",
      "preview nests {++ inside {++ — invalid CriticMarkup notation",
    ).not.toMatch(/\{\+\+[^}]*\{\+\+/);
  });
});

// ---------------------------------------------------------------------------
// F7: dry-run report must mention the edit's comment
// ---------------------------------------------------------------------------

describe("F7: dry-run report carries comments", () => {
  it("an edit's comment text appears in the dry-run report", async () => {
    const COMMENT = "Flagged for legal review: payment term doubled.";
    const doc = await createTestDocument();
    addParagraph(doc, "Payment is due in 30 days.");
    const engine = new RedlineEngine(doc, "Editor");
    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "30 days",
          new_text: "60 days",
          comment: COMMENT,
        },
      ],
      true, // dry_run
    );
    const report = stats.edits[0];
    expect(report.status).toBe("applied");
    // Verifying what WILL happen is the whole point of dry-run: the report
    // must carry the comment (its text, or at minimum that one is attached).
    expect(
      JSON.stringify(stats),
      "dry-run report contains no trace of the edit's comment",
    ).toContain(COMMENT);
  });
});
