// Ports of the 2026-07-17 QA report regression tests (Python
// tests/test_repro_qa_report_v4.py) for the findings that also applied to the
// Node engine: H2 (run-boundary matching), C2 (bounded report echoes),
// H1 (faithful previews), M3/M4 (insert_row cell handling and reporting).

import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { DocumentMapper } from "./mapper.js";

function addFormattedParagraph(
  doc: any,
  segments: [string, { bold?: boolean; italic?: boolean }][],
): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  for (const [text, fmt] of segments) {
    const r = xmlDoc.createElement("w:r");
    if (fmt.bold || fmt.italic) {
      const rPr = xmlDoc.createElement("w:rPr");
      if (fmt.bold) rPr.appendChild(xmlDoc.createElement("w:b"));
      if (fmt.italic) rPr.appendChild(xmlDoc.createElement("w:i"));
      r.appendChild(rPr);
    }
    const t = xmlDoc.createElement("w:t");
    t.textContent = text;
    if (text.trim() !== text) t.setAttribute("xml:space", "preserve");
    r.appendChild(t);
    p.appendChild(r);
  }
  doc.element.appendChild(p);
  return p;
}

function cleanText(doc: any): string {
  return new DocumentMapper(doc, true).full_text;
}

async function ndaDoc() {
  const doc = await createTestDocument();
  addParagraph(
    doc,
    "This Agreement is entered into as of January 1, 2025, by Acme Corp and Beta LLC.",
  );
  addParagraph(
    doc,
    "This Agreement shall be governed by the laws of the State of California.",
  );
  addParagraph(doc, "Neither party shall solicit employees of the other party.");
  addParagraph(
    doc,
    "This Agreement shall remain in effect for a period of two (2) years.",
  );
  return doc;
}

