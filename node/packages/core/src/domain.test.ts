import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { ModifyText } from "./models.js";
import { split_structural_appendix } from "./pagination.js";
import { parseXml } from "./docx/dom.js";

function addBookmark(
  paragraph: Element,
  name: string,
  idVal: string = "0",
  text: string = "",
) {
  const doc = paragraph.ownerDocument!;
  const start = doc.createElement("w:bookmarkStart");
  start.setAttribute("w:name", name);
  start.setAttribute("w:id", idVal);
  paragraph.appendChild(start);

  if (text) {
    const r = doc.createElement("w:r");
    const t = doc.createElement("w:t");
    t.textContent = text;
    if (text.includes(" ")) t.setAttribute("xml:space", "preserve");
    r.appendChild(t);
    paragraph.appendChild(r);
  }

  const end = doc.createElement("w:bookmarkEnd");
  end.setAttribute("w:id", idVal);
  paragraph.appendChild(end);
}

function addCrossReference(paragraph: Element, refName: string, text: string) {
  const doc = paragraph.ownerDocument!;
  const fld = doc.createElement("w:fldSimple");
  fld.setAttribute("w:instr", ` REF ${refName} \\h `);
  const r = doc.createElement("w:r");
  const t = doc.createElement("w:t");
  t.textContent = text;
  if (text.includes(" ")) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  fld.appendChild(r);
  paragraph.appendChild(fld);
}

function addHyperlink(
  docObj: DocumentObject,
  paragraph: Element,
  url: string,
  text: string,
) {
  const idStr = docObj.relateToExternal(
    url,
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
  );

  const doc = paragraph.ownerDocument!;
  const hyperlink = doc.createElement("w:hyperlink");
  hyperlink.setAttribute("r:id", idStr);
  const r = doc.createElement("w:r");
  const t = doc.createElement("w:t");
  t.textContent = text;
  if (text.includes(" ")) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  hyperlink.appendChild(r);
  paragraph.appendChild(hyperlink);
}

