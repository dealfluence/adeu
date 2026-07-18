/**
 * Repro tests for the 2026-07-18 black-box QA and UX assessment
 * (adeu 1.23.0+8102b64) — Node parity with
 * python/tests/test_repro_qa_report_v6.py.
 *
 * Finding index:
 *   C1  finalize_document reports "Result: CLEAN" while docProps/custom.xml
 *       (custom document properties) survives in the SAVED package. The Node
 *       engine additionally leaked ALL removed parts (including customXml/*)
 *       through pkg.unzipped: save() zips every original member, so parts
 *       dropped from pkg.parts still shipped with their original bytes.
 *       dc:identifier / dc:language / cp:version were silently retained.
 *   H1  a table-row modification followed by an insert_row anchored on the
 *       modified row (the shape `generate_structured_edits` emits, replayed
 *       without private pins — the MCP process_document_batch shape) either
 *       fails or inserts the row at the wrong position: the clean-view
 *       fallback resolves a clean-view offset, but the row lookup ran
 *       against the raw mapper.
 *   H2  a context-wrapped paragraph insertion (target "two.\n\nFinal" →
 *       new "two.\n\nAdded.\n\nFinal") is word-diffed after context
 *       trimming; dmp cross-matches punctuation between context and inserted
 *       text, stranding characters and gluing paragraphs after accept-all.
 *
 * (C2 — extract overwriting its own input — is a Python-CLI-only surface;
 * the Node packages expose no file-writing extract command.)
 */

import { describe, it, expect } from "vitest";
import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer, _extractTextFromDoc } from "./ingest.js";
import { generate_structured_edits } from "./diff.js";
import { RedlineEngine } from "./engine.js";
import { finalize_document } from "./sanitize/core.js";

const CUSTOM_PROPS_XML =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
  '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" ' +
  'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">' +
  '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="ClientSecret">' +
  "<vt:lpwstr>TOP-SECRET-ORCHID</vt:lpwstr></property>" +
  '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" name="MatterNumber">' +
  "<vt:lpwstr>MAT-998877</vt:lpwstr></property>" +
  "</Properties>";

const CUSTOM_XML_ITEM =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  "<clientData><name>Kontoso Legal Oy</name><id>SENTINEL-HETU-131052-308T</id></clientData>";

/**
 * Injects secret-bearing metadata (custom properties part, customXml part,
 * core identifier/language/version) into a saved DOCX buffer at ZIP level —
 * the same fixture shape the QA report used.
 */
function injectMetadata(buffer: Buffer): Buffer {
  const unzipped = unzipSync(new Uint8Array(buffer));

  let ct = strFromU8(unzipped["[Content_Types].xml"]);
  ct = ct.replace(
    "</Types>",
    '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>' +
      '<Override PartName="/customXml/item1.xml" ContentType="application/xml"/>' +
      "</Types>",
  );
  unzipped["[Content_Types].xml"] = strToU8(ct);

  let rels = strFromU8(unzipped["_rels/.rels"]);
  rels = rels.replace(
    "</Relationships>",
    '<Relationship Id="rIdCustomProps" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>' +
      "</Relationships>",
  );
  unzipped["_rels/.rels"] = strToU8(rels);

  let core = strFromU8(unzipped["docProps/core.xml"]);
  core = core
    .replace("<dc:creator></dc:creator>", "<dc:creator>Alice Example</dc:creator>")
    .replace("</cp:coreProperties>", "<dc:identifier>CLIENT-12345</dc:identifier><dc:language>fi-FI</dc:language><cp:version>9.9-internal</cp:version></cp:coreProperties>");
  unzipped["docProps/core.xml"] = strToU8(core);

  unzipped["docProps/custom.xml"] = strToU8(CUSTOM_PROPS_XML);
  unzipped["customXml/item1.xml"] = strToU8(CUSTOM_XML_ITEM);

  return Buffer.from(zipSync(unzipped));
}

async function buildMetadataDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Body content that is perfectly fine to share.");
  const buf = injectMetadata(await doc.save());
  return DocumentObject.load(buf);
}

function packageText(buffer: Buffer): string {
  const unzipped = unzipSync(new Uint8Array(buffer));
  return Object.values(unzipped)
    .map((data) => strFromU8(data))
    .join("\n");
}

// ---------------------------------------------------------------------------
// C1 — sanitize must remove custom properties / customXml from SAVED bytes
// ---------------------------------------------------------------------------

