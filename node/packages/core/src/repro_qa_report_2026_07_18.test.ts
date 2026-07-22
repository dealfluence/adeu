// FILE: src/repro_qa_report_2026_07_18.test.ts
//
// Node-side regression tests for the 2026-07-18 black-box QA report
// (adeu 1.22.0). The report was executed against the Python CLI; per the
// "Make Both Perfect" principle every engine-level finding is ported and
// verified here too (CLI-only findings H2/M2/M8/L2-L6 have no Node surface):
//
//   C1  cross-part edits — the flattened projection let diff/apply write a
//       new final body paragraph into word/footer1.xml
//   C2  DOCX-to-DOCX table row changes became pipe-text writes into one cell
//   H1  the read-only structural appendix leaked into diff extraction
//   H4  comments anchored in footers/footnotes produce files Word/LibreOffice
//       cannot open
//   M1  markup did not honor apply's semantics (regex, match_mode, failures)
//   M3  VML watermarks survived sanitize unreported (ported with H3)
//   M4  style-based lists (List Bullet / List Number) lost list semantics
//   M5  inline images disappeared from the projection
//   M6  reserved separator footnotes with untyped ids surfaced as [^fn--1]
//   M7  only the first quoted defined term per paragraph was captured
//   L1  verified only: Node's no-match advice names real MCP parameters

import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { serializeXml } from "./docx/dom.js";
import { RedlineEngine, BatchValidationError, validate_edit_strings } from "./engine.js";
import { DocumentMapper } from "./mapper.js";
import {
  extractTextFromBuffer,
  _extractTextFromDoc,
} from "./ingest.js";
import {
  generate_edits_from_text,
  generate_structured_edits,
} from "./diff.js";
import { apply_edits_to_markdown } from "./markup.js";
import {
  extract_all_domain_metadata,
  extract_terms_from_paragraph,
} from "./domain.js";
import { detect_watermarks } from "./sanitize/transforms.js";
import { finalize_document } from "./sanitize/core.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

const CT_HEADER =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml";
const CT_FOOTER =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml";
const CT_FOOTNOTES =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml";

// ---------------------------------------------------------------------------
// Fixture builders (raw OOXML node injection into the initial.docx fixture,
// mirroring python/tests/test_repro_qa_2026_07_18.py builders)
// ---------------------------------------------------------------------------

/** Header + body + footer document. Mirrors Python's build_header_footer_doc. */
async function buildHeaderFooterDoc(
  extraBodyParagraph = false,
): Promise<DocumentObject> {
  const doc = await createTestDocument();
  doc.pkg.addPart(
    "/word/header1.xml",
    CT_HEADER,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:hdr xmlns:w="${W_NS}"><w:p><w:r><w:t xml:space="preserve">HEADER MARKER</w:t></w:r></w:p></w:hdr>`,
  );
  doc.pkg.addPart(
    "/word/footer1.xml",
    CT_FOOTER,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:ftr xmlns:w="${W_NS}"><w:p><w:r><w:t xml:space="preserve">FOOTER MARKER</w:t></w:r></w:p></w:ftr>`,
  );
  addParagraph(doc, "Body paragraph one.");
  if (extraBodyParagraph) addParagraph(doc, "New final body paragraph.");
  return doc;
}

function footerXml(doc: DocumentObject): string {
  const part = doc.pkg.getPartByPath("word/footer1.xml");
  return part ? serializeXml(part._element) : "";
}

function bodyXml(doc: DocumentObject): string {
  return serializeXml(doc.pkg.mainDocumentPart._element);
}

const FOOTNOTE_SEP_TYPED =
  '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>';
const FOOTNOTE_CONT_TYPED =
  '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>';
// Reserved notes as emitted by some generators: ids -1/0 but NO w:type attribute.
const FOOTNOTE_SEP_UNTYPED =
  '<w:footnote w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>';
const FOOTNOTE_CONT_UNTYPED =
  '<w:footnote w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>';

/**
 * A body paragraph carrying footnote 1, plus separator/continuation notes.
 * Mirrors Python's build_footnote_doc.
 */