function setupFootnotesPart(docObj: DocumentObject) {
  const fnXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:footnote w:type="separator" w:id="-1">
            <w:p><w:r><w:separator/></w:r></w:p>
        </w:footnote>
        <w:footnote w:id="1">
            <w:p><w:r><w:t>Footnote content.</w:t></w:r></w:p>
        </w:footnote>
    </w:footnotes>`;

  const partname = "/word/footnotes.xml";
  const ctype =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml";
  const relType =
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes";

  const part = docObj.pkg.addPart(partname, ctype, fnXml);
  docObj.relateTo(part, relType);
}

async function createDomainSemanticsStream() {
  const doc = await createTestDocument();

  // 1. Appendix / Definitions
  const p1 = addParagraph(doc, "1. Definitions");
  p1.insertBefore(p1.ownerDocument!.createElement("w:pPr"), p1.firstChild);
  addParagraph(
    doc,
    '"Affiliate" means any entity that controls, is controlled by, or is under common control.',
  );
  addParagraph(
    doc,
    "“Confidential Information” means all non-public information disclosed by one party to the other.",
  );
  addParagraph(doc, "This paragraph does not define anything.");

  const p2 = addParagraph(doc, "2. Obligations");
  p2.insertBefore(p2.ownerDocument!.createElement("w:pPr"), p2.firstChild);
  addParagraph(
    doc,
    "The Affiliate shall protect the Confidential Information to the highest standard.",
  );

  // 3. Bookmarks and Cross-References
  const p3 = addParagraph(doc, "Subject to ");
  addBookmark(p3, "MyBookmark_1", "1", "Anchored Clause");
  const r3 = p3.ownerDocument!.createElement("w:r");
  const t3 = p3.ownerDocument!.createElement("w:t");
  t3.textContent = ", the parties agree to...";
  t3.setAttribute("xml:space", "preserve");
  r3.appendChild(t3);
  p3.appendChild(r3);

  const p4 = addParagraph(doc, "As strictly stated in ");
  addCrossReference(p4, "MyBookmark_1", "Anchored Clause");
  const r4 = p4.ownerDocument!.createElement("w:r");
  const t4 = p4.ownerDocument!.createElement("w:t");
  t4.textContent = ", either party may terminate.";
  t4.setAttribute("xml:space", "preserve");
  r4.appendChild(t4);
  p4.appendChild(r4);

  // 4. Internal Anchors
  const pAnchor = addParagraph(doc, "Section 5. Indemnification");
  addBookmark(pAnchor, "_Ref12345", "0");

  const pNoise = addParagraph(doc, "Some text.");
  addBookmark(pNoise, "_GoBack", "2");
  addBookmark(pNoise, "_Toc123456789", "3");

  // 5. Footnotes
  const pFn = addParagraph(doc, "Sentence with footnote");
  const rFn = pFn.ownerDocument!.createElement("w:r");
  const ref = pFn.ownerDocument!.createElement("w:footnoteReference");
  ref.setAttribute("w:id", "1");
  rFn.appendChild(ref);
  pFn.appendChild(rFn);
  setupFootnotesPart(doc);

  // 6. Links and Cross references
  const pLink = addParagraph(doc, "Please visit ");
  addHyperlink(doc, pLink, "https://adeu.com", "Adeu HQ");

  const pXref = addParagraph(doc, "As detailed in ");
  addCrossReference(pXref, "_Ref12345", "Section 5");

  return doc.save();
}

describe("Domain Semantics Engine", () => {
  it("extracts and projects structural appendix and diagnostics correctly", async () => {
    const buf = await createDomainSemanticsStream();
    const text = await extractTextFromBuffer(buf);

    expect(text).toContain("<!-- READONLY_BOUNDARY_START -->");
    expect(text).toContain("# Document Structure (Read-Only)");

    // Definitions
    expect(text).toContain("## Defined Terms");
    expect(text).toContain('"Affiliate"');
    expect(text).toContain('"Confidential Information"');
    expect(text).toContain("used 1 times");

    // Named Anchors & Back-References
    expect(text).toContain("## Named Anchors");
    expect(text).toContain("MyBookmark_1");
    expect(text).toContain("Anchored to:");
    expect(text).toContain("Referenced from:");

    // Internal anchors & Noise suppression
    expect(text).toContain("{#_Ref12345}");
    expect(text).toContain("Section 5. Indemnification{#_Ref12345}");
    expect(text).not.toContain("{#_GoBack}");
    expect(text).not.toContain("{#_Toc123456789}");

    // Footnotes
    expect(text).toContain("[^fn-1]");
    expect(text).toContain("## Footnotes");
    expect(text).toContain("[^fn-1]: Footnote content.");

    // Links
    expect(text).toContain("[Adeu HQ](https://adeu.com)");
    expect(text).toContain("[~Section 5~](#_Ref12345)");
  });

  const edgeCases = [
    {
      target: "# Document Structure (Read-Only)",
      newText: "# Modified Document Structure",
      errChecker: (m: string) =>
        m.includes("read-only boundary") || m.includes("appendix"),
    },
    {
      target: "Sentence with footnote[^fn-1]",
      newText: "Sentence with footnote",
      errChecker: (m: string) =>
        m.includes("footnote") &&
        (m.includes("delete") || m.includes("remove")),
    },
    {
      target: "Sentence with footnote",
      newText: "Sentence with footnote[^fn-99]",
      errChecker: (m: string) =>
        m.includes("footnote") &&
        (m.includes("insert") || m.includes("create")),
    },
    {
      target: "Some text.",
      newText: "Some text.{#_Ref99999}",
      errChecker: (m: string) => m.includes("anchor"),
    },
    {
      target: "Section 5. Indemnification{#_Ref12345}",
      newText: "Section 5. Indemnification{#_Ref99999}",
      errChecker: (m: string) => m.includes("anchor"),
    },
    {
      target: "[~Section 5~](#_Ref12345)",
      newText: "[~Section 6~](#_Ref12345)",
      errChecker: (m: string) =>
        m.includes("cross-reference") || m.includes("rejected"),
    },
    {
      target: "[~Section 5~](#_Ref12345)",
      newText: "[~Section 5~](#_Ref99999)",
      errChecker: (m: string) =>
        m.includes("dependency corruption") || m.includes("rejected"),
    },
    {
      target: "As detailed in [~Section 5~](#_Ref12345)",
      newText:
        "As detailed in [~Section 5~](#_Ref12345) and [~Section 6~](#_Ref999)",
      errChecker: (m: string) =>
        m.includes("cross-reference") || m.includes("read-only"),
    },
    {
      target: "As detailed in [~Section 5~](#_Ref12345)",
      newText: "As detailed in nothing",
      errChecker: (m: string) =>
        m.includes("cross-reference") || m.includes("delete"),
    },
    {
      target: "Please visit [Adeu HQ](https://adeu.com)",
      newText:
        "Please visit [Adeu HQ](https://adeu.com) and [Google](https://google.com)",
      errChecker: (m: string) =>
        m.includes("hyperlink") || m.includes("insert"),
    },
    {
      target: "Please visit [Adeu HQ](https://adeu.com)",
      newText: "Please visit nothing",
      errChecker: (m: string) =>
        m.includes("hyperlink") || m.includes("delete"),
    },
  ];

  for (const tc of edgeCases) {
    it(`rejects invalid edits: ${tc.target} -> ${tc.newText}`, async () => {
      const buf = await createDomainSemanticsStream();
      const doc = await DocumentObject.load(buf);
      const engine = new RedlineEngine(doc);
      const edit: ModifyText = {
        type: "modify",
        target_text: tc.target,
        new_text: tc.newText,
      };

      let errorThrown = false;
      try {
        engine.process_batch([edit]);
      } catch (e) {
        errorThrown = true;
        if (e instanceof BatchValidationError) {
          const msg = e.errors.join("\n").toLowerCase();
          expect(tc.errChecker(msg)).toBe(true);
        } else {
          throw e; // unexpected error
        }
      }
      expect(errorThrown).toBe(true);
    });
  }

  it("safely edits footnotes and accepts changes", async () => {
    const buf = await createDomainSemanticsStream();
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc);

    const edit: ModifyText = {
      type: "modify",
      target_text: "Footnote content.",
      new_text: "This is an edited footnote.",
    };
    const stats = engine.process_batch([edit]);
    expect(stats.edits_applied).toBe(1);

    engine.accept_all_revisions();
    const outBuf = await doc.save();
    const cleanText = await extractTextFromBuffer(outBuf, true);

    expect(cleanText).toContain("[^fn-1]: This is an edited footnote.");
  });

  it("extracts defined terms and finds typos correctly", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, '"Agreement" means this contract.');
    addParagraph(doc, "“Party” shall mean either side.");
    addParagraph(doc, '"Agreement" means another thing.'); // Duplicate
    addParagraph(doc, 'This contract (hereinafter, the "Contract") is valid.');
    addParagraph(doc, '"Confidential Information" on salainen asia.');
    addParagraph(doc, '1.1 "Affiliate" tarkoittaa osakkuusyhtiötä.');
    addParagraph(
      doc,
      'We will act as the disclosing party (jäljempänä "Discloser").',
    );
    addParagraph(doc, 'This is a syntax example: ("Heading*") and ("<Term>")');

    addParagraph(doc, "The Agreement is binding. The Contract is signed.");
    addParagraph(doc, "There is an Agrement here.");
    addParagraph(
      doc,
      "We shared Confidential Information with the Affiliate. The Discloser is happy.",
    );

    const buf = await doc.save();
    const full_text = await extractTextFromBuffer(buf, false);
    const [, appendix] = split_structural_appendix(full_text);

    expect(appendix).toContain('"Agreement" \u2014 used');
    expect(appendix).toContain('"Contract" \u2014 used');
    expect(appendix).toContain('"Confidential Information" \u2014 used');
    expect(appendix).toContain('"Affiliate" \u2014 used');
    expect(appendix).toContain('"Discloser" \u2014 used');

    expect(appendix).not.toContain('"Party"');
    expect(appendix).not.toContain('"Heading*"');
    expect(appendix).not.toContain('"<Term>"');

    expect(appendix).toContain(
      "[Error] Duplicate Definition: 'Agreement' is defined multiple times.",
    );
    expect(appendix).toContain(
      "[Info] Possible Typos for 'Agreement': Found 'Agrement'",
    );
  });

  it("reduces typo noise for short acronyms", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, '"PSUs" means power supply units.');
    addParagraph(doc, '"CPU" means central processing unit.');
    addParagraph(doc, '"Party" means the entity.');
    addParagraph(doc, "We rely on ESAs, LSPs, and GPUs for the servers.");
    addParagraph(doc, "The GPU is very fast.");
    addParagraph(doc, "The Pary signed the contract.");
    addParagraph(doc, "We bought PSUs and a CPU.");
    addParagraph(doc, "The Party begins today.");

    const buf = await doc.save();
    const full_text = await extractTextFromBuffer(buf, false);
    const [, appendix] = split_structural_appendix(full_text);

    expect(appendix).toContain(
      "[Info] Possible Typos for 'Party': Found 'Pary'",
    );
    expect(appendix).not.toContain("'GPU'");
    expect(appendix).not.toContain("'GPUs'");
    expect(appendix).not.toContain("'ESAs'");
    expect(appendix).not.toContain("'LSPs'");
  });

  describe("Privacy-flag warnings in Document Settings section", () => {
    // Helper to attach a synthetic word/settings.xml with the given inner XML
    // (children of <w:settings>). Replaces any existing settings part.
    function installSettings(doc: any, innerXml: string) {
      const fullXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">${innerXml}</w:settings>`;
      doc.pkg.parts = doc.pkg.parts.filter(
        (p: any) =>
          p.partname !== "/word/settings.xml" &&
          p.partname !== "word/settings.xml",
      );
      doc.pkg.addPart(
        "/word/settings.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
        fullXml,
      );
    }

    it("emits warnings when removePersonalInformation and removeDateAndTime are enabled", async () => {
      const doc = await createTestDocument();
      addParagraph(doc, "Some body text so the doc is non-empty.");
      installSettings(
        doc,
        '<w:removePersonalInformation w:val="true"/><w:removeDateAndTime w:val="true"/>',
      );

      const buf = await doc.save();
      const full_text = await extractTextFromBuffer(buf, false);
      const [, appendix] = split_structural_appendix(full_text);

      expect(appendix).toContain("## Document Settings");
      expect(appendix).toContain("`removePersonalInformation` is enabled");
      expect(appendix).toContain("`removeDateAndTime` is enabled");
      expect(appendix).toContain("[Warning]");
    });

    it("treats an element with no w:val as truthy (OOXML default)", async () => {
      const doc = await createTestDocument();
      addParagraph(doc, "Body.");
      installSettings(doc, "<w:removePersonalInformation/>");

      const buf = await doc.save();
      const full_text = await extractTextFromBuffer(buf, false);
      const [, appendix] = split_structural_appendix(full_text);

      expect(appendix).toContain("`removePersonalInformation` is enabled");
    });

    it.each(["0", "false", "off", "FALSE"])(
      'does NOT warn when the flag is explicitly disabled (w:val="%s")',
      async (val) => {
        const doc = await createTestDocument();
        addParagraph(doc, "Body.");
        installSettings(
          doc,
          `<w:removePersonalInformation w:val="${val}"/><w:removeDateAndTime w:val="${val}"/>`,
        );

        const buf = await doc.save();
        const full_text = await extractTextFromBuffer(buf, false);
        const [, appendix] = split_structural_appendix(full_text);

        expect(appendix).not.toContain("## Document Settings");
        expect(appendix).not.toContain(
          "`removePersonalInformation` is enabled",
        );
        expect(appendix).not.toContain("`removeDateAndTime` is enabled");
      },
    );

    it("does NOT emit the section when no privacy flags are present", async () => {
      const doc = await createTestDocument();
      addParagraph(doc, "Body with no privacy flags.");
      installSettings(doc, '<w:zoom w:percent="100"/>');

      const buf = await doc.save();
      const full_text = await extractTextFromBuffer(buf, false);
      const [, appendix] = split_structural_appendix(full_text);

      expect(appendix).not.toContain("## Document Settings");
    });

    it("places ## Document Settings before ## Defined Terms in the appendix", async () => {
      const doc = await createTestDocument();
      addParagraph(doc, '"MyTerm" means something important.');
      addParagraph(doc, "The MyTerm is referenced again here.");
      installSettings(doc, '<w:removePersonalInformation w:val="true"/>');

      const buf = await doc.save();
      const full_text = await extractTextFromBuffer(buf, false);
      const [, appendix] = split_structural_appendix(full_text);

      const settingsIdx = appendix.indexOf("## Document Settings");
      const termsIdx = appendix.indexOf("## Defined Terms");

      expect(settingsIdx).toBeGreaterThan(-1);
      expect(termsIdx).toBeGreaterThan(-1);
      expect(settingsIdx).toBeLessThan(termsIdx);
    });
  });
});
