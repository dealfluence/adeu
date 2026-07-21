import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph, addTable, setCellText } from "./test-utils.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { extract_outline } from "./outline.js";
import { paginate } from "./pagination.js";
import { finalize_document } from "./sanitize/core.js";
import { findChild, findAllDescendants, QN_W_R, QN_W_RPR, QN_W_B, QN_W_VAL, QN_W_T } from "./utils/docx.js";

describe("Adeu MCP QA Report - Issue 1: Markdown headings in new_text", () => {
  it("TC 1.1: single modify to ### heading changes style and is visible in outline", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a normal paragraph.");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "This is a normal paragraph.",
        new_text: "### Heading Only Test",
      },
    ]);

    expect(res.edits_applied).toBe(1);

    // Re-extract outline
    const extract_res = _extractTextFromDoc(doc, false, false, true) as {
      text: string;
      paragraph_offsets: Map<any, [number, number]>;
    };
    const pages = paginate(extract_res.text, "");
    const nodes = extract_outline(
      doc,
      extract_res.text,
      pages.body_pages,
      pages.body_page_offsets,
      extract_res.paragraph_offsets as any,
    );

    // The heading must be present in the outline
    const testNode = nodes.find((n) => n.text.includes("Heading Only Test"));
    expect(testNode).toBeDefined();
    expect(testNode!.level).toBe(3);
  });

  it("TC 1.2: multi-paragraph insert with heading does not leak ### prefix to other paragraphs", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Replace me.");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "Replace me.",
        new_text: "### Title\n\nBody with **bold** and _italic_.\n\nSecond paragraph.",
      },
    ]);

    expect(res.edits_applied).toBe(1);

    const buf = await doc.save();
    const cleanText = await extractTextFromBuffer(buf, true);

    // Subsequent paragraphs must not have literal "###" prepended
    expect(cleanText).toContain("Body with **bold** and _italic_.");
    expect(cleanText).toContain("Second paragraph.");
    expect(cleanText).not.toContain("### Body with");
    expect(cleanText).not.toContain("### Second");
  });
});

describe("Adeu MCP QA Report - Issue 2: Bold/large-font table cells misclassified as headings", () => {
  it("TC 2.1: bold table cell with uppercase/numeric text is NOT classified as a heading", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 1);
    setCellText(tbl, 0, 0, "$ 88,136 $ 72,361 $ 72,738");

    // Make the run inside the cell bold
    const p = tbl.getElementsByTagName("w:p")[0];
    const r = p.getElementsByTagName("w:r")[0];
    const xmlDoc = doc.element.ownerDocument!;
    const rPr = xmlDoc.createElement("w:rPr");
    const b = xmlDoc.createElement("w:b");
    rPr.appendChild(b);
    r.insertBefore(rPr, r.firstChild);

    // Re-extract outline
    const extract_res = _extractTextFromDoc(doc, false, false, true) as {
      text: string;
      paragraph_offsets: Map<any, [number, number]>;
    };
    const pages = paginate(extract_res.text, "");
    const nodes = extract_outline(
      doc,
      extract_res.text,
      pages.body_pages,
      pages.body_page_offsets,
      extract_res.paragraph_offsets as any,
    );

    // The outline should be empty or should NOT contain the table cell text as a heading
    const fakeHeading = nodes.find((n) => n.text.includes("$ 88,136"));
    expect(fakeHeading).toBeUndefined();
  });
});

describe("Adeu MCP QA Report - Issue 3: insert_row/delete_row with anchor-only target", () => {
  it("TC 3.1: insert_row succeeds when targeted with a {#cell:paraId} anchor alone", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "A1");
    setCellText(tbl, 0, 1, "A2");

    // Add w14:paraId to the paragraph of cell 0 to act as our stable anchor
    const rows = Array.from(tbl.childNodes).filter((n) => (n as Element).tagName === "w:tr") as Element[];
    const cells = Array.from(rows[0].childNodes).filter((n) => (n as Element).tagName === "w:tc") as Element[];
    const p = cells[0].getElementsByTagName("w:p")[0];
    p.setAttribute("w14:paraId", "DEADBEEF");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "insert_row",
        target_text: "{#cell:DEADBEEF}",
        cells: ["B1", "B2"],
        position: "below",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);
  });

  it("TC 3.2: delete_row succeeds when targeted with a {#cell:paraId} anchor alone", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "A1");
    setCellText(tbl, 0, 1, "A2");
    setCellText(tbl, 1, 0, "B1");
    setCellText(tbl, 1, 1, "B2");

    // Add w14:paraId to the paragraph of cell (0,0) to act as our stable anchor
    const rows = Array.from(tbl.childNodes).filter((n) => (n as Element).tagName === "w:tr") as Element[];
    const cells0 = Array.from(rows[0].childNodes).filter((n) => (n as Element).tagName === "w:tc") as Element[];
    const p = cells0[0].getElementsByTagName("w:p")[0];
    p.setAttribute("w14:paraId", "DEADBEEF");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "delete_row",
        target_text: "{#cell:DEADBEEF}",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);
  });
});

describe("Adeu MCP QA Report - Issue 5: Empty-cell fill spacing", () => {
  it("TC 5.1: modifying cell anchor next to a label preserves or inserts a separator space", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 1);
    const rows = Array.from(tbl.childNodes).filter((n) => (n as Element).tagName === "w:tr") as Element[];
    const cells = Array.from(rows[0].childNodes).filter((n) => (n as Element).tagName === "w:tc") as Element[];
    const tc = cells[0];

    // Clear and construct a paragraph with text "Nimi" and paraId "DEADBEEF"
    while (tc.firstChild) tc.removeChild(tc.firstChild);
    const xmlDoc = doc.element.ownerDocument!;
    const p = xmlDoc.createElement("w:p");
    p.setAttribute("w14:paraId", "DEADBEEF");
    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "Nimi";
    r.appendChild(t);
    p.appendChild(r);
    tc.appendChild(p);

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "{#cell:DEADBEEF}",
        new_text: "Testi Testinen",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);

    (engine as any).accept_all_revisions();
    const cleanText = await extractTextFromBuffer(await doc.save(), true);
    // It should have some separator, e.g. "Nimi Testi Testinen" instead of "NimiTesti Testinen"
    expect(cleanText).not.toContain("NimiTesti Testinen");
  });
});

describe("Adeu MCP QA Report - Issue 7: finalize_document keep-markup comments section label", () => {
  it("TC 7.1: keep-markup with open comments does NOT label them as COMMENTS (stripped)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a normal paragraph.");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "This is a normal paragraph.",
        new_text: "This is a normal paragraph.",
        comment: "This is a comment",
      } as any,
    ]);
    expect(res.edits_applied).toBe(1);

    const finalizeRes = await finalize_document(doc, {
      filename: "test.docx",
      sanitize_mode: "keep-markup",
    });

    const report = finalizeRes.reportText;

    // The report must NOT contain the contradictory "COMMENTS (stripped)" header
    // when comments are actually kept (0 comments removed, 1 comment kept).
    expect(report).not.toContain("COMMENTS (stripped)");
  });
});