describe("QA v6 C1: custom document properties & retained core fields", () => {
  it("removes docProps/custom.xml from the saved package", async () => {
    const doc = await buildMetadataDoc();
    const { outBuffer, reportText } = await finalize_document(doc, {
      filename: "metadata.docx",
      sanitize_mode: "full",
    });

    expect(outBuffer).toBeDefined();
    const names = Object.keys(unzipSync(new Uint8Array(outBuffer!)));
    expect(names).not.toContain("docProps/custom.xml");
    const pkgText = packageText(outBuffer!);
    expect(pkgText).not.toContain("TOP-SECRET-ORCHID");
    expect(pkgText).not.toContain("MAT-998877");
    expect(reportText.toLowerCase()).toContain("custom");
  });

  it("removes customXml parts from the saved package (pkg.unzipped leak)", async () => {
    const doc = await buildMetadataDoc();
    const { outBuffer } = await finalize_document(doc, {
      filename: "metadata.docx",
      sanitize_mode: "full",
    });

    const names = Object.keys(unzipSync(new Uint8Array(outBuffer!)));
    expect(names.filter((n) => n.startsWith("customXml/"))).toEqual([]);
    expect(packageText(outBuffer!)).not.toContain("SENTINEL-HETU-131052-308T");
  });

  it("scrubs dc:identifier, dc:language and cp:version from core.xml", async () => {
    const doc = await buildMetadataDoc();
    const { outBuffer, reportText } = await finalize_document(doc, {
      filename: "metadata.docx",
      sanitize_mode: "full",
    });

    const unzipped = unzipSync(new Uint8Array(outBuffer!));
    const core = strFromU8(unzipped["docProps/core.xml"]);
    expect(core).not.toContain("CLIENT-12345");
    expect(core).not.toContain("fi-FI");
    expect(core).not.toContain("9.9-internal");
    // Scrubbed fields must be enumerated in the report, not silent.
    expect(reportText).toContain("CLIENT-12345");
  });

  it("keep-markup mode also removes custom properties", async () => {
    const doc = await buildMetadataDoc();
    const { outBuffer } = await finalize_document(doc, {
      filename: "metadata.docx",
      sanitize_mode: "keep-markup",
    });

    const names = Object.keys(unzipSync(new Uint8Array(outBuffer!)));
    expect(names).not.toContain("docProps/custom.xml");
    expect(packageText(outBuffer!)).not.toContain("TOP-SECRET-ORCHID");
  });

  it("sanitized output still loads and keeps body content", async () => {
    const doc = await buildMetadataDoc();
    const { outBuffer } = await finalize_document(doc, {
      filename: "metadata.docx",
      sanitize_mode: "full",
    });

    const text = await extractTextFromBuffer(outBuffer!, true);
    expect(text).toContain("perfectly fine to share");
  });
});

// ---------------------------------------------------------------------------
// H1 — row ops replayed without pins (the MCP batch shape)
// ---------------------------------------------------------------------------

const ORIG_ROWS = [
  ["Seats", "5", "€100"],
  ["Support", "1", "€500"],
];
const MOD_ROWS = [
  ["Seats", "10", "€125"],
  ["Support", "1", "Included"],
  ["Storage", "100 GB", "€50"],
];

async function buildTableDoc(rows: string[][]): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Pricing schedule below.");
  const tbl = addTable(doc, rows.length, rows[0].length);
  for (let r = 0; r < rows.length; r++) {
    for (let c = 0; c < rows[r].length; c++) setCellText(tbl, r, c, rows[r][c]);
  }
  addParagraph(doc, "Terms follow the table.");
  return doc;
}

function extractWithStructure(doc: DocumentObject): { text: string; structure: any } {
  return (_extractTextFromDoc as any)(doc, true, false, false, true);
}

/** Simulates the JSON round trip: private positional pins do not survive. */
function stripPins(edits: any[]): any[] {
  return edits.map((e) => {
    const clone = { ...e };
    delete clone._match_start_index;
    delete clone._resolved_start_idx;
    return clone;
  });
}