async function buildFootnoteDoc(
  typedSeparators = true,
  noteText = "This is a QA footnote about governing law.",
): Promise<DocumentObject> {
  const doc = await createTestDocument();
  const p = addParagraph(doc, "The governing law clause");
  const xmlDoc = doc.element.ownerDocument!;
  const r = xmlDoc.createElement("w:r");
  const ref = xmlDoc.createElement("w:footnoteReference");
  ref.setAttribute("w:id", "1");
  r.appendChild(ref);
  p.appendChild(r);
  addParagraph(doc, "Second body paragraph.");

  const sep = typedSeparators ? FOOTNOTE_SEP_TYPED : FOOTNOTE_SEP_UNTYPED;
  const cont = typedSeparators ? FOOTNOTE_CONT_TYPED : FOOTNOTE_CONT_UNTYPED;
  doc.pkg.addPart(
    "/word/footnotes.xml",
    CT_FOOTNOTES,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:footnotes xmlns:w="${W_NS}">${sep}${cont}` +
      `<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r>` +
      `<w:r><w:t xml:space="preserve"> ${noteText}</w:t></w:r></w:p></w:footnote>` +
      `</w:footnotes>`,
  );
  return doc;
}

function footnotesXml(doc: DocumentObject): string {
  const part = doc.pkg.getPartByPath("word/footnotes.xml");
  return part ? serializeXml(part._element) : "";
}

/** SLA table document. Mirrors Python's build_table_doc. */
async function buildTableDoc(
  opts: { extraRow?: boolean; dropMiddleRow?: boolean } = {},
): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Service Level Agreement");
  let rows = [
    ["Service", "Uptime", "Price"],
    ["Standard support", "99.0%", "EUR 1,000"],
    ["Premium support", "99.9%", "EUR 2,000"],
  ];
  if (opts.dropMiddleRow) rows = [rows[0], rows[2]];
  if (opts.extraRow) rows = [...rows, ["Backup service", "99.5%", "EUR 500"]];
  const tbl = addTable(doc, rows.length, 3);
  // addTable appends after the trailing paragraph; rebuild order: para, table, para.
  for (let i = 0; i < rows.length; i++) {
    for (let j = 0; j < 3; j++) setCellText(tbl, i, j, rows[i][j]);
  }
  addParagraph(doc, "Documentation: see appendix.");
  return doc;
}

/** Extracts { text, structure } via the structured extraction API. */
function extractWithStructure(doc: DocumentObject): { text: string; structure: any } {
  return (_extractTextFromDoc as any)(doc, true, false, false, true);
}

/** Body paragraph with an inline image (wp:docPr id=7, descr+title). */
async function buildImageDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Before image.");
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  const r = xmlDoc.createElement("w:r");
  const drawing = xmlDoc.createElement("w:drawing");
  const inline = xmlDoc.createElement("wp:inline");
  const docPr = xmlDoc.createElement("wp:docPr");
  docPr.setAttribute("id", "7");
  docPr.setAttribute("name", "Picture 7");
  docPr.setAttribute("descr", "Red rectangle QA diagram");
  docPr.setAttribute("title", "Red rectangle QA diagram");
  inline.appendChild(docPr);
  drawing.appendChild(inline);
  r.appendChild(drawing);
  p.appendChild(r);
  doc.element.appendChild(p);
  addParagraph(doc, "After image.");
  return doc;
}

/** List Bullet / List Number styles resolved via styles.xml + numbering.xml. */
async function buildStyleListDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  const stylesPart = doc.pkg.getPartByPath("word/styles.xml")!;
  const sDoc = stylesPart._element.ownerDocument!;
  const addListStyle = (styleId: string, name: string, numId: string) => {
    const style = sDoc.createElement("w:style");
    style.setAttribute("w:type", "paragraph");
    style.setAttribute("w:styleId", styleId);
    const nameEl = sDoc.createElement("w:name");
    nameEl.setAttribute("w:val", name);
    style.appendChild(nameEl);
    const basedOn = sDoc.createElement("w:basedOn");
    basedOn.setAttribute("w:val", "Normal");
    style.appendChild(basedOn);
    const pPr = sDoc.createElement("w:pPr");
    const numPr = sDoc.createElement("w:numPr");
    const ilvl = sDoc.createElement("w:ilvl");
    ilvl.setAttribute("w:val", "0");
    const numIdEl = sDoc.createElement("w:numId");
    numIdEl.setAttribute("w:val", numId);
    numPr.appendChild(ilvl);
    numPr.appendChild(numIdEl);
    pPr.appendChild(numPr);
    style.appendChild(pPr);
    stylesPart._element.appendChild(style);
  };
  addListStyle("ListBullet", "List Bullet", "1");
  addListStyle("ListNumber", "List Number", "2");

  doc.pkg.addPart(
    "/word/numbering.xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:numbering xmlns:w="${W_NS}">` +
      `<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>` +
      `<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>` +
      `<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>` +
      `<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>` +
      `</w:numbering>`,
  );

  const addStyledParagraph = (text: string, styleId: string) => {
    const p = addParagraph(doc, text);
    const xmlDoc = doc.element.ownerDocument!;
    const pPr = xmlDoc.createElement("w:pPr");
    const pStyle = xmlDoc.createElement("w:pStyle");
    pStyle.setAttribute("w:val", styleId);
    pPr.appendChild(pStyle);
    p.insertBefore(pPr, p.firstChild);
  };

  addParagraph(doc, "Requirements:");
  addStyledParagraph("Maintain ISO 27001 controls", "ListBullet");
  addStyledParagraph("Notify incidents without undue delay", "ListBullet");
  addParagraph(doc, "Escalation:");
  addStyledParagraph("First escalation", "ListNumber");
  addStyledParagraph("Second escalation", "ListNumber");
  return doc;
}

/** Header with a VML watermark shape (v:textpath). Mirrors build_watermark_doc. */
async function buildWatermarkDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Confidential body.");
  doc.pkg.addPart(
    "/word/header1.xml",
    CT_HEADER,
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
      `<w:hdr xmlns:w="${W_NS}" xmlns:v="urn:schemas-microsoft-com:vml">` +
      `<w:p><w:r><w:t>Header text</w:t></w:r></w:p>` +
      `<w:p><w:r><w:pict>` +
      `<v:shape id="PowerPlusWaterMarkObject" type="#_x0000_t136" style="position:absolute;width:400pt;height:100pt">` +
      `<v:textpath style="font-family:Calibri" string="DRAFT ACME SECRET"/>` +
      `</v:shape>` +
      `</w:pict></w:r></w:p></w:hdr>`,
  );
  return doc;
}

// ---------------------------------------------------------------------------
// C1 — cross-part (body/footer) edits
// ---------------------------------------------------------------------------

