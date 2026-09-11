import { describe, expect, it } from "vitest";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";
import { addParagraph, createTestDocument } from "./test-utils.js";

async function fixture(withBoundary: boolean) {
  const doc = await createTestDocument();
  const p = addParagraph(doc, "Before ");
  const xml = p.ownerDocument!;
  for (const [index, text] of ["red ", "green ", "blue"].entries()) {
    if (withBoundary && index !== 0) {
      const marker = xml.createElement("w:proofErr");
      marker.setAttribute("w:type", index === 1 ? "spellStart" : "spellEnd");
      p.appendChild(marker);
    }
    const run = xml.createElement("w:r");
    const props = xml.createElement("w:rPr");
    const size = xml.createElement("w:sz");
    size.setAttribute("w:val", String(24 + index * 2));
    props.appendChild(size);
    run.appendChild(props);
    const t = xml.createElement("w:t");
    t.setAttribute("xml:space", "preserve");
    t.textContent = text;
    run.appendChild(t);
    p.appendChild(run);
  }
  addParagraph(doc, "Untouched paragraph.");
  return doc;
}

describe("multi-run deletions have unique XML revision IDs", () => {
  for (const withBoundary of [false, true]) {
    for (const replacement of ["violet", ""]) {
      it(`preserves runs and resolves the whole edit (boundary=${withBoundary}, replacement=${replacement})`, async () => {
        const doc = await fixture(withBoundary);
        const original = Buffer.from(await doc.save());
        const originalText = await extractTextFromBuffer(original, true);
        const report = new RedlineEngine(doc, "Reviewer").process_batch([
          { type: "modify", target_text: "red green blue", new_text: replacement },
        ]);
        expect(report.edits_applied).toBe(1);
        const edited = Buffer.from(await doc.save());
        const reloaded = await DocumentObject.load(edited);
        const deletions = Array.from(reloaded.element.getElementsByTagName("w:del"));
        const ids = deletions.map(el => el.getAttribute("w:id")!);
        expect(ids.length).toBe(withBoundary ? 3 : 1);
        expect(new Set(ids).size).toBe(ids.length);
        const sizes = Array.from(reloaded.element.getElementsByTagName("w:sz"))
          .slice(0, 3)
          .map(el => el.getAttribute("w:val"));
        expect(sizes).toEqual(["24", "26", "28"]);
        expect(reloaded.element.getElementsByTagName("w:proofErr").length).toBe(withBoundary ? 2 : 0);
        for (const marker of Array.from(reloaded.element.getElementsByTagName("w:proofErr"))) {
          expect((marker.parentNode as Element).tagName).toBe("w:p");
        }
        if (replacement) {
          const insertion = reloaded.element.getElementsByTagName("w:ins")[0];
          expect(insertion.getElementsByTagName("w:sz")[0].getAttribute("w:val")).toBe("28");
        }
        for (const type of ["accept", "reject"] as const) {
          const resolved = await DocumentObject.load(edited);
          new RedlineEngine(resolved, "Reviewer").process_batch(ids.map(target_id => ({ type, target_id })));
          const text = await extractTextFromBuffer(Buffer.from(await resolved.save()), true);
          const expected = type === "reject"
            ? originalText
            : originalText.replace("red green blue", replacement);
          expect(text).toBe(expected);
          expect(resolved.element.getElementsByTagName("w:del").length).toBe(0);
          expect(resolved.element.getElementsByTagName("w:ins").length).toBe(0);
        }
      });
    }
  }

  it("leaves another author's existing revisions unchanged across save/reload", async () => {
    const doc = await fixture(false);
    new RedlineEngine(doc, "Other reviewer").process_batch([
      { type: "modify", target_text: "Untouched", new_text: "Protected" },
    ]);
    const reloaded = await DocumentObject.load(Buffer.from(await doc.save()));
    const foreignParagraph = reloaded.element.getElementsByTagName("w:p")[1].toString();
    new RedlineEngine(reloaded, "Reviewer").process_batch([
      { type: "modify", target_text: "red green blue", new_text: "violet" },
    ]);
    expect(reloaded.element.getElementsByTagName("w:p")[1].toString()).toBe(foreignParagraph);
    const ids = Array.from(reloaded.element.getElementsByTagName("w:del"))
      .map(el => el.getAttribute("w:id"));
    expect(new Set(ids).size).toBe(ids.length);
  });
});
