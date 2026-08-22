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
 * Covers table SDT visibility (A0.1-A0.4). Mostly
 * visibility; the final block additionally exercises A0.3's apply half — an
 * edit inside a row-level control must resolve and keep its tracked change
 * inside the wrapper.
 */

import { describe, it, expect } from "vitest";
import { parseFastXml } from "./docx/fast-xml.js";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants, findChild } from "./docx/dom.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { RedlineEngine } from "./engine.js";
import { ModifyText } from "./models.js";

const NS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
  'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' +
  'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"';

const p = (text: string) =>
  `<w:p><w:r><w:t xml:space="preserve">${text}</w:t></w:r></w:p>`;

const tc = (text: string) =>
  `<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>${p(text)}</w:tc>`;

/**
 * The table portion of the normative standard fixture
 * (shared/fixtures/fixture-standard.md), which A0 declares
 * sufficient for A0.1-A0.4. Tags and w:id values are reproduced verbatim so
 * CC-1 can layer {#cc:N} anchors onto this same shape.
 *
 * Row 1: plain cell + a CELL-level sdt          (fixture CC:14, tag cell_role)
 * Row 2: the whole row behind a ROW-level sdt   (fixture CC:15, tag row_approver)
 * Row 3: plain row, BLOCK-level sdt in cell 2   (fixture CC:16, tag cell_notes)
 *
 * Row 4 has no counterpart in the standard fixture: A0.4 requires a
 * w15:repeatingSectionItem row nested one level inside another sdt, and the
 * fixture only carries repeating sections at block level (CC:11-13).
 */