describe("C1: OPC part boundaries", () => {
  it("mapper records part ranges, kinds and boundaries", async () => {
    const doc = await buildHeaderFooterDoc();
    const mapper = new DocumentMapper(doc);

    const kinds = (mapper as any).part_ranges
      .filter(([s, e]: [number, number, string]) => e > s)
      .map(([, , k]: [number, number, string]) => k);
    expect(kinds).toEqual(["header", "body", "footer"]);

    const full = mapper.full_text;
    expect(full).toBe("HEADER MARKER\n\nBody paragraph one.\n\nFOOTER MARKER");

    expect((mapper as any).part_kind_at(full.indexOf("HEADER"))).toBe("header");
    expect((mapper as any).part_kind_at(full.indexOf("Body"))).toBe("body");
    expect((mapper as any).part_kind_at(full.indexOf("FOOTER"))).toBe("footer");

    // Inside the separator between body and footer (prev_end < idx <= next_start).
    const bodyEnd = full.indexOf("one.") + "one.".length;
    const footerStart = full.indexOf("FOOTER MARKER");
    expect((mapper as any).part_boundary_at(bodyEnd)).toBeNull();
    const boundary = (mapper as any).part_boundary_at(footerStart);
    expect(boundary).not.toBeNull();
    const [prevIdx, nextIdx] = boundary!;
    expect((mapper as any).part_kind_of(prevIdx)).toBe("body");
    expect((mapper as any).part_kind_of(nextIdx)).toBe("footer");

    // Every real span carries the part it was projected from.
    for (const s of mapper.spans) {
      if (s.run === null) continue;
      if (s.text.includes("FOOTER")) {
        expect((s as any).part_index).toBe(nextIdx);
      }
      if (s.text.includes("Body paragraph")) {
        expect((s as any).part_index).toBe(prevIdx);
      }
    }
  });

  it("validate_edits rejects a replacement spanning the body/footer wall", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text: "paragraph one.\n\nFOOTER MARKER",
        new_text: "paragraph two.\n\nALTERED FOOTER",
      },
    ]);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.join("\n")).toContain(
      "spans a structural document-part boundary (body → footer)",
    );
  });

  it("structured diff + apply keeps the new final paragraph in the body", async () => {
    const orig = await buildHeaderFooterDoc();
    const mod = await buildHeaderFooterDoc(true);

    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits, warnings } = generate_structured_edits(
      o.text,
      o.structure,
      m.text,
      m.structure,
    );
    expect(warnings).toEqual([]);
    expect(edits.length).toBeGreaterThan(0);
    // No edit may target text on both sides of the body/footer wall.
    for (const e of edits as any[]) {
      const tgt = e.target_text || "";
      expect(tgt.includes("FOOTER") && tgt.includes("one.")).toBe(false);
    }

    const applyDoc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(applyDoc);
    engine.process_batch(edits as any[]);

    expect(bodyXml(applyDoc)).toContain("New final body paragraph.");
    expect(footerXml(applyDoc)).not.toContain("New final body paragraph.");

    const clean = await extractTextFromBuffer(await applyDoc.save(), true);
    expect(clean).toContain("Body paragraph one.\n\nNew final body paragraph.");
    expect(clean).not.toContain("New final body paragraph.FOOTER");
  });

  it("a plain insertion pinned at the footer start re-anchors to the body", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const footerStart = engine.mapper.full_text.indexOf("FOOTER MARKER");

    const [applied, skipped] = engine.apply_edits([
      {
        type: "modify",
        target_text: "",
        new_text: "New final body paragraph.",
        _match_start_index: footerStart,
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);

    expect(bodyXml(doc)).toContain("New final body paragraph.");
    expect(footerXml(doc)).not.toContain("New final body paragraph.");

    const clean = await extractTextFromBuffer(await doc.save(), true);
    expect(clean).toContain("Body paragraph one.\n\nNew final body paragraph.");
    expect(clean).toContain("FOOTER MARKER");
  });

  it("apply-level backstop: a pinned modification may never mutate two parts", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const full = engine.mapper.full_text;
    const start = full.indexOf("one.");
    const target = full.substring(start, full.indexOf("MARKER", start) + "MARKER".length);
    expect(target).toContain("FOOTER"); // sanity: really crosses the wall

    const [applied, skipped] = engine.apply_edits([
      {
        type: "modify",
        target_text: target,
        new_text: "",
        _match_start_index: start,
      },
    ]);
    expect(applied).toBe(0);
    expect(skipped).toBe(1);
    expect(footerXml(doc)).toContain("FOOTER MARKER");
  });
});

// ---------------------------------------------------------------------------
// C2 — structured table row operations
// ---------------------------------------------------------------------------

