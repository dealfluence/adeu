// FILE: node/packages/core/src/repro_qa_2026_07_23.rendering.test.ts
/**
 * Node-engine repro tests for ADEU-MCP-QA-REPORT.md (2026-07-23, black-box
 * QA of v1.29.0+4bb70f9). Covers the outline/search/table-rendering findings:
 *
 *   F4   outline/search rendering strips the leading underscore from
 *        bookmark anchors ({#_RefNNN} -> {#RefNNN}) — the markdown
 *        emphasis-pairing regexes consume the `_` — and collapses literal
 *        underscore placeholder runs ([_________] -> [___])
 *   F13  outline rendering defects: (a) a bold heuristic heading containing
 *        a line break renders with unbalanced ** markers across two outline
 *        lines; (b) a multi-hundred-word paragraph styled as a heading
 *        appears IN FULL in the outline (no truncation); (c) a heading whose
 *        visible text is just ":" (number comes from numbering) renders as a
 *        bare "## :" outline entry
 *   F21  table rendering artifacts: (a) an inserted row glues the Chg id to
 *        the last cell ("signed |Chg:1++}"); (b) filling an EMPTY cell via
 *        its {#cell:paraId} anchor emits an empty {----} deletion token in
 *        the batch-report CriticMarkup preview
 *   F22  (a) the defined-terms appendix says "used 1 times" for a
 *        single-use term; (b) the search-result Path breadcrumb leaks raw
 *        CriticMarkup ({--...--}{++...++}{>>...<<}) when the heading above
 *        the match carries a pending tracked change
 *
 * The outline/search RENDERING surface lives in the MCP server package
 * (response-builders.ts), so this file imports it cross-package; the
 * underscore/emphasis stripping itself lives in core (outline.ts).
 *
 * Every test in this file is written test-first: it fails on current main
 * and passes once the finding is fixed.
 */

import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { extract_outline, OutlineNode } from "./outline.js";
import { paginate } from "./pagination.js";
import {
  build_search_response,
  render_outline_tree,
} from "../../mcp-server/src/response-builders.js";

// ---------------------------------------------------------------------------
// fixture helpers
// ---------------------------------------------------------------------------

function setParagraphStyle(p: Element, styleId: string): void {
  const xmlDoc = p.ownerDocument!;
  const pPr = xmlDoc.createElement("w:pPr");
  const pStyle = xmlDoc.createElement("w:pStyle");
  pStyle.setAttribute("w:val", styleId);
  pPr.appendChild(pStyle);
  p.insertBefore(pPr, p.firstChild);
}

function addHeading(doc: DocumentObject, text: string, styleId = "Heading2") {
  const p = addParagraph(doc, text);
  setParagraphStyle(p, styleId);
  return p;
}

/** Real w:bookmarkStart/w:bookmarkEnd pair, as Word emits for cross-refs. */
function addBookmark(paragraph: Element, name: string, idVal: string): void {
  const xmlDoc = paragraph.ownerDocument!;
  const start = xmlDoc.createElement("w:bookmarkStart");
  start.setAttribute("w:name", name);
  start.setAttribute("w:id", idVal);
  paragraph.appendChild(start);
  const end = xmlDoc.createElement("w:bookmarkEnd");
  end.setAttribute("w:id", idVal);
  paragraph.appendChild(end);
}

function addRun(
  p: Element,
  text: string,
  opts: { bold?: boolean; brBefore?: boolean } = {},
): Element {
  const xmlDoc = p.ownerDocument!;
  const r = xmlDoc.createElement("w:r");
  if (opts.bold) {
    const rPr = xmlDoc.createElement("w:rPr");
    rPr.appendChild(xmlDoc.createElement("w:b"));
    r.appendChild(rPr);
  }
  if (opts.brBefore) r.appendChild(xmlDoc.createElement("w:br"));
  const t = xmlDoc.createElement("w:t");
  t.textContent = text;
  if (text !== text.trim()) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  p.appendChild(r);
  return r;
}

/** Markup-view body (no appendix) — what outline/search views are built on. */
function projectedBody(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, false, false) as string;
}

function outlineNodes(doc: DocumentObject): OutlineNode[] {
  const body = projectedBody(doc);
  const pag = paginate(body, "");
  return extract_outline(
    doc,
    body,
    pag.body_pages,
    pag.body_page_offsets,
    null,
  );
}

function searchMarkdown(body: string, query: string): string {
  const res = build_search_response(
    body,
    query,
    false,
    false,
    undefined,
    "qa_repro_2026_07_23.docx",
  );
  return (res.structuredContent as any).markdown as string;
}

// ---------------------------------------------------------------------------
// F4: outline/search rendering strips underscores from anchors and
//     placeholder runs
// ---------------------------------------------------------------------------

