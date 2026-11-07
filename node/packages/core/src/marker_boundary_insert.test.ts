import { describe, it, expect, vi, afterEach } from "vitest";
import { createTestDocument } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";

// Twin of python/tests/test_marker_boundary_insert.py. A PLAIN target fuzzy-matched onto styled text makes dmp pair
// the inserted words with the adjacent (zero-width, virtual) `**` marker: `"**"` -> `", as supplemented ..."`. This
// engine failed the whole batch on it ("Failed to apply edit targeting: '**...'").

async function doc(before: string, bold: string, after: string): Promise<Buffer> {
  const d = await createTestDocument();
  const xml = d.element.ownerDocument!;
  const p = xml.createElement("w:p");
  for (const [text, isBold] of [
    [before, false],
    [bold, true],
    [after, false],
  ] as const) {
    const r = xml.createElement("w:r");
    if (isBold) {
      const rPr = xml.createElement("w:rPr");
      rPr.appendChild(xml.createElement("w:b"));
      r.appendChild(rPr);
    }
    const t = xml.createElement("w:t");
    t.setAttribute("xml:space", "preserve");
    t.textContent = text;
    r.appendChild(t);
    p.appendChild(r);
  }
  d.element.appendChild(p);
  return d.save();
}

async function apply(src: Buffer, target_text: string, new_text: string) {
  const d = await DocumentObject.load(src);
  const eng = new RedlineEngine(d, "Reviewer");
  const stats: any = eng.process_batch([{ type: "modify", target_text, new_text }]);
  return { stats, out: await d.save() };
}

// Formatting of the inserted words follows the engine's existing style-source rules; these tests pin that the words
// are written at the right place, not which run they inherit from.
const plain = async (b: Buffer) => (await extractTextFromBuffer(b, true)).replaceAll("**", "");

async function rejected(b: Buffer): Promise<string> {
  const d = await DocumentObject.load(b);
  new RedlineEngine(d).reject_all_revisions();
  return extractTextFromBuffer(await d.save(), true);
}

describe("insertions beside a styled run from a plain target", () => {
  afterEach(() => vi.restoreAllMocks());

  it("appends after a bold run before trailing punctuation", async () => {
    const src = await doc("The amount shall be determined in accordance with ", "Section 6(e) of the Agreement", ".");
    const { stats, out } = await apply(
      src,
      "in accordance with Section 6(e) of the Agreement.",
      "in accordance with Section 6(e) of the Agreement, as supplemented by the provisions of this Section 5.1.",
    );
    expect([stats.edits_applied, stats.edits_skipped]).toEqual([1, 0]);
    expect(await plain(out)).toBe(
      "The amount shall be determined in accordance with " +
        "Section 6(e) of the Agreement, as supplemented by the provisions of this Section 5.1.",
    );
    expect(await rejected(out)).toBe(await extractTextFromBuffer(src, true));
  });

  it("inserts before a bold run", async () => {
    const src = await doc("Invoices are due ", "Net 90", " days after receipt.");
    const { stats, out } = await apply(src, "due Net 90 days", "due within Net 90 days");
    expect([stats.edits_applied, stats.edits_skipped]).toEqual([1, 0]);
    expect(await plain(out)).toBe("Invoices are due within Net 90 days after receipt.");
    expect(await rejected(out)).toBe(await extractTextFromBuffer(src, true));
  });

  it("names the caller's target, not an internal fragment, when a sub-edit fails", async () => {
    const src = await doc("The amount shall be determined in accordance with ", "Section 6(e) of the Agreement", ".");
    const d = await DocumentObject.load(src);
    const eng = new RedlineEngine(d, "Reviewer");
    // Fault injection: any apply-stage failure of a resolved sub-edit.
    vi.spyOn(RedlineEngine.prototype as any, "_apply_single_edit_indexed").mockReturnValue(false);
    const target = "in accordance with Section 6(e) of the Agreement.";
    const [, skipped] = eng.apply_edits([
      { type: "modify", target_text: target, new_text: target.slice(0, -1) + ", as amended." } as any,
    ]);
    expect(skipped).toBe(1);
    expect(eng.skipped_details.some((m) => m.includes(target.slice(0, 40)))).toBe(true);
    expect(eng.skipped_details.some((m) => m.includes("'**"))).toBe(false);
  });
});