describe("C2: table row round trip", () => {
  it("structured extraction reports part ranges and table row geometry", async () => {
    const doc = await buildTableDoc();
    const { text, structure } = extractWithStructure(doc);
    expect(structure.part_ranges.length).toBe(1);
    expect(structure.part_ranges[0][2]).toBe("body");
    expect(structure.tables.length).toBe(1);
    const table = structure.tables[0];
    expect(table.rows.length).toBe(3);
    expect(table.rows[0].cells).toEqual(["Service", "Uptime", "Price"]);
    for (const row of table.rows) {
      expect(text.substring(row.start, row.end)).toBe(row.cells.join(" | "));
    }
  });

  it("diff emits insert_row for an added row (below anchor, no pipe smuggling)", async () => {
    const orig = await buildTableDoc();
    const mod = await buildTableDoc({ extraRow: true });
    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const rowOps = (edits as any[]).filter((e) => e.type === "insert_row");
    expect(rowOps.length).toBe(1);
    expect(rowOps[0].cells).toEqual(["Backup service", "99.5%", "EUR 500"]);
    expect(rowOps[0].position).toBe("below");
    expect(rowOps[0].target_text).toBe("Premium support | 99.9% | EUR 2,000");
    for (const e of edits as any[]) {
      if (e.type === "modify") {
        expect(e.new_text || "").not.toContain("Backup service | 99.5%");
      }
    }
  });

  it("row addition round-trips through the engine", async () => {
    const orig = await buildTableDoc();
    const mod = await buildTableDoc({ extraRow: true });
    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const engine = new RedlineEngine(orig);
    const res = engine.process_batch(edits as any[]);
    expect(res.edits_applied).toBeGreaterThan(0);
    expect(res.edits_skipped).toBe(0);

    const buf = await orig.save();
    const clean = await extractTextFromBuffer(buf, true, false);
    expect(clean).toContain(
      "Premium support | 99.9% | EUR 2,000\nBackup service | 99.5% | EUR 500",
    );

    // Follow-up structured diff must converge to zero edits.
    const applied = await DocumentObject.load(buf);
    const a = extractWithStructure(applied);
    const again = generate_structured_edits(a.text, a.structure, m.text, m.structure);
    expect(again.edits).toEqual([]);
  });

  it("diff emits delete_row for a removed row and round-trips", async () => {
    const orig = await buildTableDoc();
    const mod = await buildTableDoc({ dropMiddleRow: true });
    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const rowOps = (edits as any[]).filter((e) => e.type === "delete_row");
    expect(rowOps.length).toBe(1);
    expect(rowOps[0].target_text).toBe("Standard support | 99.0% | EUR 1,000");

    const engine = new RedlineEngine(orig);
    engine.process_batch(edits as any[]);
    const buf = await orig.save();
    const clean = await extractTextFromBuffer(buf, true, false);
    expect(clean).not.toContain("Standard support");

    const applied = await DocumentObject.load(buf);
    const a = extractWithStructure(applied);
    const again = generate_structured_edits(a.text, a.structure, m.text, m.structure);
    expect(again.edits).toEqual([]);
  });

  it("a 1:1 modified row still diffs as a text edit, not a row op", async () => {
    const orig = await buildTableDoc();
    const mod = await buildTableDoc();
    // EUR 1,000 -> EUR 1,250 in the modified doc.
    const tbl = mod.element.getElementsByTagName("w:tbl")[0];
    setCellText(tbl, 1, 2, "EUR 1,250");

    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);
    expect((edits as any[]).every((e) => e.type === "modify")).toBe(true);

    const engine = new RedlineEngine(orig);
    engine.process_batch(edits as any[]);
    const clean = await extractTextFromBuffer(await orig.save(), true, false);
    expect(clean).toContain("Standard support | 99.0% | EUR 1,250");
  });

  it("validate_edits rejects a replacement smuggling a pipe row into a table", async () => {
    const doc = await buildTableDoc();
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text: "2,000\n\nDocumentation:",
        new_text: "2,000\nBackup service | 99.5% | EUR 500\n\nDocumentation:",
      },
    ]);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.join("\n")).toContain("insert_row");
  });

  it("apply-level backstop: a pinned insertion of row-shaped text is refused", async () => {
    const doc = await buildTableDoc();
    const engine = new RedlineEngine(doc);
    // Pinned inside the table (an insert-above shape from a stale diff).
    const idx = engine.mapper.full_text.indexOf("Premium support");
    const [applied, skipped] = engine.apply_edits([
      {
        type: "modify",
        target_text: "",
        new_text: "Backup service | 99.5% | EUR 500\n",
        _match_start_index: idx,
      },
    ]);
    expect(applied).toBe(0);
    expect(skipped).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// H1 — appendix leakage into diff extraction
// ---------------------------------------------------------------------------

async function buildDefinedTermsBuffer(extraUses = false): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(
    doc,
    '"Agreement" means this service agreement between the parties.',
  );
  addParagraph(doc, "The Agreement enters into force upon signature.");
  if (extraUses) {
    addParagraph(doc, "Termination of the Agreement requires notice.");
    addParagraph(doc, "Amendments to the Agreement must be written.");
  }
  return doc.save();
}

describe("H1: the structural appendix must not leak into diff", () => {
  it("extractTextFromBuffer(buf, clean, includeAppendix=false) omits the appendix", async () => {
    const buf = await buildDefinedTermsBuffer();
    const withAppendix = await extractTextFromBuffer(buf, true);
    expect(withAppendix).toContain("Document Structure (Read-Only)");
    const without = await extractTextFromBuffer(buf, true, false);
    expect(without).not.toContain("Document Structure");
    expect(without).not.toContain("— used ");
  });

  it("a usage-count-only change produces no appendix edits", async () => {
    const origBuf = await buildDefinedTermsBuffer();
    const modBuf = await buildDefinedTermsBuffer(true);
    const textOrig = await extractTextFromBuffer(origBuf, true, false);
    const textMod = await extractTextFromBuffer(modBuf, true, false);
    const edits = generate_edits_from_text(textOrig, textMod);
    expect(edits.length).toBeGreaterThan(0);
    for (const e of edits) {
      const joined = (e.target_text || "") + (e.new_text || "");
      expect(joined).not.toContain("— used ");
      expect(joined).not.toContain("Document Structure");
    }
  });
});

// ---------------------------------------------------------------------------
// H4 — comments outside the main document story
// ---------------------------------------------------------------------------

describe("H4: comments outside the main story", () => {
  it("a tracked footnote edit applies but drops the comment with a warning", async () => {
    const doc = await buildFootnoteDoc();
    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "QA footnote about governing law",
        new_text: "QA footnote about applicable law",
        comment: "Diff: Replacement",
      } as any,
    ]);
    expect(res.edits_applied).toBe(1);

    const fnXml = footnotesXml(doc);
    expect(fnXml).toContain("applicable");
    expect(fnXml).not.toContain("commentReference");
    expect(fnXml).not.toContain("commentRangeStart");

    const details = engine.skipped_details.join("\n");
    expect(details).toContain("was NOT attached");
    expect(details).toContain("footnote");
  });

  it("a comment-only edit on footnote text fails validation", async () => {
    const doc = await buildFootnoteDoc();
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text: "QA footnote about governing law",
        new_text: "QA footnote about governing law",
        comment: "please review",
      },
    ]);
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("comments cannot be anchored inside a footnotes part");
  });

  it("a footer edit drops its comment while a body comment still attaches", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "FOOTER MARKER",
        new_text: "FOOTER MARK",
        comment: "footer comment",
      } as any,
      {
        type: "modify",
        target_text: "Body paragraph one.",
        new_text: "Body paragraph 1.",
        comment: "body comment",
      } as any,
    ]);
    expect(res.edits_applied).toBe(2);

    expect(footerXml(doc)).not.toContain("commentReference");
    expect(bodyXml(doc)).toContain("commentRangeStart");
    expect(engine.skipped_details.join("\n")).toContain("footer");
  });
});

