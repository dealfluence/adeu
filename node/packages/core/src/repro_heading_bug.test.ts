import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine } from "./engine.js";

describe("QA Heading Bug Repro Verification", () => {
  it("TC-1: # prefixed target, no duplicate — Node fails", async () => {
    const doc = await createTestDocument();
    
    // Add Heading 1 paragraph
    const p1 = doc.element.ownerDocument!.createElement('w:p');
    const p1Pr = doc.element.ownerDocument!.createElement('w:pPr');
    const p1Style = doc.element.ownerDocument!.createElement('w:pStyle');
    p1Style.setAttribute('w:val', 'Heading1');
    p1Pr.appendChild(p1Style);
    p1.appendChild(p1Pr);
    const r1 = doc.element.ownerDocument!.createElement('w:r');
    const t1 = doc.element.ownerDocument!.createElement('w:t');
    t1.textContent = '2. Confidentiality';
    r1.appendChild(t1);
    p1.appendChild(r1);
    doc.element.appendChild(p1);

    addParagraph(doc, "As defined in Section 1, the Recipient shall...");

    const engine = new RedlineEngine(doc);
    
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "# 2. Confidentiality",
        new_text: "## 2. Confidentiality"
      }
    ]);

    // Assert that the edit was successfully applied
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    // Verify style of the paragraph changed to Heading2
    const pStyle = p1.getElementsByTagName('w:pStyle')[0];
    expect(pStyle).toBeDefined();
    expect(pStyle.getAttribute('w:val')).toBe('Heading2');
  });

  it("TC-2: Bare target, duplicate in body — ambiguity error expected", async () => {
    const doc = await createTestDocument();
    
    // Add Heading 1 paragraph
    const p1 = doc.element.ownerDocument!.createElement('w:p');
    const p1Pr = doc.element.ownerDocument!.createElement('w:pPr');
    const p1Style = doc.element.ownerDocument!.createElement('w:pStyle');
    p1Style.setAttribute('w:val', 'Heading1');
    p1Pr.appendChild(p1Style);
    p1.appendChild(p1Pr);
    const r1 = doc.element.ownerDocument!.createElement('w:r');
    const t1 = doc.element.ownerDocument!.createElement('w:t');
    t1.textContent = '2. Confidentiality';
    r1.appendChild(t1);
    p1.appendChild(r1);
    doc.element.appendChild(p1);

    addParagraph(doc, "As defined in Section 1, the Recipient shall...");
    addParagraph(doc, "Page footer notice: subject to NDA dated 2026-01-15.");
    addParagraph(doc, "For further detail see section 2. Confidentiality above.");

    const engine = new RedlineEngine(doc);

    // Should throw BatchValidationError due to ambiguity
    let caught: any = null;
    try {
      engine.process_batch([
        {
          type: "modify",
          target_text: "2. Confidentiality",
          new_text: "2. CONFIDENTIALITY"
        }
      ]);
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeDefined();
    expect(caught.name).toBe("BatchValidationError");
    expect(caught.message).toContain("Ambiguous match");
  });

  it("TC-3: # prefixed target, duplicate in body — Node fails", async () => {
    const doc = await createTestDocument();
    
    // Add Heading 1 paragraph
    const p1 = doc.element.ownerDocument!.createElement('w:p');
    const p1Pr = doc.element.ownerDocument!.createElement('w:pPr');
    const p1Style = doc.element.ownerDocument!.createElement('w:pStyle');
    p1Style.setAttribute('w:val', 'Heading1');
    p1Pr.appendChild(p1Style);
    p1.appendChild(p1Pr);
    const r1 = doc.element.ownerDocument!.createElement('w:r');
    const t1 = doc.element.ownerDocument!.createElement('w:t');
    t1.textContent = '2. Confidentiality';
    r1.appendChild(t1);
    p1.appendChild(r1);
    doc.element.appendChild(p1);

    addParagraph(doc, "As defined in Section 1, the Recipient shall...");
    addParagraph(doc, "Page footer notice: subject to NDA dated 2026-01-15.");
    addParagraph(doc, "For further detail see section 2. Confidentiality above.");

    const engine = new RedlineEngine(doc);
    
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "# 2. Confidentiality",
        new_text: "## 2. Confidentiality"
      }
    ]);

    // Assert that the edit was successfully applied (resolving ambiguity)
    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    // Verify style of the paragraph changed to Heading2
    const pStyle = p1.getElementsByTagName('w:pStyle')[0];
    expect(pStyle).toBeDefined();
    expect(pStyle.getAttribute('w:val')).toBe('Heading2');
  });

  it("TC-4: Bare target, no duplicate — Node matches heading but mishandles new_text", async () => {
    const doc = await createTestDocument();
    
    // Add Heading 1 paragraph
    const p1 = doc.element.ownerDocument!.createElement('w:p');
    const p1Pr = doc.element.ownerDocument!.createElement('w:pPr');
    const p1Style = doc.element.ownerDocument!.createElement('w:pStyle');
    p1Style.setAttribute('w:val', 'Heading1');
    p1Pr.appendChild(p1Style);
    p1.appendChild(p1Pr);
    const r1 = doc.element.ownerDocument!.createElement('w:r');
    const t1 = doc.element.ownerDocument!.createElement('w:t');
    t1.textContent = '2. Confidentiality';
    r1.appendChild(t1);
    p1.appendChild(r1);
    doc.element.appendChild(p1);

    addParagraph(doc, "As defined in Section 1, the Recipient shall...");

    const engine = new RedlineEngine(doc);
    
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "2. Confidentiality",
        new_text: "## 2. Confidentiality"
      }
    ]);

    expect(res.edits_applied).toBe(1);
    expect(res.edits_skipped).toBe(0);

    // Paragraph style should change to Heading2 (style id Heading2)
    const pStyle = p1.getElementsByTagName('w:pStyle')[0];
    expect(pStyle).toBeDefined();
    expect(pStyle.getAttribute('w:val')).toBe('Heading2');

    // Verify that the run text itself does not contain literal "##"
    const textElements = p1.getElementsByTagName('w:t');
    let rawText = "";
    for (let j = 0; j < textElements.length; j++) {
      rawText += textElements[j].textContent || "";
    }
    expect(rawText).not.toContain("##");
  });
});
