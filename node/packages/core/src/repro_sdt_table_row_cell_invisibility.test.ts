// FILE: node/packages/core/src/repro_sdt_table_row_cell_invisibility.test.ts
/**
 * Guard for table rows/cells wrapped in structured document tags (content
 * controls).
 *
 * Word emits these whenever a template uses content controls:
 *
 *     <w:tbl><w:sdt><w:sdtContent><w:tr>...        (row-level control)
 *     <w:tr><w:sdt><w:sdtContent><w:tc>...         (cell-level control)
 *
 * and, for repeating sections, an extra nesting level
 * (w15:repeatingSection > w15:repeatingSectionItem > w:tr).
 *
 * The Node engine has always traversed all three because Table/Row in
 * src/docx/primitives.ts enumerate with getElementsByTagName. That behaviour
 * was accidental rather than asserted, so nothing stopped a future
 * "tighten the shim to direct children" refactor from silently deleting
 * content. The Python engine regressed on exactly this shape and had to be
 * repaired (python/tests/test_repro_sdt_table_row_cell_invisibility.py);
 * this file pins the Node side so the two engines cannot drift again.
 *
 * Real-world blast radius: the FedRAMP SSP Moderate rev4 template carries
 * 371 cell-level SDTs.
 *
 * Visibility only — no edit/apply semantics are exercised here.
 */

import { describe, it, expect } from "vitest";
import { parseFastXml } from "./docx/fast-xml.js";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const NS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

const p = (text: string) =>
  `<w:p><w:r><w:t xml:space="preserve">${text}</w:t></w:r></w:p>`;

const tc = (text: string) =>
  `<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>${p(text)}</w:tc>`;

/**
 * Row 1: plain cell + a CELL-level sdt.
 * Row 2: the whole row behind a ROW-level sdt.
 * Row 3: a plain row whose second cell holds a BLOCK-level sdt around a w:p.
 * Row 4: a row behind two nested sdt levels (FedRAMP repeating-section shape).
 */
const TABLE_XML = `<w:tbl ${NS}>
  <w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>
  <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>

  <w:tr>
    ${tc("Role")}
    <w:sdt>
      <w:sdtPr><w:alias w:val="OfficerCell"/><w:id w:val="101"/></w:sdtPr>
      <w:sdtContent>${tc("Contracting Officer")}</w:sdtContent>
    </w:sdt>
  </w:tr>

  <w:sdt>
    <w:sdtPr><w:alias w:val="ApproverRow"/><w:id w:val="102"/></w:sdtPr>
    <w:sdtContent>
      <w:tr>${tc("Approver")}${tc("Jane Roe")}</w:tr>
    </w:sdtContent>
  </w:sdt>

  <w:tr>
    ${tc("Notes")}
    <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>
      <w:sdt>
        <w:sdtPr><w:alias w:val="NoteBlock"/><w:id w:val="103"/></w:sdtPr>
        <w:sdtContent>${p("Block Level Note")}</w:sdtContent>
      </w:sdt>
    </w:tc>
  </w:tr>

  <w:sdt>
    <w:sdtPr>
      <w:alias w:val="RepeatSection"/><w:id w:val="104"/>
      <w15:repeatingSection/>
    </w:sdtPr>
    <w:sdtContent>
      <w:sdt>
        <w:sdtPr><w:id w:val="105"/><w15:repeatingSectionItem/></w:sdtPr>
        <w:sdtContent>
          <w:tr>${tc("Repeated")}${tc("Item One")}</w:tr>
        </w:sdtContent>
      </w:sdt>
    </w:sdtContent>
  </w:sdt>
</w:tbl>`;

/**
 * Appends a raw XML fragment to the body.
 *
 * Built by re-creating nodes through the target document's own factory rather
 * than importNode/adoptNode: the engine's DOM is the fast-xml shim
 * (src/docx/fast-xml.ts), which implements neither. Only createElement,
 * setAttribute and createTextNode are used, so this works on any of the DOM
 * implementations the engine has shipped.
 */
function appendRawXml(doc: DocumentObject, xml: string): void {
  const target = doc.element.ownerDocument!;
  const parsed = parseFastXml(xml);

  const importNode = (src: any): any => {
    if (src.nodeType === 3 || src.nodeType === 4) {
      return target.createTextNode(src.data ?? src.nodeValue ?? "");
    }
    const el = target.createElement(src.tagName);
    const attrs = src.attributes;
    if (attrs) {
      for (let i = 0; i < attrs.length; i++) {
        const a = attrs[i] ?? attrs.item?.(i);
        if (a) el.setAttribute(a.name ?? a.nodeName, a.value ?? a.nodeValue);
      }
    }
    const kids = src.childNodes ?? [];
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      if (k.nodeType === 1 || k.nodeType === 3 || k.nodeType === 4) {
        el.appendChild(importNode(k));
      }
    }
    return el;
  };

  doc.element.appendChild(importNode(parsed.documentElement));
}