// ---------------------------------------------------------------------------
// M1 — markup parity with apply's edit semantics
// ---------------------------------------------------------------------------

describe("M1: markup honors apply's edit semantics", () => {
  const md = "Alpha fee applies.\n\nBeta fee applies.\n\nGamma fee applies.";

  it('match_mode "all" marks every occurrence', () => {
    const result = apply_edits_to_markdown(md, [
      { type: "modify", target_text: "fee", new_text: "charge", match_mode: "all" } as any,
    ]);
    expect((result.match(/\{--fee--\}\{\+\+charge\+\+\}/g) || []).length).toBe(3);
  });

  it("strict (default) refuses an ambiguous target, exactly like apply", () => {
    const reports: any[] = [];
    const result = apply_edits_to_markdown(
      md,
      [{ type: "modify", target_text: "fee", new_text: "charge" } as any],
      false,
      false,
      reports,
    );
    expect(result).toBe(md);
    expect(reports.length).toBe(1);
    expect(reports[0].status).toBe("failed");
    expect(reports[0].error).toContain("Ambiguous");
  });

  it('match_mode "first" gives the previous first-occurrence behavior', () => {
    const result = apply_edits_to_markdown(md, [
      { type: "modify", target_text: "fee", new_text: "charge", match_mode: "first" } as any,
    ]);
    expect((result.match(/\{--fee--\}/g) || []).length).toBe(1);
    expect(result.startsWith("Alpha {--fee--}{++charge++}")).toBe(true);
  });

  it("regex targets are honored", () => {
    const result = apply_edits_to_markdown(md, [
      {
        type: "modify",
        target_text: "(?<=Alpha )fee",
        new_text: "charge",
        regex: true,
      } as any,
    ]);
    expect(result).toContain("Alpha {--fee--}{++charge++}");
    expect(result).toContain("Beta fee applies.");
  });

  it("a missing target is a per-edit failure, never a silent skip", () => {
    const reports: any[] = [];
    const result = apply_edits_to_markdown(
      md,
      [{ type: "modify", target_text: "does not exist", new_text: "x" } as any],
      false,
      false,
      reports,
    );
    expect(result).toBe(md);
    expect(reports[0].status).toBe("failed");
    expect(reports[0].error).toContain("not found");
  });

  it("an empty target is recorded as a failure", () => {
    const reports: any[] = [];
    apply_edits_to_markdown(
      md,
      [{ type: "modify", target_text: "", new_text: "inserted" } as any],
      false,
      false,
      reports,
    );
    expect(reports[0].status).toBe("failed");
    expect(reports[0].error).toContain("target_text is empty");
  });

  it("edit reports carry truthful applied/occurrence stats", () => {
    const reports: any[] = [];
    apply_edits_to_markdown(
      md,
      [
        { type: "modify", target_text: "Alpha fee", new_text: "Alpha charge" } as any,
        { type: "modify", target_text: "fee", new_text: "charge", match_mode: "all" } as any,
      ],
      false,
      false,
      reports,
    );
    expect(reports.length).toBe(2);
    expect(reports[0]).toMatchObject({ index: 0, status: "applied", occurrences: 1 });
    // "Alpha fee" occupies the first "fee": the fan-out overlaps there and the
    // edit is reported failed (mirrors Python's overlap accounting).
    expect(reports[1].index).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// M3 — VML watermark reporting (ported alongside H3's sanitize fixes)
// ---------------------------------------------------------------------------

describe("M3: sanitize reports watermark-like VML text objects", () => {
  it("detect_watermarks finds the header textpath", async () => {
    const doc = await buildWatermarkDoc();
    const warnings = detect_watermarks(doc);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain("Watermark-like text object in header");
    expect(warnings[0]).toContain("DRAFT ACME SECRET");
    expect(warnings[0]).toContain("NOT removed");
  });

  it("finalize_document surfaces the watermark in its report", async () => {
    const doc = await buildWatermarkDoc();
    const { reportText } = await finalize_document(doc, {
      filename: "wm.docx",
      sanitize_mode: "full",
      accept_all: true,
    });
    expect(reportText).toContain("DRAFT ACME SECRET");
    expect(reportText.toLowerCase()).toContain("watermark");
  });

  it("a clean document reports no watermarks", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "No watermark here.");
    expect(detect_watermarks(doc)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// M4 — style-based list semantics
// ---------------------------------------------------------------------------

describe("M4: style-based lists project list markers", () => {
  it("List Bullet projects '* ' and List Number projects '1. '", async () => {
    const doc = await buildStyleListDoc();
    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).toContain("* Maintain ISO 27001 controls");
    expect(text).toContain("* Notify incidents without undue delay");
    expect(text).toContain("1. First escalation");
    expect(text).toContain("1. Second escalation");
  });

  it("mapper and ingest stay in sync for style-based lists", async () => {
    const buf = await (await buildStyleListDoc()).save();
    const mapper = new DocumentMapper(await DocumentObject.load(buf));
    const ingestText = _extractTextFromDoc(
      await DocumentObject.load(buf),
      false,
      false,
    ) as string;
    expect(mapper.full_text).toBe(ingestText);
  });

  it("editing a style-based list item still applies", async () => {
    const buf = await (await buildStyleListDoc()).save();
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "ISO 27001 controls",
        new_text: "ISO 27002 controls",
      } as any,
    ]);
    expect(res.edits_applied).toBe(1);
    const clean = await extractTextFromBuffer(await doc.save(), true, false);
    expect(clean).toContain("ISO 27002 controls");
  });
});

// ---------------------------------------------------------------------------
// M5 — inline image markers
// ---------------------------------------------------------------------------

describe("M5: inline images project protected markers", () => {
  it("an inline image projects ![alt](docx-image:id)", async () => {
    const doc = await buildImageDoc();
    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).toContain("![Red rectangle QA diagram](docx-image:7)");
  });

  it("mapper and ingest stay in sync for image markers", async () => {
    const buf = await (await buildImageDoc()).save();
    const mapper = new DocumentMapper(await DocumentObject.load(buf));
    const ingestText = _extractTextFromDoc(
      await DocumentObject.load(buf),
      false,
      false,
    ) as string;
    expect(mapper.full_text).toBe(ingestText);
    const markerSpan = mapper.spans.find((s) =>
      s.text.startsWith("![Red rectangle"),
    );
    expect(markerSpan).toBeTruthy();
    expect((markerSpan as any).is_image_marker).toBe(true);
  });

  it("image markers cannot be fabricated in new_text", () => {
    const errors = validate_edit_strings([
      {
        type: "modify",
        target_text: "some prose",
        new_text: "some prose ![fake](docx-image:9)",
      },
    ]);
    // The generic hyperlink shape check also fires on ![...](...) — exactly
    // like Python — so assert the image-specific error is present.
    const imageErrors = errors.filter((e) => e.includes("docx-image"));
    expect(imageErrors.length).toBe(1);
    expect(imageErrors[0]).toContain("read-only");
  });

  it("image markers are write-protected in the document", async () => {
    const doc = await buildImageDoc();
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text: "![Red rectangle QA diagram](docx-image:7)",
        new_text: "some replacement prose",
      },
    ]);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.join("\n")).toContain("image marker");
  });
});