const TABLE_XML = `<w:tbl ${NS}>
  <w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>
  <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>

  <w:tr>
    ${tc("Role")}
    <w:sdt>
      <w:sdtPr><w:tag w:val="cell_role"/><w:id w:val="201"/><w:text/></w:sdtPr>
      <w:sdtContent>${tc("Contracting Officer")}</w:sdtContent>
    </w:sdt>
  </w:tr>

  <w:sdt>
    <w:sdtPr><w:tag w:val="row_approver"/><w:id w:val="202"/></w:sdtPr>
    <w:sdtContent>
      <w:tr>${tc("Approver")}${tc("Jane Roe")}</w:tr>
    </w:sdtContent>
  </w:sdt>

  <w:tr>
    ${tc("Notes")}
    <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>
      <w:sdt>
        <w:sdtPr><w:tag w:val="cell_notes"/><w:id w:val="203"/></w:sdtPr>
        <w:sdtContent>${p("Approved without conditions.")}</w:sdtContent>
      </w:sdt>
    </w:tc>
  </w:tr>

  <w:sdt>
    <w:sdtPr>
      <w:tag w:val="deliverable_rows"/><w:id w:val="204"/>
      <w15:repeatingSection/>
    </w:sdtPr>
    <w:sdtContent>
      <w:sdt>
        <w:sdtPr><w:id w:val="205"/><w15:repeatingSectionItem/></w:sdtPr>
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

const CC_TOKEN_RE = /\{#\/?cc:\d+[^}]*\}/g;

/**
 * Drop content-control anchors. CC-1b projects `{#cc:N}` pairs around the very
 * controls these fixtures wrap. This suite's subject is whether the wrapped
 * rows and cells are VISIBLE AT ALL — a question that must read the same
 * however much chrome CC-1 adds around them. The anchors are asserted
 * separately, both below and in the CC-1 suites.
 */
function stripCc(text: string): string {
  return text.replace(CC_TOKEN_RE, "");
}

/** Projected line whose first cell is `firstCell`, anchors stripped. */
function rowLine(text: string, firstCell: string): string {
  const line = text
    .split("\n")
    .map(stripCc)
    .find((l) => l.startsWith(firstCell));
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
      expect(rowLine(text, "Notes")).toContain("Approved without conditions.");
    },
  );

  it("emits the GFM divider with the true first-row column count", async () => {
    const text = await extractTextFromBuffer(await buildSdtTableDoc(), false, false);
    expect(text).toContain("--- | ---");
  });

  it("projects every row once, in document order", async () => {
    const text = await extractTextFromBuffer(await buildSdtTableDoc(), true, false);
    const lines = text.split("\n").filter((l) => l.trim().length > 0).map(stripCc);
    expect(lines[0]).toBe("Intro paragraph.");
    expect(lines[1]).toBe("Role | Contracting Officer");
    expect(lines[2]).toBe("--- | ---");
    expect(lines[3]).toBe("Approver | Jane Roe");
    expect(lines[4]).toBe("Notes | Approved without conditions.");
    expect(lines[5]).toBe("Repeated | Item One");
    expect(lines[6]).toBe("Outro paragraph.");

    for (const token of [
      "Contracting Officer",
      "Approver",
      "Jane Roe",
      "Approved without conditions.",
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

/** Every w:tr in the tree, including sdt-wrapped ones. */
function trCount(doc: DocumentObject): number {
  return findAllDescendants((doc as any).element, "w:tr").length;
}

/** The sdt-wrapped w:tr whose cells mention `needle`. */
function wrappedRow(doc: DocumentObject, needle: string): Element | null {
  for (const sdt of findAllDescendants((doc as any).element, "w:sdt")) {
    const content = findChild(sdt, "w:sdtContent");
    const tr = content && findChild(content, "w:tr");
    if (tr && (tr.textContent ?? "").includes(needle)) return tr;
  }
  return null;
}

describe("edits inside a row-level content control (A0.3)", () => {
  it("applies as a tracked change that stays inside the wrapper", async () => {
    const before = await DocumentObject.load(await buildSdtTableDoc());
    const rowsBefore = trCount(before);

    const doc = await DocumentObject.load(await buildSdtTableDoc());
    const engine = new RedlineEngine(doc, "A0 Reviewer");
    const stats: any = engine.process_batch([
      { type: "modify", target_text: "Jane Roe", new_text: "John Roe" } as ModifyText,
    ]);
    expect(stats.edits_applied, `edit did not resolve: ${JSON.stringify(stats)}`).toBe(1);
    expect(stats.edits_skipped).toBe(0);

    const out = await doc.save();
    const reloaded = await DocumentObject.load(out);

    const row = wrappedRow(reloaded, "Approver");
    expect(row, "the row-level content control no longer wraps a w:tr").toBeTruthy();

    // Token-level diff: "Jane Roe" -> "John Roe" shares " Roe", so only the
    // differing token is redlined.
    const ins = findAllDescendants(row!, "w:ins")
      .flatMap((n) => findAllDescendants(n, "w:t"))
      .map((n) => n.textContent ?? "")
      .join("");
    const del = findAllDescendants(row!, "w:del")
      .flatMap((n) => findAllDescendants(n, "w:delText"))
      .map((n) => n.textContent ?? "")
      .join("");
    expect(ins, `insertion did not land inside the control (got "${ins}")`).toContain("John");
    expect(del, `deletion did not land inside the control (got "${del}")`).toContain("Jane");

    expect(trCount(reloaded), "table row count changed").toBe(rowsBefore);

    expect(await extractTextFromBuffer(out, false, false)).toContain("{--Jane--}{++John++}");
    expect(rowLine(await extractTextFromBuffer(out, true, false), "Approver")).toBe(
      "Approver | John Roe",
    );
  });

  it("keeps the control in place when the revision is accepted", async () => {
    const rowsBefore = trCount(await DocumentObject.load(await buildSdtTableDoc()));

    const doc = await DocumentObject.load(await buildSdtTableDoc());
    const engine = new RedlineEngine(doc, "A0 Reviewer");
    engine.process_batch([
      { type: "modify", target_text: "Jane Roe", new_text: "John Roe" } as ModifyText,
    ]);

    const accepted = await DocumentObject.load(await doc.save());
    const acceptEngine = new RedlineEngine(accepted, "A0 Reviewer");
    (acceptEngine as any).accept_all_revisions();
    const finalBuf = await accepted.save();
    const finalDoc = await DocumentObject.load(finalBuf);

    expect(wrappedRow(finalDoc, "Approver"), "accept dissolved the content control").toBeTruthy();
    expect(trCount(finalDoc), "accept changed the table row count").toBe(rowsBefore);

    const text = await extractTextFromBuffer(finalBuf, true, false);
    expect(rowLine(text, "Approver")).toBe("Approver | John Roe");
    expect(text).not.toContain("Jane Roe");
  });

  // CC-1b — the same controls now carry anchors. Asserted here, next to the
  // visibility guarantees, so a future change cannot restore visibility while
  // silently dropping the anchors (or vice versa).
  it("row and cell controls carry anchors", async () => {
    const data = await buildSdtTableDoc();
    const text = await extractTextFromBuffer(data, false, false);
    expect(text, "cell-level control lost its inline anchors").toContain(
      "Role | {#cc:1}Contracting Officer{#/cc:1}",
    );
    expect(text, "row-level control must bracket the whole row line").toContain(
      "{#cc:2}Approver | Jane Roe{#/cc:2}",
    );
    // A block-level control inside a cell anchors INLINE, not on its own
    // lines — token lines would break the "|" row grammar.
    expect(text).toContain("Notes | {#cc:3}Approved without conditions.{#/cc:3}");
    expect(new DocumentMapper(await DocumentObject.load(data), false).full_text).toBe(text);
  });
});
