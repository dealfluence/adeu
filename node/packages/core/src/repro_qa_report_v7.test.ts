// FILE: node/packages/core/src/repro_qa_report_v7.test.ts
/**
 * Node-engine repro tests for the 2026-07-19 black-box QA and UX report
 * (adeu 1.25.0+35c8bb4). Mirrors python/tests/test_repro_qa_report_v7.py
 * for the findings that live in the shared core engine:
 *
 *   F-02  a formatting-only replacement (bold -> italic) inherits the
 *         replaced span's bold instead of realizing exactly the markers
 *   F-03  a bold run with boundary whitespace projects as malformed
 *         Markdown (`**The Supplier **`) in both ingest and mapper
 *   F-04  an alt-text-only image difference becomes an unappliable edit
 *   F-06  match_mode=all rebuilds the document map once per occurrence
 *   F-09  sanitize leaves original comment timestamps un-normalized
 *   F-13  an invalid regex target is reported as "not found" instead of
 *         as a regex syntax error
 *   F-21  edits_applied counts occurrences instead of change objects
 *
 * Every test fails against the commit preceding its fix.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { DocumentMapper } from "./mapper.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { _extractTextFromDoc, ExtractStructure } from "./ingest.js";
import {
  generate_structured_edits,
  collect_media_difference_warnings,
} from "./diff.js";
import { zipSync } from "fflate";
import { finalize_document } from "./sanitize/core.js";
import { qn } from "./docx/dom.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function addStyledRun(
  p: Element,
  text: string,
  style: { bold?: boolean; italic?: boolean } = {},
): Element {
  const xmlDoc = p.ownerDocument!;
  const r = xmlDoc.createElement("w:r");
  if (style.bold || style.italic) {
    const rPr = xmlDoc.createElement("w:rPr");
    if (style.bold) rPr.appendChild(xmlDoc.createElement("w:b"));
    if (style.italic) rPr.appendChild(xmlDoc.createElement("w:i"));
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

/** Builds the QA report's F-02 fixture: "Normal before **Important phrase** normal after." */
async function buildBoldDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  const p = addEmptyParagraph(doc);
  addStyledRun(p, "Normal before ");
  addStyledRun(p, "Important phrase", { bold: true });
  addStyledRun(p, " normal after.");
  return doc;
}

function cleanText(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, { cleanView: true, includeAppendix: false }) as string;
}

// ---------------------------------------------------------------------------
// F-02: formatting-only replacements realize exactly the requested markers
// ---------------------------------------------------------------------------

describe("QA v7 F-02: formatting-only replacement fidelity", () => {
  it("clears inherited bold when the replacement carries explicit italic markers", async () => {
    const doc = await buildBoldDoc();
    const engine = new RedlineEngine(doc);
    engine.process_batch([
      {
        type: "modify",
        target_text: "**Important phrase**",
        new_text: "_Important phrase_",
      },
    ]);
    engine.accept_all_revisions();

    const accepted = cleanText(doc);
    expect(accepted).toContain("_Important phrase_");
    expect(accepted).not.toContain("**");
  });

  it("clears bold when the replacement removes the markers entirely", async () => {
    const doc = await buildBoldDoc();
    const engine = new RedlineEngine(doc);
    engine.process_batch([
      {
        type: "modify",
        target_text: "**Important phrase**",
        new_text: "Important phrase",
      },
    ]);
    engine.accept_all_revisions();

    const accepted = cleanText(doc);
    expect(accepted).toContain("Normal before Important phrase normal after.");
    expect(accepted).not.toContain("**");
  });

  it("still inherits bold for unmarked plain-text replacements", async () => {
    const doc = await buildBoldDoc();
    const engine = new RedlineEngine(doc);
    engine.process_batch([
      { type: "modify", target_text: "Important", new_text: "Critical" },
    ]);
    engine.accept_all_revisions();
    expect(cleanText(doc)).toContain("**Critical phrase**");
  });
});