// ---------------------------------------------------------------------------
// M6 — reserved footnotes (untyped separator ids)
// ---------------------------------------------------------------------------

describe("M6: reserved separator footnotes stay hidden", () => {
  it("untyped id=-1/0 notes are filtered from extraction", async () => {
    const doc = await buildFootnoteDoc(false);
    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).not.toContain("[^fn--1]");
    expect(text).not.toContain("[^fn-0]");
    expect(text).toContain("[^fn-1]:");
    expect(text).toContain("QA footnote about governing law");
  });
});

// ---------------------------------------------------------------------------
// M7 — every quoted defined term in a paragraph
// ---------------------------------------------------------------------------

describe("M7: every defined term in a paragraph is captured", () => {
  it("extract_terms_from_paragraph finds leading, sentence-leading and inline terms", () => {
    expect(
      extract_terms_from_paragraph(
        '"Alpha" means the first party. "Beta" means the second party. "Gamma" means the third party.',
      ),
    ).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(
      extract_terms_from_paragraph(
        'This agreement is between Acme Oy (the "Customer") and Beta Oy ' +
          '(the "Provider") for the provision of services (the "Services").',
      ),
    ).toEqual(["Customer", "Provider", "Services"]);
  });

  it("extract_all_domain_metadata sees all three sentence-leading terms", async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      '"Alpha" means the first party. "Beta" means the second party. "Gamma" means the third party.',
    );
    addParagraph(doc, "Alpha, Beta and Gamma perform the obligations.");
    const baseText = _extractTextFromDoc(doc, false, false) as string;
    const [defs] = extract_all_domain_metadata(doc, baseText);
    expect(Object.keys(defs).sort()).toEqual(["Alpha", "Beta", "Gamma"]);
  });
});

// ---------------------------------------------------------------------------
// Post-review hardening (adversarial review of the fixes themselves; mirrors
// python commits 5b23b20 + b269427)
// ---------------------------------------------------------------------------

