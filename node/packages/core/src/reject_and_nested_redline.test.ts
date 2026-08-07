/**
 * Canonical track-change semantics for editing inside another author's pending
 * insertion, plus reject_all_revisions round-trips. Mirrors the Python suite
 * python/tests/test_reject_and_nested_redline.py (Python is canonical).
 *
 * When an author edits/deletes text inside another author's still-pending
 * <w:ins>, the deletion is NESTED inside that <w:ins> and the replacement is a
 * SIBLING after it (splitting the insertion if the edit is mid-insertion);
 * <w:ins> is never nested in <w:ins>. This preserves authorship and makes
 * reject-all revert the contingent text to nothing rather than promoting an
 * unaccepted proposal to committed body text.
 */
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";

function foreignInsDoc(doc: any) {
  const xmlDoc = doc.element.ownerDocument!;
  const p = addParagraph(doc, "The party shall provide ");
  const ins = xmlDoc.createElement("w:ins");
  ins.setAttribute("w:id", "201");
  ins.setAttribute("w:author", "Supplier's Counsel");
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.textContent = "written notice";
  r.appendChild(t);
  ins.appendChild(r);
  p.appendChild(ins);
  const r2 = xmlDoc.createElement("w:r");
  const t2 = xmlDoc.createElement("w:t");
  t2.textContent = " within 30 days.";
  r2.appendChild(t2);
  p.appendChild(r2);
  return p;
}

async function cleanText(doc: any): Promise<string> {
  return await extractTextFromBuffer(await doc.save());
}

describe("Canonical nesting + reject round-trips", () => {
  it("modify inside a foreign insertion nests del and emits a sibling ins (no ins-in-ins)", async () => {
    const doc = await createTestDocument();
    const p = foreignInsDoc(doc);
    const engine = new RedlineEngine(doc, "Reviewer AI");
    const res = engine.process_batch(
      [{ type: "modify", target_text: "written notice", new_text: "email notification" }] as any);
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    // del nested inside Supplier's <w:ins>; no <w:ins> nested in <w:ins>.
    const insNodes = Array.from(p.getElementsByTagName("w:ins"));
    const supplierIns = insNodes.find(
      (i) => i.getAttribute("w:author") === "Supplier's Counsel",
    )!;
    expect(supplierIns).toBeTruthy();
    expect(supplierIns.getElementsByTagName("w:del").length).toBeGreaterThan(0);
    for (const i of insNodes) {
      expect(i.getElementsByTagName("w:ins").length).toBe(0);
    }
    // Reviewer's replacement insertion exists as a separate node.
    expect(
      insNodes.some(
        (i) =>
          i.getAttribute("w:author") === "Reviewer AI" &&
          (i.textContent || "").includes("email notification"),
      ),
    ).toBe(true);
  });

  it("accept-all and reject-all round-trip correctly for a foreign-ins modify", async () => {
    const accDoc = await createTestDocument();
    foreignInsDoc(accDoc);
    const ae = new RedlineEngine(accDoc, "Reviewer AI");
    ae.process_batch(
      [{ type: "modify", target_text: "written notice", new_text: "email notification" }] as any);
    ae.accept_all_revisions();
    expect(await cleanText(accDoc)).toContain(
      "The party shall provide email notification within 30 days.",
    );

    const rejDoc = await createTestDocument();
    foreignInsDoc(rejDoc);
    const re = new RedlineEngine(rejDoc, "Reviewer AI");
    re.process_batch(
      [{ type: "modify", target_text: "written notice", new_text: "email notification" }] as any);
    re.reject_all_revisions();
    const rejected = await cleanText(rejDoc);
    // Reject reverts to the true baseline: the pending insertion vanishes.
    expect(rejected).not.toContain("written notice");
    expect(rejected).not.toContain("email notification");
    expect(rejected).toContain("The party shall provide  within 30 days.");
  });

  it("insertion inside a foreign ins splits (no ins-in-ins) and round-trips", async () => {
    const doc = await createTestDocument();
    const p = foreignInsDoc(doc);
    const e = new RedlineEngine(doc, "Reviewer AI");
    // A modify that trims to a pure INSERTION inside the foreign <w:ins>.
    const res = e.process_batch(
      [{ type: "modify", target_text: "written notice", new_text: "written notice please" }] as any);
    expect(res.edits_applied).toBe(1);
    for (const i of Array.from(p.getElementsByTagName("w:ins"))) {
      expect(i.getElementsByTagName("w:ins").length).toBe(0);
    }

    const ad = await createTestDocument();
    foreignInsDoc(ad);
    const ae = new RedlineEngine(ad, "Reviewer AI");
    ae.process_batch(
      [{ type: "modify", target_text: "written notice", new_text: "written notice please" }] as any);
    ae.accept_all_revisions();
    expect(await cleanText(ad)).toContain(
      "The party shall provide written notice please within 30 days.",
    );
  });

  it("reject-all restores the original for an own-author edit", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The cat sat.");
    const e = new RedlineEngine(doc, "Z");
    e.process_batch([{ type: "modify", target_text: "cat", new_text: "dog" }] as any);
    e.reject_all_revisions();
    expect(await cleanText(doc)).toContain("The cat sat.");
  });
});