// ---------------------------------------------------------------------------
// F-03: styled-run boundary whitespace stays outside emphasis markers
// ---------------------------------------------------------------------------

describe("QA v7 F-03: styled-run boundary whitespace", () => {
  async function buildBoundaryDoc(): Promise<DocumentObject> {
    const doc = await createTestDocument();
    const p = addEmptyParagraph(doc);
    addStyledRun(p, "The Supplier ", { bold: true });
    addStyledRun(p, "shall deliver");
    addStyledRun(p, " the Services by 31 December 2026.", { italic: true });
    return doc;
  }

  it("extraction emits valid markdown with whitespace outside the markers", async () => {
    const doc = await buildBoundaryDoc();
    const text = (_extractTextFromDoc(doc, { cleanView: false }) as string).trim();
    expect(text).toBe(
      "**The Supplier** shall deliver _the Services by 31 December 2026._",
    );
  });

  it("mapper projection matches ingest projection (virtual text contract)", async () => {
    const doc = await buildBoundaryDoc();
    const ingestText = (_extractTextFromDoc(doc, { cleanView: false }) as string).trim();
    const mapper = new DocumentMapper(doc);
    expect(mapper.full_text.trim()).toBe(ingestText);
  });

  it("plain-text edits across the styled boundary still apply", async () => {
    const doc = await buildBoundaryDoc();
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      { type: "modify", target_text: "shall deliver", new_text: "must deliver" },
    ]);
    expect(stats.edits_applied).toBe(1);
    engine.accept_all_revisions();
    expect(cleanText(doc)).toContain("must deliver");
  });

  it("whitespace-only styled runs are not wrapped", async () => {
    const doc = await createTestDocument();
    const p = addEmptyParagraph(doc);
    addStyledRun(p, "Alpha");
    addStyledRun(p, "   ", { bold: true });
    addStyledRun(p, "Omega");
    const text = (_extractTextFromDoc(doc, { cleanView: false }) as string).trim();
    expect(text).toBe("Alpha   Omega");
  });
});

// ---------------------------------------------------------------------------
// F-04: an alt-text-only image difference must not become an edit
// ---------------------------------------------------------------------------

describe("QA v7 F-04: image alt-text diffs", () => {
  it("drops hunks that reach inside a read-only image marker, with a warning", () => {
    const text_orig = "Before image.\n\n![RED logo](docx-image:1)\n\nAfter image.";
    const text_mod = "Before image.\n\n![BLUE logo](docx-image:1)\n\nAfter image.";
    const struct = (text: string): ExtractStructure => ({
      part_ranges: [[0, text.length, "body"]],
      tables: [],
    });

    const { edits, warnings } = generate_structured_edits(
      text_orig,
      struct(text_orig),
      text_mod,
      struct(text_mod),
    );

    expect(edits).toEqual([]);
    expect(
      warnings.some((w) => /alternative text|image/i.test(w)),
    ).toBe(true);
  });
});