describe("Review hardening: C1 boundary re-anchor gate", () => {
  it("a target-anchored footer prefix edit stays in the footer", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "FOOTER MARKER",
        new_text: "DRAFT FOOTER MARKER",
      } as any,
    ]);
    expect(res.edits_applied).toBe(1);

    expect(bodyXml(doc)).not.toContain("DRAFT");
    expect(footerXml(doc)).toContain("DRAFT");

    const clean = await extractTextFromBuffer(await doc.save(), true, false);
    expect(clean).toContain("DRAFT FOOTER MARKER");
  });

  it("a machine-pinned pure insertion ending in a paragraph break still re-anchors to the body", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    const footerStart = engine.mapper.full_text.indexOf("FOOTER MARKER");
    const [applied, skipped] = engine.apply_edits([
      {
        type: "modify",
        target_text: "",
        new_text: "New final body paragraph.\n\n",
        _match_start_index: footerStart,
      },
    ]);
    expect(applied).toBe(1);
    expect(skipped).toBe(0);
    expect(bodyXml(doc)).toContain("New final body paragraph.");
    expect(footerXml(doc)).not.toContain("New final body paragraph.");
  });
});

describe("Review hardening: validation uses the RAW match range across parts", () => {
  it("an insertion-shaped cross-part trim is rejected with single-part guidance", async () => {
    const doc = await buildHeaderFooterDoc();
    const engine = new RedlineEngine(doc);
    // Trims to a pure insertion of "DRAFT " at the footer's first character,
    // but the MATCH spans body + footer — inherently ambiguous.
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text: "one.\n\nFOOTER MARKER",
        new_text: "one.\n\nDRAFT FOOTER MARKER",
      },
    ]);
    expect(errors.length).toBeGreaterThan(0);
    const joined = errors.join("\n");
    expect(joined).toContain("spans a structural document-part boundary (body → footer)");
    expect(joined).toContain("within a single part");
  });

  it("an image marker untouched in shared context does not block the edit", async () => {
    const doc = await buildImageDoc();
    const engine = new RedlineEngine(doc);
    // The marker sits in the CONTEXT; the changed span is only the prose.
    const errors = engine.validate_edits([
      {
        type: "modify",
        target_text:
          "Before image.\n\n![Red rectangle QA diagram](docx-image:7)\n\nAfter image.",
        new_text:
          "Before the image.\n\n![Red rectangle QA diagram](docx-image:7)\n\nAfter image.",
      },
    ]);
    expect(errors).toEqual([]);
  });
});

describe("Review hardening: diff never emits image-marker edits", () => {
  it("an added inline image becomes a warning, not an edit", async () => {
    const orig = await createTestDocument();
    addParagraph(orig, "Before image.");
    addParagraph(orig, "After image.");
    const mod = await buildImageDoc();

    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits, warnings } = generate_structured_edits(
      o.text,
      o.structure,
      m.text,
      m.structure,
    );

    const markerRe = /!\[[^\]]*\]\(docx-image:[^)]*\)/g;
    for (const e of edits as any[]) {
      if (e.type !== "modify") continue;
      const t = ((e.target_text || "").match(markerRe) || []).sort();
      const n = ((e.new_text || "").match(markerRe) || []).sort();
      expect(t).toEqual(n);
    }
    expect(warnings.some((w) => w.toLowerCase().includes("image"))).toBe(true);
  });
});

describe("Review hardening: structured row ops are pinned and warn on ambiguity", () => {
  it("insert_row/delete_row carry their source-row offsets", async () => {
    const orig = await buildTableDoc();
    const modAdd = await buildTableDoc({ extraRow: true });
    let o = extractWithStructure(orig);
    let m = extractWithStructure(modAdd);
    let res = generate_structured_edits(o.text, o.structure, m.text, m.structure);
    const insertOp: any = (res.edits as any[]).find((e) => e.type === "insert_row");
    expect(insertOp).toBeTruthy();
    // Pinned to the anchor row's ("Premium support …") start offset.
    expect(insertOp._match_start_index).toBe(o.structure.tables[0].rows[2].start);

    const orig2 = await buildTableDoc();
    const modDrop = await buildTableDoc({ dropMiddleRow: true });
    o = extractWithStructure(orig2);
    m = extractWithStructure(modDrop);
    res = generate_structured_edits(o.text, o.structure, m.text, m.structure);
    const deleteOp: any = (res.edits as any[]).find((e) => e.type === "delete_row");
    expect(deleteOp).toBeTruthy();
    // Pinned to the deleted row's ("Standard support …") own start offset.
    expect(deleteOp._match_start_index).toBe(o.structure.tables[0].rows[1].start);
  });

  it("warns once when a row anchor's text appears more than once", async () => {
    // Two identical tables; the second gains a row — its anchor text also
    // lives in the first table.
    const build = async (extra: boolean) => {
      const doc = await createTestDocument();
      for (let t = 0; t < 2; t++) {
        const rows = extra && t === 1
          ? [["Name", "Value"], ["A", "1"], ["B", "2"]]
          : [["Name", "Value"], ["A", "1"]];
        const tbl = addTable(doc, rows.length, 2);
        for (let i = 0; i < rows.length; i++) {
          for (let j = 0; j < 2; j++) setCellText(tbl, i, j, rows[i][j]);
        }
        addParagraph(doc, `Divider ${t}.`);
      }
      return doc;
    };
    const orig = await build(false);
    const mod = await build(true);
    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits, warnings } = generate_structured_edits(
      o.text,
      o.structure,
      m.text,
      m.structure,
    );
    expect((edits as any[]).some((e) => e.type === "insert_row")).toBe(true);
    expect(
      warnings.filter((w) => w.includes("appears more than once")).length,
    ).toBe(1);
  });
});