describe("QA 2026-07-23 F4: underscores survive outline/search rendering", () => {
  it("outline keeps the leading underscore of a bookmark anchor in a heading", async () => {
    const doc = await createTestDocument();
    // A heading carrying a Word cross-reference anchor AND a fill-in
    // placeholder run — the QA report's fixture shape. The emphasis-pairing
    // regex in the outline renderer pairs the anchor's `_` with one of the
    // placeholder's underscores and consumes both.
    const h = addHeading(doc, "Definitions ");
    addBookmark(h, "_Ref444615940", "7");
    addRun(h, " [_________]");
    addParagraph(doc, "Body paragraph under the heading.");

    // The full projection renders the anchor intact...
    expect(projectedBody(doc)).toContain("{#_Ref444615940}");

    // ...and the outline view must keep it intact too: an agent that copies
    // the anchor from the outline into an edit must target a REAL anchor.
    const nodes = outlineNodes(doc);
    expect(nodes.length).toBe(1);
    expect(nodes[0].text).toContain("{#_Ref444615940}");
  });

  it("search snippet keeps the leading underscore of a bookmark anchor", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "The deposit is refundable per clause four ");
    addBookmark(p, "_Ref444615940", "11");
    addRun(p, " as stated.");
    addParagraph(doc, "Another paragraph.");

    const body = projectedBody(doc);
    expect(body).toContain("{#_Ref444615940}");

    const markdown = searchMarkdown(body, "refundable");
    expect(markdown).toContain("**refundable**");
    expect(markdown).toContain("{#_Ref444615940}");
  });

  it("search snippet keeps the underscores of TWO adjacent anchors", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "The deposit is refundable per clause four ");
    addBookmark(p, "_Ref444615940", "11");
    addBookmark(p, "_Ref264019820", "12");
    addRun(p, " as stated.");
    addParagraph(doc, "Another paragraph.");

    const body = projectedBody(doc);
    expect(body).toContain("{#_Ref444615940}{#_Ref264019820}");

    const markdown = searchMarkdown(body, "refundable");
    expect(markdown).toContain("{#_Ref444615940}");
    expect(markdown).toContain("{#_Ref264019820}");
  });

  it("outline does not collapse a literal underscore placeholder run", async () => {
    const doc = await createTestDocument();
    addHeading(doc, "Signature [_________]");
    addParagraph(doc, "Body paragraph.");

    expect(projectedBody(doc)).toContain("[_________]");

    const nodes = outlineNodes(doc);
    expect(nodes.length).toBe(1);
    // On current main this renders as "Signature [___]" — the underscore run
    // is consumed by the __..__ / _.._ emphasis-stripping rules.
    expect(nodes[0].text).toContain("[_________]");
  });
});

// ---------------------------------------------------------------------------
// F13: outline rendering defects
// ---------------------------------------------------------------------------

