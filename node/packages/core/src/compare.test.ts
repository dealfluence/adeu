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