describe("Review hardening: consecutive same-anchor row inserts", () => {
  it("two consecutive added rows apply in one sweep, in order", async () => {
    const build = async (extra: boolean) => {
      const doc = await createTestDocument();
      const rows = extra
        ? [["A", "B"], ["C", "D"], ["X1", "Y1"], ["X2", "Y2"]]
        : [["A", "B"], ["C", "D"]];
      const tbl = addTable(doc, rows.length, 2);
      for (let i = 0; i < rows.length; i++) {
        for (let j = 0; j < 2; j++) setCellText(tbl, i, j, rows[i][j]);
      }
      return doc;
    };
    const orig = await build(false);
    const mod = await build(true);
    const o = extractWithStructure(orig);
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);
    expect((edits as any[]).filter((e) => e.type === "insert_row").length).toBe(2);

    // One apply_edits sweep: the zero-width insert ranges must not collide.
    const engine = new RedlineEngine(orig);
    const [applied, skipped] = engine.apply_edits(edits as any[]);
    expect(applied).toBe(2);
    expect(skipped).toBe(0);

    const clean = await extractTextFromBuffer(await orig.save(), true, false);
    expect(clean).toContain("A | B\n--- | ---\nC | D\nX1 | Y1\nX2 | Y2");
  });
});

describe("Review hardening: numId=0 disables the style's numbering", () => {
  it("a direct numId=0 override suppresses the List Number marker", async () => {
    const doc = await buildStyleListDoc();
    // Append a ListNumber-styled paragraph carrying the ECMA-376 §17.9.15
    // "remove numbering" override.
    const xmlDoc = doc.element.ownerDocument!;
    const p = addParagraph(doc, "Not a list item");
    const pPr = xmlDoc.createElement("w:pPr");
    const pStyle = xmlDoc.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "ListNumber");
    pPr.appendChild(pStyle);
    const numPr = xmlDoc.createElement("w:numPr");
    const numId = xmlDoc.createElement("w:numId");
    numId.setAttribute("w:val", "0");
    numPr.appendChild(numId);
    pPr.appendChild(numPr);
    p.insertBefore(pPr, p.firstChild);

    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).not.toContain("1. Not a list item");
    expect(text).toContain("Not a list item");
    expect(text).toContain("1. First escalation");
  });
});

describe("Review hardening: legacy VML pictures project image markers", () => {
  async function buildVmlImageDoc(withTitle = true): Promise<DocumentObject> {
    const doc = await createTestDocument();
    addParagraph(doc, "Before legacy image.");
    const xmlDoc = doc.element.ownerDocument!;
    const p = xmlDoc.createElement("w:p");
    const r = xmlDoc.createElement("w:r");
    const pict = xmlDoc.createElement("w:pict");
    const shape = xmlDoc.createElement("v:shape");
    shape.setAttribute("id", "_x0000_i1025");
    const imagedata = xmlDoc.createElement("v:imagedata");
    imagedata.setAttribute("r:id", "rIdImg1");
    if (withTitle) imagedata.setAttribute("o:title", "Legacy logo");
    shape.appendChild(imagedata);
    pict.appendChild(shape);
    r.appendChild(pict);
    p.appendChild(r);
    doc.element.appendChild(p);
    addParagraph(doc, "After legacy image.");
    return doc;
  }

  it("w:pict with v:imagedata projects ![title](docx-image:vml)", async () => {
    const doc = await buildVmlImageDoc();
    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).toContain("![Legacy logo](docx-image:vml)");
  });

  it("mapper and ingest stay in sync for VML image markers", async () => {
    const buf = await (await buildVmlImageDoc()).save();
    const mapper = new DocumentMapper(await DocumentObject.load(buf));
    const ingestText = _extractTextFromDoc(
      await DocumentObject.load(buf),
      false,
      false,
    ) as string;
    expect(mapper.full_text).toBe(ingestText);
    expect(mapper.full_text).toContain("![Legacy logo](docx-image:vml)");
  });

  it("textpath-only watermark shapes stay out of the projection", async () => {
    const doc = await buildWatermarkDoc();
    const text = await extractTextFromBuffer(await doc.save(), false, false);
    expect(text).not.toContain("docx-image:vml");
    expect(text).not.toContain("DRAFT ACME SECRET");
  });
});

describe("Review hardening: numbered sentence-leading defined terms", () => {
  it('captures "2.2 \\"Beta\\"" after a sentence delimiter', async () => {
    expect(
      extract_terms_from_paragraph(
        '2.1 "Alpha" means the product. 2.2 "Beta" means the service.',
      ),
    ).toEqual(["Alpha", "Beta"]);

    const doc = await createTestDocument();
    addParagraph(
      doc,
      '2.1 "Alpha" means the product. 2.2 "Beta" means the service.',
    );
    addParagraph(doc, "Alpha and Beta apply to this order.");
    const baseText = _extractTextFromDoc(doc, false, false) as string;
    const [defs] = extract_all_domain_metadata(doc, baseText);
    expect(Object.keys(defs).sort()).toEqual(["Alpha", "Beta"]);
  });
});

describe("Review hardening: markup markdown-strip matching rung", () => {
  it("a **marked** target resolves against plain text", () => {
    const reports: any[] = [];
    const result = apply_edits_to_markdown(
      "The Fee applies.",
      [{ type: "modify", target_text: "**Fee**", new_text: "**Charge**" } as any],
      false,
      false,
      reports,
    );
    expect(reports[0].status).toBe("applied");
    expect(result).toContain("{--Fee--}");
  });

  it("a plain target resolves against text with mid-word markers", () => {
    const reports: any[] = [];
    const result = apply_edits_to_markdown(
      "The **Fe**e applies.",
      [{ type: "modify", target_text: "Fee", new_text: "Charge" } as any],
      false,
      false,
      reports,
    );
    expect(reports[0].status).toBe("applied");
    expect(result).toContain("{++Charge++}");
  });
});
