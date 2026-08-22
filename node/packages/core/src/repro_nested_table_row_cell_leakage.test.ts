// FILE: node/packages/core/src/repro_nested_table_row_cell_leakage.test.ts
/**
 * Guard against nested-table leakage in the Node table shim.
 *
 * `Table`/`Row` in src/docx/primitives.ts used to enumerate with
 * `getElementsByTagName`, which is *recursive*. For a table containing a
 * nested table (a `w:tbl` inside a `w:tc` — extremely common in RFP response
 * grids and signature blocks) that produced three separate corruptions:
 *
 *   1. the outer row absorbed every inner cell:
 *        "AfterInner | InnerA1 | InnerB1 | InnerA2 | InnerB2 | OuterB2"
 *   2. the inner rows were re-emitted as rows of the *outer* table
 *   3. every inner cell's text was therefore projected three times
 *
 * Duplicated text is worse than missing text here: `find_text`/`modify` match
 * on the projection, so a duplicated target reads as ambiguous (or patches the
 * wrong run), and the outer row's column count no longer matches its grid.
 *
 * Python (python-docx `CT_Tbl.tr_lst` / `CT_Row.tc_lst`) was always correct;
 * this was Node-only. The fix is direct-child enumeration that still descends
 * through content controls, so it must not regress the w:sdt handling that
 * repro_sdt_table_row_cell_invisibility.test.ts pins.
 *
 * Visibility only — no edit/apply semantics are exercised here.
 */

import { describe, it, expect } from "vitest";
import { parseFastXml } from "./docx/fast-xml.js";
import { createTestDocument, addParagraph, appendRawXml, loadSharedFixtureXml } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const NS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

const p = (text: string) =>
  `<w:p><w:r><w:t xml:space="preserve">${text}</w:t></w:r></w:p>`;

const tc = (inner: string) =>
  `<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>${inner}</w:tc>`;

const OUTER = loadSharedFixtureXml("nested_table_leakage.xml");

/** Outer table whose nested table is additionally wrapped in content controls. */
const OUTER_SDT = `<w:tbl ${NS}>
  <w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>
  <w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>
  <w:tr>${tc(p("OuterA1"))}${tc(p("OuterB1"))}</w:tr>
  <w:sdt>
    <w:sdtPr><w:alias w:val="NestRow"/><w:id w:val="201"/></w:sdtPr>
    <w:sdtContent>
      <w:tr>
        <w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>
          <w:tbl>
            <w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>
            <w:tblGrid><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/></w:tblGrid>
            <w:sdt>
              <w:sdtPr><w:id w:val="202"/></w:sdtPr>
              <w:sdtContent><w:tr>${tc(p("InnerA1"))}${tc(p("InnerB1"))}</w:tr></w:sdtContent>
            </w:sdt>
          </w:tbl>
          ${p("AfterInner")}
        </w:tc>
        ${tc(p("OuterB2"))}
      </w:tr>
    </w:sdtContent>
  </w:sdt>
</w:tbl>`;

async function buildDoc(tableXml: string): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(doc, "Intro.");
  appendRawXml(doc, tableXml);
  addParagraph(doc, "Outro.");
  return doc.save();
}

const CC_TOKEN_RE = /\{#\/?cc:\d+[^}]*\}/g;

/**
 * Non-blank projected lines, with content-control anchors stripped. This
 * suite's subject is whether the sdt-transparent walk STOPS at a nested
 * `w:tbl` — structural, and must read the same however much chrome CC-1 adds.
 */
function lines(text: string): string[] {
  return text
    .split("\n")
    .filter((l) => l.trim().length > 0)
    .map((l) => l.replace(CC_TOKEN_RE, ""));
}

describe("nested tables do not leak rows or cells into the outer table", () => {
  it.each([false, true])(
    "keeps the outer row at its own column count (cleanView=%s)",
    async (cleanView) => {
      const text = await extractTextFromBuffer(await buildDoc(OUTER), cleanView, false);
      const outerRow = lines(text).find((l) => l.startsWith("AfterInner"));
      expect(outerRow, `outer row missing:\n${text}`).toBeTruthy();
      for (const inner of ["InnerA1", "InnerB1", "InnerA2", "InnerB2"]) {
        expect(
          outerRow,
          `inner cell "${inner}" leaked into the outer row: ${outerRow}`,
        ).not.toContain(inner);
      }
      expect(outerRow!.split(" | ")).toHaveLength(2);
    },
  );

  it("projects each nested cell exactly once", async () => {
    const text = await extractTextFromBuffer(await buildDoc(OUTER), true, false);
    for (const token of ["InnerA1", "InnerB1", "InnerA2", "InnerB2", "AfterInner"]) {
      const count = text.split(token).length - 1;
      expect(count, `"${token}" projected ${count}x (expected 1):\n${text}`).toBe(1);
    }
  });

  it("projects the nested table in document order, inside its cell", async () => {
    const text = await extractTextFromBuffer(await buildDoc(OUTER), true, false);
    expect(lines(text)).toEqual([
      "Intro.",
      "OuterA1 | OuterB1",
      "--- | ---",
      "InnerA1 | InnerB1",
      "--- | ---",
      "InnerA2 | InnerB2",
      "AfterInner | OuterB2",
      "Outro.",
    ]);
  });

  it("emits one divider per table, sized to that table's first row", async () => {
    const text = await extractTextFromBuffer(await buildDoc(OUTER), false, false);
    // Outer table is 2 cols and inner table is 2 cols, so both dividers read
    // "--- | ---"; the count is what proves the inner rows did not become
    // outer rows (which produced a third, misplaced divider).
    expect(text.split("--- | ---").length - 1).toBe(2);
  });

  it("handles a nested table wrapped in content controls", async () => {
    const text = await extractTextFromBuffer(await buildDoc(OUTER_SDT), true, false);
    expect(lines(text)).toEqual([
      "Intro.",
      "OuterA1 | OuterB1",
      "--- | ---",
      // The single-row inner table gets its own divider after its first row.
      "InnerA1 | InnerB1",
      "--- | ---",
      "AfterInner | OuterB2",
      "Outro.",
    ]);
  });

  it.each([OUTER, OUTER_SDT])(
    "keeps ingest and the mapper synchronized (#%#)",
    async (tableXml) => {
      const buf = await buildDoc(tableXml);
      const projected = await extractTextFromBuffer(buf, true, false);
      const mapped = new DocumentMapper(await DocumentObject.load(buf), true).full_text;
      expect(mapped, "DocumentMapper drifted from the ingest projection").toBe(projected);
    },
  );

  it("keeps nested-cell text addressable via run-backed spans", async () => {
    const doc = await DocumentObject.load(await buildDoc(OUTER));
    const mapper = new DocumentMapper(doc);
    for (const target of ["InnerB2", "AfterInner"]) {
      const start = mapper.full_text.indexOf(target);
      expect(start, `"${target}" absent from the mapper projection`).toBeGreaterThanOrEqual(0);
      expect(mapper.full_text.indexOf(target, start + 1), `"${target}" duplicated`).toBe(-1);
      const end = start + target.length;
      const covering = mapper.spans.filter((s) => s.run && s.start < end && s.end > start);
      expect(covering.map((s) => s.text).join("")).toBe(target);
    }
  });
});
