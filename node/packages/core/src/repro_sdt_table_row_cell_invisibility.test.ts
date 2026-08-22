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
import { createTestDocument, addParagraph, appendRawXml, loadSharedFixtureXml } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants, findChild } from "./docx/dom.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { RedlineEngine } from "./engine.js";
import { ModifyText } from "./models.js";

const TABLE_XML = loadSharedFixtureXml("sdt_table.xml");

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