describe("QA 2026-07-23 F13: outline rendering defects", () => {
  it("(a) every outline line carries balanced ** emphasis markers", async () => {
    const doc = await createTestDocument();
    // Heuristic bold ALL-CAPS heading whose text wraps over a w:br. The run
    // merger in build_paragraph_text hoists the newline INSIDE the bold pair
    // ("**CONFIDENTIALITY AND\nNON-DISCLOSURE**"), and the outline's
    // **-stripping regex cannot cross the newline — the rendered outline
    // splits the heading over two lines, each with a dangling `**`.
    const xmlDoc = doc.element.ownerDocument!;
    const p = xmlDoc.createElement("w:p");
    doc.element.appendChild(p);
    addRun(p, "CONFIDENTIALITY AND", { bold: true });
    addRun(p, "NON-DISCLOSURE", { bold: true, brBefore: true });
    addParagraph(doc, "Body paragraph.");

    const nodes = outlineNodes(doc);
    expect(nodes.length).toBe(1);

    const rendered = render_outline_tree(nodes, 6);
    for (const line of rendered.split("\n")) {
      const markers = (line.match(/\*\*/g) || []).length;
      expect(
        markers % 2,
        `unbalanced ** markers in outline line: ${JSON.stringify(line)}`,
      ).toBe(0);
    }
  });

  it("(b) a 300+ word paragraph styled as a heading is truncated in the outline", async () => {
    const doc = await createTestDocument();
    const words: string[] = [];
    for (let i = 0; i < 320; i++) words.push(`word${i}`);
    const longText = "The parties agree that " + words.join(" ") + ".";
    addHeading(doc, longText);
    addParagraph(doc, "Body paragraph.");

    const nodes = outlineNodes(doc);
    expect(nodes.length).toBe(1);
    // The outline is a heading MAP — a multi-hundred-word body paragraph
    // that happens to carry a heading style must not appear in full.
    expect(nodes[0].text.length).toBeLessThan(longText.length);
  });

  it("(c) outline never emits a heading entry whose text is empty or ':'", async () => {
    const doc = await createTestDocument();
    // An auto-numbered heading: the visible number comes from numbering, so
    // the projected heading text is just ":". Renders as bare "## :" today.
    addHeading(doc, ":");
    addParagraph(doc, "Body under the numbered heading.");
    addHeading(doc, "Termination");
    addParagraph(doc, "Body under the second heading.");

    const nodes = outlineNodes(doc);
    // The real heading must survive whatever the fix does...
    expect(nodes.some((n) => n.text.includes("Termination"))).toBe(true);
    // ...but no outline entry may render as empty-or-":" only.
    for (const node of nodes) {
      const text = node.text.trim();
      expect(
        text === "" || text === ":",
        `outline emitted a bare heading entry: ${JSON.stringify(node.text)}`,
      ).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// F21: table row-op / empty-cell rendering artifacts
// ---------------------------------------------------------------------------

describe("QA 2026-07-23 F21: table rendering artifacts", () => {
  it("(a) insert_row projection separates the Chg id from cell content", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 2, 2);
    setCellText(tbl, 0, 0, "Name");
    setCellText(tbl, 0, 1, "Date signed");
    setCellText(tbl, 1, 0, "Alice");
    setCellText(tbl, 1, 1, "2026-01-01");

    const midDoc = await DocumentObject.load(await doc.save());
    const engine = new RedlineEngine(midDoc, "QA");
    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "Alice",
        position: "below",
        cells: ["Bob", "signed"],
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    const projected = await extractTextFromBuffer(
      await midDoc.save(),
      false,
      false,
    );
    expect(projected).toContain("Bob");
    // Current main renders "{++ Bob | signed |Chg:1++}" — the change bubble
    // id is glued onto the last cell through a pipe, so the row reads as if
    // it had an extra cell named "Chg:1".
    expect(projected).not.toMatch(/\|Chg:\d/);
    expect(projected).not.toContain("|Chg");
  });

  it("(b) filling an empty cell emits no empty {----} deletion token", async () => {
    const doc = await createTestDocument();
    const tbl = addTable(doc, 1, 2);
    setCellText(tbl, 0, 0, "Date");
    // Leave cell (0,1) empty; give its paragraph the stable w14:paraId the
    // {#cell:...} anchor scheme keys on.
    const rows = Array.from(tbl.childNodes).filter(
      (n) => (n as Element).tagName === "w:tr",
    ) as Element[];
    const cells = Array.from(rows[0].childNodes).filter(
      (n) => (n as Element).tagName === "w:tc",
    ) as Element[];
    const emptyP = cells[1].getElementsByTagName("w:p")[0];
    emptyP.setAttribute("w14:paraId", "DEADBEEF");

    const buf = await doc.save();
    expect(await extractTextFromBuffer(buf, false, false)).toContain(
      "{#cell:DEADBEEF}",
    );

    const midDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(midDoc, "QA");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "{#cell:DEADBEEF}",
        new_text: "2026-07-23",
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    // The batch report's CriticMarkup preview: current main renders
    // "Date | {----}{++2026-07-23++}{#cell:DEADBEEF}" — a {----} EMPTY
    // deletion token, because the anchor target resolves to zero document
    // characters yet is still rendered as a deletion.
    const report = stats.edits[0] as any;
    expect(String(report.critic_markup)).toContain("{++2026-07-23++}");
    expect(String(report.critic_markup)).not.toContain("{----}");

    // And the re-read projection must never carry one either.
    const projected = await extractTextFromBuffer(
      await midDoc.save(),
      false,
      false,
    );
    expect(projected).toContain("2026-07-23");
    expect(projected).not.toContain("{----}");
  });
});

// ---------------------------------------------------------------------------
// F22: appendix grammar + search breadcrumb CriticMarkup leak
// ---------------------------------------------------------------------------

describe("QA 2026-07-23 F22: appendix grammar and breadcrumb hygiene", () => {
  it('(a) defined-terms appendix says "used 1 time" for a single-use term', async () => {
    const doc = await createTestDocument();
    addParagraph(
      doc,
      '"Agreement" means this master services agreement between the parties.',
    );
    addParagraph(doc, "Obligations under the Agreement survive termination.");

    const text = await extractTextFromBuffer(await doc.save());
    // Current main renders '- "Agreement" — used 1 times.'
    expect(text).toContain('- "Agreement" — used 1 time.');
    expect(text).not.toContain("used 1 times");
  });

  it("(b) search Path breadcrumb renders clean heading text, not raw CriticMarkup", async () => {
    const doc = await createTestDocument();
    addHeading(doc, "Definitions and Interpretation");
    addParagraph(doc, "This agreement is governed by Finnish law.");

    // A pending tracked change INSIDE the heading.
    const engine = new RedlineEngine(doc, "QA");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "Interpretation",
        new_text: "Construction",
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    const body = await extractTextFromBuffer(await doc.save(), false, false);
    const markdown = searchMarkdown(body, "governed by");

    const pathMatch = markdown.match(/\*\*Path:\*\* `([^`]*)`/);
    expect(pathMatch, "search result must carry a Path breadcrumb").toBeTruthy();
    const breadcrumb = pathMatch![1];
    // Current main leaks the heading's pending edit verbatim:
    // "Definitions and {--Interpretation--}{++Construction++}{>>[Chg:1 ...".
    expect(breadcrumb).toContain("Definitions and");
    for (const token of ["{--", "{++", "{>>", "{=="]) {
      expect(
        breadcrumb.includes(token),
        `breadcrumb leaks raw CriticMarkup token ${token}: ${JSON.stringify(breadcrumb)}`,
      ).toBe(false);
    }
  });
});
