import { describe, it, expect, vi, afterEach } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { build_redline_comparison, ComparisonError } from "./compare.js";

// Mirrors python/tests/test_onestep_compare.py.
async function confirmation(suffix: string): Promise<Buffer> {
  const doc = await createTestDocument();
  for (const heading of ["5. Close-out and Early Termination", "5.1 Close-out Amount" + suffix]) {
    const p = addParagraph(doc, heading);
    const r = p.getElementsByTagName("w:r")[0];
    const rPr = r.ownerDocument!.createElement("w:rPr");
    rPr.appendChild(r.ownerDocument!.createElement("w:b"));
    r.insertBefore(rPr, r.firstChild);
  }
  addParagraph(doc, "The Close-out Amount shall be determined by the Determining Party.");
  addParagraph(doc, "References to 5.1 Close-out Amount include any successor provision.");
  return doc.save();
}

async function rejected(buf: Buffer): Promise<string> {
  const doc = await DocumentObject.load(buf);
  new RedlineEngine(doc).reject_all_revisions();
  return extractTextFromBuffer(await doc.save(), true);
}

describe("build_redline_comparison (one-step compare)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("writes a redline whose accepted view is the modified document and rejected view the original", async () => {
    const v1 = await confirmation("");
    const v2 = await confirmation(" — Mandatory Dealer Poll");
    const res = await build_redline_comparison(v1, v2, "Reviewer");
    expect(res.applied).toBeGreaterThan(0);
    const out = Buffer.from(res.data);
    expect(await extractTextFromBuffer(out, true)).toBe(await extractTextFromBuffer(v2, true));
    expect(await rejected(out)).toBe(await extractTextFromBuffer(v1, true));
  });

  it("writes property containers in schema order (inherited tblBorders after tblLook)", async () => {
    // compare-v1-v2's v1 lists w:tblBorders after w:tblLook in every table; ordinary saves keep that (surgical), but
    // the comparison redline is a new document. Twin: test_one_step_compare_writes_property_containers_in_schema_order.
    const bordered = async (cell: string) => {
      const d = await createTestDocument();
      addParagraph(d, "Schedule of rates");
      const xml = d.element.ownerDocument!;
      const tbl = xml.createElement("w:tbl");
      const tblPr = xml.createElement("w:tblPr");
      for (const tag of ["w:tblW", "w:tblLook", "w:tblBorders"]) tblPr.appendChild(xml.createElement(tag));
      tbl.appendChild(tblPr);
      const grid = xml.createElement("w:tblGrid");
      grid.appendChild(xml.createElement("w:gridCol"));
      tbl.appendChild(grid);
      const tr = xml.createElement("w:tr");
      const tc = xml.createElement("w:tc");
      const p = xml.createElement("w:p");
      const r = xml.createElement("w:r");
      const t = xml.createElement("w:t");
      t.textContent = cell;
      r.appendChild(t);
      p.appendChild(r);
      tc.appendChild(p);
      tr.appendChild(tc);
      tbl.appendChild(tr);
      d.element.appendChild(tbl);
      return d.save();
    };
    const tblPrKids = async (buf: Buffer) => {
      const d = await DocumentObject.load(buf);
      const tblPr = d.element.getElementsByTagName("w:tblPr")[0];
      return Array.from(tblPr.childNodes)
        .filter((n) => n.nodeType === 1)
        .map((n) => (n as Element).tagName);
    };
    const v1 = await bordered("Rate: 5.00%");
    const v2 = await bordered("Rate: 5.25%");
    expect((await tblPrKids(v1)).at(-1)).toBe("w:tblBorders");

    const out = Buffer.from((await build_redline_comparison(v1, v2, "Reviewer")).data);

    const kids = await tblPrKids(out);
    expect(kids.indexOf("w:tblBorders")).toBeLessThan(kids.indexOf("w:tblLook"));
    expect(await extractTextFromBuffer(out, true)).toBe(await extractTextFromBuffer(v2, true));
  });

  it("throws and returns nothing when any computed change is skipped", async () => {
    const v1 = await confirmation("");
    const v2 = await confirmation(" — Mandatory Dealer Poll");
    // Fault injection at the engine boundary: an apply-stage skip the diff could not foresee.
    vi.spyOn(RedlineEngine.prototype, "apply_edits").mockImplementation((edits: any[]) => [edits.length - 1, 1]);
    await expect(build_redline_comparison(v1, v2, "Reviewer")).rejects.toThrow(ComparisonError);
    await expect(build_redline_comparison(v1, v2, "Reviewer")).rejects.toThrow(
      /could not be written as tracked changes/,
    );
  });
});