async function buildSdtTableDoc(): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(doc, "Intro paragraph.");
  appendRawXml(doc, TABLE_XML);
  addParagraph(doc, "Outro paragraph.");
  return doc.save();
}

/** Projected line whose first cell is `firstCell`. */
function rowLine(text: string, firstCell: string): string {
  const line = text.split("\n").find((l) => l.startsWith(firstCell));
  expect(line, `no projected row starting with "${firstCell}" in:\n${text}`).toBeTruthy();
  return line!;
}

describe("SDT-wrapped table rows and cells stay visible", () => {
  it.each([false, true])(
    "projects a cell-level sdt with its column separator (cleanView=%s)",
    async (cleanView) => {
      const text = await extractTextFromBuffer(await buildSdtTableDoc(), cleanView, false);
      const line = rowLine(text, "Role");
      expect(line, `row 1 lost a cell (column misalignment): ${line}`).toContain(" | ");
      expect(line).toContain("Contracting Officer");
    },
  );

  it.each([false, true])(
    "projects a row-level sdt row (cleanView=%s)",
    async (cleanView) => {
      const text = await extractTextFromBuffer(await buildSdtTableDoc(), cleanView, false);
      expect(rowLine(text, "Approver")).toContain("Jane Roe");
    },
  );

  it.each([false, true])(
    "projects a nested repeatingSectionItem row (cleanView=%s)",
    async (cleanView) => {
      const text = await extractTextFromBuffer(await buildSdtTableDoc(), cleanView, false);
      expect(rowLine(text, "Repeated")).toContain("Item One");
    },
  );

  it.each([false, true])(
    "projects a block-level sdt inside a cell (cleanView=%s)",
    async (cleanView) => {
      // A w:sdt wrapping a w:p rather than a w:tr/w:tc. iter_block_items used
      // to accept only direct w:p / w:tbl children and dropped this outright.
      const text = await extractTextFromBuffer(await buildSdtTableDoc(), cleanView, false);
      expect(rowLine(text, "Notes")).toContain("Block Level Note");
    },
  );

  it("emits the GFM divider with the true first-row column count", async () => {
    const text = await extractTextFromBuffer(await buildSdtTableDoc(), false, false);
    expect(text).toContain("--- | ---");
  });

  it("projects every row once, in document order", async () => {
    const text = await extractTextFromBuffer(await buildSdtTableDoc(), true, false);
    const lines = text.split("\n").filter((l) => l.trim().length > 0);
    expect(lines[0]).toBe("Intro paragraph.");
    expect(lines[1]).toBe("Role | Contracting Officer");
    expect(lines[2]).toBe("--- | ---");
    expect(lines[3]).toBe("Approver | Jane Roe");
    expect(lines[4]).toBe("Notes | Block Level Note");
    expect(lines[5]).toBe("Repeated | Item One");
    expect(lines[6]).toBe("Outro paragraph.");

    for (const token of [
      "Contracting Officer",
      "Approver",
      "Jane Roe",
      "Block Level Note",
      "Repeated",
      "Item One",
    ]) {
      const count = text.split(token).length - 1;
      expect(count, `"${token}" projected ${count} times:\n${text}`).toBe(1);
    }
  });

  it.each([false, true])(
    "keeps ingest and the mapper synchronized (cleanView=%s)",
    async (cleanView) => {
      const buf = await buildSdtTableDoc();
      const projected = await extractTextFromBuffer(buf, cleanView, false);
      const doc = await DocumentObject.load(buf);
      const mapped = new DocumentMapper(doc, cleanView).full_text;
      expect(mapped, "DocumentMapper drifted from the ingest projection").toBe(projected);
    },
  );

  it.each(["Contracting Officer", "Jane Roe", "Item One"])(
    "backs sdt-wrapped text with addressable virtual-text spans (%s)",
    async (target) => {
      const doc = await DocumentObject.load(await buildSdtTableDoc());
      const mapper = new DocumentMapper(doc);
      const start = mapper.full_text.indexOf(target);
      expect(start).toBeGreaterThanOrEqual(0);
      const end = start + target.length;
      const covering = mapper.spans.filter((s) => s.run && s.start < end && s.end > start);
      expect(covering.length, `no run-backed span covers "${target}"`).toBeGreaterThan(0);
      expect(covering.map((s) => s.text).join("")).toBe(target);
    },
  );
});