describe("H2 — plain targets match across bold/italic run boundaries", () => {
  async function boundaryDoc() {
    const doc = await createTestDocument();
    addFormattedParagraph(doc, [
      ["The word ", {}],
      ["Al", { bold: true }],
      ["pha is the bold target.", {}],
    ]);
    addFormattedParagraph(doc, [
      ["The word ", {}],
      ["Br", { italic: true }],
      ["avo is the italic target.", {}],
    ]);
    addFormattedParagraph(doc, [
      ["The word ", {}],
      ["Ch", { bold: true }],
      ["arlie", { italic: true }],
      [" is the mixed target.", {}],
    ]);
    addParagraph(doc, "The word Hotel is the control target.");
    return doc;
  }

  it.each([
    ["Alpha", "AlphaEdited"],
    ["Bravo", "BravoEdited"],
    ["Charlie", "CharlieEdited"],
    ["Hotel", "HotelEdited"],
  ])("plain target %s matches and applies", async (target, replacement) => {
    const engine = new RedlineEngine(await boundaryDoc());
    const stats = engine.process_batch([
      { type: "modify", target_text: target, new_text: replacement } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    engine.accept_all_revisions();
    const finalText = cleanText(engine.doc);
    expect(finalText).toContain(replacement);
  });

  it("markdown-inclusive target still matches", async () => {
    const engine = new RedlineEngine(await boundaryDoc());
    const stats = engine.process_batch([
      { type: "modify", target_text: "**Al**pha", new_text: "AlphaEdited" } as any,
    ]);
    expect(stats.edits_applied).toBe(1);
  });

  it("boundary matches are still ambiguity-checked", async () => {
    const doc = await createTestDocument();
    for (let i = 0; i < 2; i++) {
      addFormattedParagraph(doc, [
        ["Prefix ", {}],
        ["Zu", { bold: true }],
        ["lu suffix.", {}],
      ]);
    }
    const engine = new RedlineEngine(doc);
    const errors = engine.validate_edits([
      { type: "modify", target_text: "Zulu", new_text: "Zebra" },
    ]);
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("Ambiguous match");
  });
});

describe("C2 — oversized edit values are never echoed unbounded", () => {
  it("giant new_text is truncated in the report but fully applied", async () => {
    const big = "X".repeat(2_000_000);
    const engine = new RedlineEngine(await ndaDoc());
    const stats = engine.process_batch([
      { type: "modify", target_text: "California", new_text: big } as any,
    ]);

    expect(stats.edits_applied).toBe(1);
    const report = stats.edits[0];
    expect(report.new_text.length).toBeLessThan(2_000);
    expect(report.new_text).toContain("chars omitted");
    expect(report.critic_markup.length).toBeLessThan(2_000);
    expect(report.clean_text.length).toBeLessThan(2_000);
    expect(JSON.stringify(stats).length).toBeLessThan(20_000);

    // Truncation is display-only: the document receives the full value.
    engine.accept_all_revisions();
    expect(cleanText(engine.doc)).toContain(big);
  });

  it("giant target_text is truncated in the not-found error", async () => {
    const big = "Y".repeat(1_000_000);
    const engine = new RedlineEngine(await ndaDoc());
    const errors = engine.validate_edits([
      { type: "modify", target_text: big, new_text: "z" },
    ]);
    expect(errors.length).toBe(1);
    expect(errors[0].length).toBeLessThan(2_000);
    expect(errors[0].toLowerCase()).toContain("not found");
  });
});

describe("H1 — previews are faithful: no scaffolding, no cross-edit bleed", () => {
  it("multi-edit batch previews are clean and localized", async () => {
    const engine = new RedlineEngine(await ndaDoc());
    const stats = engine.process_batch([
      { type: "modify", target_text: "California", new_text: "Delaware" },
      {
        type: "modify",
        target_text: "solicit employees",
        new_text: "poach employees",
      },
      {
        type: "modify",
        target_text: "two (2) years",
        new_text: "five (5) years",
      },
    ] as any[]);
    expect(stats.edits_applied).toBe(3);

    for (const report of stats.edits) {
      expect(report.critic_markup).not.toBeNull();
      // Internal scaffolding must never leak into previews.
      expect(report.critic_markup).not.toContain("[Chg:");
      expect(report.critic_markup).not.toContain("{>>");
      expect(report.critic_markup).not.toContain("<<}");
    }

    expect(stats.edits[0].critic_markup).toContain(
      "{--California--}{++Delaware++}",
    );

    // A compound change must preview the COMPLETE logical change, not just
    // its first word-diff sub-edit ("{--two--}{++five++} (2) years").
    expect(stats.edits[2].critic_markup).toContain(
      "{--two (2)--}{++five (5)++} years",
    );
    expect(stats.edits[2].clean_text).toContain("five (5) years");
    expect(stats.edits[2].clean_text).not.toContain("five (2)");
  });

  it("dry-run previews match real previews", async () => {
    const batch = () =>
      [
        { type: "modify", target_text: "California", new_text: "Delaware" },
        {
          type: "modify",
          target_text: "two (2) years",
          new_text: "five (5) years",
        },
      ] as any[];

    const dry = new RedlineEngine(await ndaDoc()).process_batch(batch(), true);
    const wet = new RedlineEngine(await ndaDoc()).process_batch(batch(), false);

    expect(dry.edits.map((r: any) => r.critic_markup)).toEqual(
      wet.edits.map((r: any) => r.critic_markup),
    );
    expect(dry.edits.map((r: any) => r.clean_text)).toEqual(
      wet.edits.map((r: any) => r.clean_text),
    );
  });
});

describe("M3/M4 — insert_row cell handling and reporting", () => {
  async function tableDoc() {
    const doc = await createTestDocument();
    addParagraph(doc, "Pricing tiers:");
    const table = addTable(doc, 2, 3);
    setCellText(table, 0, 0, "Plan");
    setCellText(table, 0, 1, "Price");
    setCellText(table, 0, 2, "Seats");
    setCellText(table, 1, 0, "Starter");
    setCellText(table, 1, 1, "$10");
    setCellText(table, 1, 2, "5");
    return doc;
  }

  it("overfilled cells are rejected at validation time", async () => {
    const engine = new RedlineEngine(await tableDoc());
    let caught: any = null;
    try {
      engine.process_batch([
        {
          type: "insert_row",
          target_text: "Starter",
          cells: ["A", "B", "C", "D", "E"],
        } as any,
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(caught.message).toContain("5 cells");
    expect(caught.message).toContain("3 column");
  });

  it("underfilled cells are padded to the table width", async () => {
    const engine = new RedlineEngine(await tableDoc());
    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "Starter",
        cells: ["OnlyTwo", "Cells"],
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);

    // The inserted row must carry exactly 3 cells (2 provided + 1 padded).
    const xml = engine.doc.element.toString();
    const insRowMatch = xml.match(/<w:tr><w:trPr><w:ins [^>]*\/><\/w:trPr>(.*?)<\/w:tr>/s);
    expect(insRowMatch).not.toBeNull();
    const cellCount = (insRowMatch![1].match(/<w:tc>/g) || []).length;
    expect(cellCount).toBe(3);
  });

  it("row ops outside a table are rejected with a specific error", async () => {
    const engine = new RedlineEngine(await tableDoc());
    let caught: any = null;
    try {
      engine.process_batch([
        {
          type: "insert_row",
          target_text: "Pricing tiers",
          cells: ["A"],
        } as any,
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect(caught.message).toContain("not inside a table row");
  });

  it("report shows the inserted cells instead of an empty new_text", async () => {
    const engine = new RedlineEngine(await tableDoc());
    const stats = engine.process_batch([
      {
        type: "insert_row",
        target_text: "Starter",
        cells: ["Pro", "$20", "10"],
      } as any,
    ]);
    expect(stats.edits[0].new_text).toBe("Pro | $20 | 10");
  });
});