describe("QA v6 H1: diff row ops must replay without pins", () => {
  it("row modify + insert_row anchored on the modified row lands correctly", async () => {
    const orig = await buildTableDoc(ORIG_ROWS);
    const mod = await buildTableDoc(MOD_ROWS);
    const origBuf = await orig.save();
    const modBuf = await mod.save();

    const o = extractWithStructure(await DocumentObject.load(origBuf));
    const m = extractWithStructure(await DocumentObject.load(modBuf));
    const { edits, warnings } = generate_structured_edits(o.text, o.structure, m.text, m.structure);
    expect(warnings).toEqual([]);

    const workDoc = await DocumentObject.load(origBuf);
    const engine = new RedlineEngine(workDoc, "QA");
    const stats = engine.process_batch(stripPins(edits));
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    const wantText = await extractTextFromBuffer(modBuf, true);
    expect(finalText).toBe(wantText);
  });

  it("inserted row lands below its modified anchor, not at a stale offset", async () => {
    const orig = await buildTableDoc(ORIG_ROWS);
    const mod = await buildTableDoc(MOD_ROWS);
    const origBuf = await orig.save();

    const o = extractWithStructure(await DocumentObject.load(origBuf));
    const m = extractWithStructure(mod);
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const workDoc = await DocumentObject.load(origBuf);
    const engine = new RedlineEngine(workDoc, "QA");
    engine.process_batch(stripPins(edits));
    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);

    const support = finalText.indexOf("Support | 1 | Included");
    const storage = finalText.indexOf("Storage | 100 GB | €50");
    expect(support, finalText).toBeGreaterThanOrEqual(0);
    expect(storage, finalText).toBeGreaterThanOrEqual(0);
    expect(support, `wrong row order:\n${finalText}`).toBeLessThan(storage);
  });

  it("delete_row + modify composition replays without pins", async () => {
    const orig = await buildTableDoc([
      ["Alpha", "1", "a"],
      ["Beta", "2", "b"],
      ["Gamma", "3", "c"],
    ]);
    const mod = await buildTableDoc([
      ["Alpha", "9", "z"],
      ["Gamma", "3", "c"],
    ]);
    const origBuf = await orig.save();
    const modBuf = await mod.save();

    const o = extractWithStructure(await DocumentObject.load(origBuf));
    const m = extractWithStructure(await DocumentObject.load(modBuf));
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const workDoc = await DocumentObject.load(origBuf);
    const engine = new RedlineEngine(workDoc, "QA");
    const stats = engine.process_batch(stripPins(edits));
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    const wantText = await extractTextFromBuffer(modBuf, true);
    expect(finalText).toBe(wantText);
  });
});

// ---------------------------------------------------------------------------
// H2 — paragraph boundaries through context-wrapped insertions
// ---------------------------------------------------------------------------

async function buildParagraphDoc(): Promise<DocumentObject> {
  const doc = await createTestDocument();
  addParagraph(doc, "Intro paragraph one.");
  addParagraph(doc, "Middle paragraph two.");
  addParagraph(doc, "Final sentinel: END-OF-DOCUMENT-9f2c.");
  return doc;
}

describe("QA v6 H2: paragraph boundary preservation", () => {
  it("context-wrapped paragraph insertion keeps its separators", async () => {
    const doc = await buildParagraphDoc();
    const buf = await doc.save();

    const workDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(workDoc, "QA");
    // The self-contained JSON shape the Python CLI emits — and the shape an
    // LLM naturally writes into process_document_batch.
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "two.\n\nFinal",
        new_text: "two.\n\nAdditional sentence inserted before the final marker.\n\nFinal",
      } as any,
    ]);
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    expect(finalText).not.toContain("marker.Final");
    expect(finalText).not.toContain("two..");
    expect(finalText).toBe(
      "Intro paragraph one.\n\nMiddle paragraph two.\n\n" +
        "Additional sentence inserted before the final marker.\n\n" +
        "Final sentinel: END-OF-DOCUMENT-9f2c.",
    );
  });

  it("docx-to-docx structured diff of an inserted paragraph round-trips (pinned, in-process)", async () => {
    // Node's structured edits are an in-process shape: pure insertions ride
    // the private pins (the Python CLI additionally rewrites them via
    // make_edits_self_contained for its JSON output). The invariant here is
    // apply(A, diff(A,B)) + accept-all == B with the paragraph boundary kept.
    const orig = await buildParagraphDoc();
    const origBuf = await orig.save();

    const mod = await createTestDocument();
    addParagraph(mod, "Intro paragraph one.");
    addParagraph(mod, "Middle paragraph two.");
    addParagraph(mod, "Additional sentence inserted before the final marker.");
    addParagraph(mod, "Final sentinel: END-OF-DOCUMENT-9f2c.");
    const modBuf = await mod.save();

    const o = extractWithStructure(await DocumentObject.load(origBuf));
    const m = extractWithStructure(await DocumentObject.load(modBuf));
    const { edits } = generate_structured_edits(o.text, o.structure, m.text, m.structure);

    const workDoc = await DocumentObject.load(origBuf);
    const engine = new RedlineEngine(workDoc, "QA");
    const stats = engine.process_batch(edits as any);
    expect(stats.edits_skipped, JSON.stringify(stats.skipped_details)).toBe(0);

    engine.accept_all_revisions(true);
    const finalText = await extractTextFromBuffer(await workDoc.save(), true);
    const wantText = await extractTextFromBuffer(modBuf, true);
    expect(finalText).not.toContain("marker.Final");
    expect(finalText).toBe(wantText);
  });
});
