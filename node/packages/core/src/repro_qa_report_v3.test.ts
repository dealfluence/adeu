import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";

describe("QA Report V3 Defects Reproductions", () => {
  it("TC1: Sequential batch evaluation (modify->modify chaining) [report F1]", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "As defined in Section 1, the Recipient shall maintain confidentiality of all materials.");
    const engine = new RedlineEngine(doc);

    // If sequential evaluation works (like Python), the second edit will find "Receiving Party" (the output of the first edit).
    // On the current unpatched Node engine, this will FAIL and throw "Target text not found in document: Receiving Party".
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "the Recipient",
        new_text: "Receiving Party",
      } as any,
      {
        type: "modify",
        target_text: "Receiving Party",
        new_text: "Disclosee",
      } as any,
    ]);

    expect(res.edits_applied).toBe(2);
    expect(res.edits_skipped).toBe(0);
  });

  it("TC2: NODE dry-run == real write for an edit inside a foreign insertion [report F2, F3]", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    // Create a paragraph with an active insertion by "Original Drafter"
    const p = addParagraph(doc, "The party shall provide ");
    const ins = xmlDoc.createElement("w:ins");
    ins.setAttribute("w:id", "101");
    ins.setAttribute("w:author", "Original Drafter");
    ins.setAttribute("w:date", "2026-06-29T12:00:00Z");

    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "five (5)";
    r.appendChild(t);
    ins.appendChild(r);
    p.appendChild(ins);

    const suffixRun = xmlDoc.createElement("w:r");
    const suffixText = xmlDoc.createElement("w:t");
    suffixText.textContent = " years.";
    suffixRun.appendChild(suffixText);
    p.appendChild(suffixRun);

    const engine = new RedlineEngine(doc, "QA Tester");

    // A strict edit fully contained inside a foreign insertion now applies: the
    // enclosing <w:ins> is split and the change nested. Dry-run and write agree.
    // (Fresh edit object per call: a dry-run mutates the edit's resolution state.)
    const resDry = engine.process_batch(
      [{ type: "modify", target_text: "five (5)", new_text: "seven (7)" } as any],
      true,
    );
    expect(resDry.edits_applied).toBe(1);
    expect(resDry.edits_skipped).toBe(0);
    expect(resDry.edits[0].status).toBe("applied");

    const resWet = engine.process_batch(
      [{ type: "modify", target_text: "five (5)", new_text: "seven (7)" } as any],
      false,
    );
    expect(resWet.edits_applied).toBe(1);
    expect(resWet.edits_skipped).toBe(0);
  });

  it("TC3: Heading targeted by markdown '#' corrupts instead of failing [report F4, F5]", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    // Create a styled heading "3. Pending Review" (without literal "#" in the Word doc) using Heading1 style
    const p = xmlDoc.createElement("w:p");
    const pPr = xmlDoc.createElement("w:pPr");
    const pStyle = xmlDoc.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "Heading1");
    pPr.appendChild(pStyle);
    p.appendChild(pPr);

    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "3. Pending Review";
    r.appendChild(t);
    p.appendChild(r);
    doc.element.appendChild(p);

    const engine = new RedlineEngine(doc);

    // Target by its markdown representation
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "# 3. Pending Review",
        new_text: "# 3. Final Review",
      } as any,
    ]);

    expect(res.edits_applied).toBe(1);

    // The correct behavior must NOT produce a redline containing a literal '#' character in the CriticMarkup preview
    // or body text (such as `{--# 3. Pending--}{++# 3. Final++} Review`).
    // Asserting that the critic_markup does NOT contain "{--#" or "{++#" will fail precisely due to this bug.
    const criticMarkup = res.edits[0].critic_markup;
    expect(criticMarkup).not.toContain("{--#");
    expect(criticMarkup).not.toContain("{++#");
  });

  it("TC5: w16du namespace stamped on untouched parts [report F9]", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is untouched body text.");

    // Inject a header part without w16du namespace
    const headerXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
      <w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:p><w:r><w:t>Header Text</w:t></w:r></w:p>
      </w:hdr>`;
    const headerPart = doc.pkg.addPart(
      "/word/header1.xml",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
      headerXml,
    );
    doc.relateTo(
      headerPart,
      "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
    );

    const engine = new RedlineEngine(doc);
    engine.process_batch([
      {
        type: "modify",
        target_text: "untouched body",
        new_text: "changed body",
      } as any,
    ]);

    // Save the package to trigger serialization of all parts
    await doc.save();

    // Verify the header XML does not contain the word16du namespace
    const savedHeaderXml = headerPart._element.toString();
    expect(savedHeaderXml).not.toContain("word16du");
  });

  it("TC8: Error message mislabels edit index [report F7]", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "First paragraph text.");

    const engine = new RedlineEngine(doc);

    // Run in dry_run mode where the bug manifests.
    // Edit 1 succeeds, Edit 2 fails because "Non-existent text" is not found.
    // On the unpatched codebase, individual validation of edit 2 passes single_errors = validate_edits([edit]),
    // which hardcodes i = 0 internally. Thus, the error in res.edits[1].error is:
    // "- Edit 1 Failed: Target text not found..." instead of "- Edit 2 Failed: ...".
    const res = engine.process_batch([
      {
        type: "modify",
        target_text: "First paragraph",
        new_text: "Updated first paragraph",
      } as any,
      {
        type: "modify",
        target_text: "Non-existent text",
        new_text: "Failed update",
      } as any,
    ], true);

    // Dry-run mirrors the real run's transactional semantics (QA 2026-07-17
    // M1 parity): a batch with any invalid edit applies nothing. The valid
    // edit is reported as blocked by the batch, the invalid one keeps its own
    // error.
    expect(res.edits_applied).toBe(0);
    expect(res.edits_skipped).toBe(2);
    expect(res.edits[0].status).toBe("failed");
    expect(res.edits[0].error).toContain("transactional");
    expect(res.edits[1].status).toBe("failed");

    // Assert that the error message correctly labels it as Edit 2.
    // The buggy unpatched codebase says "Edit 1 Failed:" inside the error message for Edit 2.
    const errorMsg = res.edits[1].error;
    expect(errorMsg).toContain("Edit 2 Failed:");
    expect(errorMsg).not.toContain("Edit 1 Failed:");
  });
});