describe("QA v7 F-04: media byte differences", () => {
  const makePkg = (mediaBytes: number[]): Uint8Array =>
    zipSync({
      "[Content_Types].xml": new TextEncoder().encode("<Types/>"),
      "word/document.xml": new TextEncoder().encode("<w:document/>"),
      "word/media/image1.png": new Uint8Array(mediaBytes),
    });

  it("warns when embedded media bytes differ", () => {
    const warnings = collect_media_difference_warnings(
      makePkg([1, 2, 3]),
      makePkg([4, 5, 6]),
    );
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toMatch(/embedded media differ/);
  });

  it("stays silent when media are byte-identical", () => {
    const warnings = collect_media_difference_warnings(
      makePkg([1, 2, 3]),
      makePkg([1, 2, 3]),
    );
    expect(warnings).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// F-06: match_mode=all must not rebuild the map once per occurrence
// ---------------------------------------------------------------------------

describe("QA v7 F-06: all-match application scaling", () => {
  it("keeps map rebuilds constant as occurrence count grows", async () => {
    const n = 30;
    const doc = await createTestDocument();
    for (let i = 0; i < n; i++) {
      addParagraph(
        doc,
        `Clause ${i}: The party PLACEHOLDER-${String(i).padStart(4, "0")} shall comply. REPLACEME token.`,
      );
    }
    const engine = new RedlineEngine(doc);

    const proto = DocumentMapper.prototype as any;
    const originalBuild = proto._build_map;
    let buildCount = 0;
    proto._build_map = function (this: any) {
      buildCount += 1;
      return originalBuild.call(this);
    };

    let stats: any;
    try {
      stats = engine.process_batch([
        {
          type: "modify",
          target_text: "REPLACEME",
          new_text: "SWAPPED",
          regex: true,
          match_mode: "all",
        },
      ]);
    } finally {
      proto._build_map = originalBuild;
    }

    expect(stats.edits_skipped).toBe(0);
    expect(buildCount).toBeLessThan(n);

    engine.accept_all_revisions();
    const final = cleanText(doc);
    expect(final.match(/SWAPPED/g)?.length).toBe(n);
    expect(final).not.toContain("REPLACEME");
  });
});

// ---------------------------------------------------------------------------
// F-09: comment timestamps must be normalized alongside change timestamps
// ---------------------------------------------------------------------------

describe("QA v7 F-09: comment timestamp normalization", () => {
  it("normalizes w:date and w16cex:dateUtc in retained comment parts", async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );
    const doc = await DocumentObject.load(readFileSync(fixturePath));

    await finalize_document(doc, {
      filename: "golden.docx",
      sanitize_mode: "keep-markup",
    });

    const commentParts = doc.pkg.parts.filter((p) =>
      p.contentType.includes("comments"),
    );
    expect(commentParts.length).toBeGreaterThan(0);

    const dates: string[] = [];
    for (const part of commentParts) {
      const xml = part._element.toString();
      for (const m of xml.matchAll(/(?:w:date|w16cex:dateUtc)="([^"]+)"/g)) {
        dates.push(m[1]);
      }
    }
    expect(dates.length).toBeGreaterThan(0);
    for (const d of dates) {
      expect(d.startsWith("2025-01-01")).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// F-13: invalid regex targets must be diagnosed as regex errors
// ---------------------------------------------------------------------------

describe("QA v7 F-13: invalid regex diagnosis", () => {
  it("names the regex problem instead of 'not found'", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "alpha DUPTOKEN one.");
    const engine = new RedlineEngine(doc);

    let error: BatchValidationError | null = null;
    try {
      engine.process_batch([
        { type: "modify", target_text: "[", new_text: "x", regex: true },
      ]);
    } catch (e) {
      error = e as BatchValidationError;
    }
    expect(error).toBeInstanceOf(BatchValidationError);
    const msg = (error as BatchValidationError).errors.join("\n");
    expect(msg).toMatch(/regular expression/i);
    expect(msg).not.toMatch(/not found/i);
  });

  it("valid regexes still apply", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "alpha DUPTOKEN one.");
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      { type: "modify", target_text: "DUP\\w+", new_text: "XTOKEN", regex: true },
    ]);
    expect(stats.edits_applied).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// F-21: edits_applied counts change objects, not occurrences
// ---------------------------------------------------------------------------

describe("QA v7 F-21: edits_applied semantics", () => {
  it("reports one applied edit with two modified occurrences", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "alpha DUPTOKEN one.");
    addParagraph(doc, "beta DUPTOKEN two.");
    const engine = new RedlineEngine(doc);
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "DUPTOKEN",
        new_text: "NEWTOKEN",
        match_mode: "all",
      },
    ]);
    expect(stats.edits.length).toBe(1);
    expect(stats.edits_applied).toBe(1);
    expect(stats.occurrences_modified).toBe(2);
    expect(stats.edits[0].occurrences_modified).toBe(2);
  });
});
