import * as fs from "fs/promises";
import * as os from "os";
import * as path from "path";
import { describe, expect, it } from "vitest";
import { build_redline_comparison } from "./compare.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { extract_outline } from "./outline.js";
import { paginate } from "./pagination.js";
import { addParagraph, createTestDocument } from "./test-utils.js";
import { apply_text_revision_core } from "./text-revision.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

function attachNumberingXml(doc: DocumentObject, innerXml: string): void {
  const fullXml =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<w:numbering xmlns:w="${W_NS}">${innerXml}</w:numbering>`;
  doc.pkg.addPart(
    "/word/numbering.xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    fullXml,
  );
}

function addListNumberStyle(doc: DocumentObject, numId = "2"): void {
  const stylesPart = doc.pkg.getPartByPath("word/styles.xml")!;
  const sDoc = stylesPart._element.ownerDocument!;
  const style = sDoc.createElement("w:style");
  style.setAttribute("w:type", "paragraph");
  style.setAttribute("w:styleId", "ListNumber");
  const nameEl = sDoc.createElement("w:name");
  nameEl.setAttribute("w:val", "List Number");
  style.appendChild(nameEl);
  const basedOn = sDoc.createElement("w:basedOn");
  basedOn.setAttribute("w:val", "Normal");
  style.appendChild(basedOn);
  const pPr = sDoc.createElement("w:pPr");
  const numPr = sDoc.createElement("w:numPr");
  const ilvl = sDoc.createElement("w:ilvl");
  ilvl.setAttribute("w:val", "0");
  const numIdEl = sDoc.createElement("w:numId");
  numIdEl.setAttribute("w:val", numId);
  numPr.appendChild(ilvl);
  numPr.appendChild(numIdEl);
  pPr.appendChild(numPr);
  style.appendChild(pPr);
  stylesPart._element.appendChild(style);
}

function addStyledListParagraph(doc: DocumentObject, text: string, styleId = "ListNumber"): Element {
  const p = addParagraph(doc, text);
  const xmlDoc = doc.element.ownerDocument!;
  const pPr = xmlDoc.createElement("w:pPr");
  const pStyle = xmlDoc.createElement("w:pStyle");
  pStyle.setAttribute("w:val", styleId);
  pPr.appendChild(pStyle);
  p.insertBefore(pPr, p.firstChild);
  return p;
}

function setNumPr(p: Element, numId: string | number, ilvl: number | null = 0, pStyleId?: string): void {
  const xmlDoc = p.ownerDocument!;
  let pPr = Array.from(p.childNodes).find(
    (n): n is Element => n.nodeType === 1 && (n as Element).tagName === "w:pPr",
  );
  if (!pPr) {
    pPr = xmlDoc.createElement("w:pPr");
    p.insertBefore(pPr, p.firstChild);
  }
  if (pStyleId) {
    const pStyle = xmlDoc.createElement("w:pStyle");
    pStyle.setAttribute("w:val", pStyleId);
    pPr.insertBefore(pStyle, pPr.firstChild);
  }
  const numPr = xmlDoc.createElement("w:numPr");
  if (ilvl !== null) {
    const ilvlEl = xmlDoc.createElement("w:ilvl");
    ilvlEl.setAttribute("w:val", String(ilvl));
    numPr.appendChild(ilvlEl);
  }
  const numIdEl = xmlDoc.createElement("w:numId");
  numIdEl.setAttribute("w:val", String(numId));
  numPr.appendChild(numIdEl);
  pPr.appendChild(numPr);
}

describe("Resolved OOXML clause and heading numbering (TypeScript parity)", () => {
  it("resolves sequential List Number items and keeps mapper in sync", async () => {
    const doc = await createTestDocument();
    addListNumberStyle(doc, "2");
    attachNumberingXml(
      doc,
      `<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>` +
        `<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>`,
    );
    addStyledListParagraph(doc, "First obligation of Seller.");
    addStyledListParagraph(doc, "Second obligation of Seller.");
    addStyledListParagraph(doc, "Liability cap clause.");
    addParagraph(doc, "Cross-reference: see Clause 3 above.");

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, false, false);
    expect(text).toContain("1. First obligation of Seller.");
    expect(text).toContain("2. Second obligation of Seller.");
    expect(text).toContain("3. Liability cap clause.");

    const reloaded = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(reloaded);
    expect(mapper.full_text).toBe(text);
  });

  it("resolves multilevel Polish legal, alpha, Roman, isLgl, lvlRestart=0, and startOverride", async () => {
    const doc = await createTestDocument();
    attachNumberingXml(
      doc,
      `
      <w:abstractNum w:abstractNumId="10">
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="§ %1."/></w:lvl>
        <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>
        <w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%3)"/></w:lvl>
      </w:abstractNum>
      <w:abstractNum w:abstractNumId="20">
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="upperRoman"/><w:lvlText w:val="ARTICLE %1"/></w:lvl>
        <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:isLgl/><w:lvlText w:val="%1.%2"/></w:lvl>
      </w:abstractNum>
      <w:abstractNum w:abstractNumId="30">
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="upperLetter"/><w:lvlText w:val="Part %1"/></w:lvl>
        <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlRestart w:val="0"/><w:lvlText w:val="Clause %2."/></w:lvl>
      </w:abstractNum>
      <w:num w:numId="10"><w:abstractNumId w:val="10"/></w:num>
      <w:num w:numId="11">
        <w:abstractNumId w:val="10"/>
        <w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride>
      </w:num>
      <w:num w:numId="20"><w:abstractNumId w:val="20"/></w:num>
      <w:num w:numId="30"><w:abstractNumId w:val="30"/></w:num>
      `,
    );

    setNumPr(addParagraph(doc, "Przedmiot umowy"), 10, 0);
    setNumPr(addParagraph(doc, "Zakres usług"), 10, 1);
    setNumPr(addParagraph(doc, "Konsultacje"), 10, 2);
    setNumPr(addParagraph(doc, "Wdrożenie"), 10, 2);
    setNumPr(addParagraph(doc, "Terminy"), 10, 1);
    setNumPr(addParagraph(doc, "Wynagrodzenie"), 10, 0);
    setNumPr(addParagraph(doc, "Fakturowanie"), 10, 1);

    setNumPr(addParagraph(doc, "Kary umowne"), 11, 0);
    setNumPr(addParagraph(doc, "Odstąpienie"), 11, 0);

    setNumPr(addParagraph(doc, "Definitions"), 20, 0);
    setNumPr(addParagraph(doc, "Defined Terms"), 20, 1);
    setNumPr(addParagraph(doc, "Obligations"), 20, 0);
    setNumPr(addParagraph(doc, "Performance"), 20, 1);

    setNumPr(addParagraph(doc, "General"), 30, 0);
    setNumPr(addParagraph(doc, "First continuous"), 30, 1);
    setNumPr(addParagraph(doc, "Special"), 30, 0);
    setNumPr(addParagraph(doc, "Second continuous"), 30, 1);

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const text = _extractTextFromDoc(reloaded, false, false) as string;
    const mapper = new DocumentMapper(reloaded);
    expect(mapper.full_text).toBe(text);

    expect(text).toBe(
      [
        "§ 1. Przedmiot umowy",
        "    1.1. Zakres usług",
        "        (a) Konsultacje",
        "        (b) Wdrożenie",
        "    1.2. Terminy",
        "§ 2. Wynagrodzenie",
        "    2.1. Fakturowanie",
        "§ 5. Kary umowne",
        "§ 6. Odstąpienie",
        "ARTICLE I Definitions",
        "    1.1 Defined Terms",
        "ARTICLE II Obligations",
        "    2.1 Performance",
        "Part A General",
        "    Clause 1. First continuous",
        "Part B Special",
        "    Clause 2. Second continuous",
      ].join("\n\n"),
    );
  });

  it("projects auto-numbered headings in body and outline", async () => {
    const doc = await createTestDocument();
    attachNumberingXml(
      doc,
      `
      <w:abstractNum w:abstractNumId="40">
        <w:lvl w:ilvl="0">
          <w:start w:val="1"/><w:numFmt w:val="decimal"/>
          <w:pStyle w:val="Heading1"/><w:lvlText w:val="§ %1."/>
        </w:lvl>
        <w:lvl w:ilvl="1">
          <w:start w:val="1"/><w:numFmt w:val="decimal"/>
          <w:pStyle w:val="Heading2"/><w:lvlText w:val="%1.%2."/>
        </w:lvl>
      </w:abstractNum>
      <w:num w:numId="40"><w:abstractNumId w:val="40"/></w:num>
      `,
    );

    setNumPr(addParagraph(doc, "Postanowienia ogólne"), 40, null, "Heading1");
    setNumPr(addParagraph(doc, "Definicje umowne"), 40, null, "Heading2");
    setNumPr(addParagraph(doc, "Odpowiedzialność stron"), 40, null, "Heading1");
    setNumPr(addParagraph(doc, "Załączniki"), 0, 0, "Heading1");

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const { text, paragraph_offsets: offsets } = _extractTextFromDoc(
      reloaded,
      false,
      false,
      true,
    ) as { text: string; paragraph_offsets: Map<any, [number, number]> };
    const mapper = new DocumentMapper(reloaded);
    expect(mapper.full_text).toBe(text);

    expect(text).toContain("# § 1. Postanowienia ogólne");
    expect(text).toContain("## 1.1. Definicje umowne");
    expect(text).toContain("# § 2. Odpowiedzialność stron");
    expect(text).toContain("# Załączniki");

    const pag = paginate(text, "");
    const pageBodies = pag.pages.map((p) => p.page_content);
    const fastNodes = extract_outline(
      reloaded,
      text,
      pageBodies,
      pag.body_page_offsets,
      offsets,
    );
    const slowNodes = extract_outline(
      reloaded,
      text,
      pageBodies,
      pag.body_page_offsets,
    );
    expect(fastNodes.map((n) => n.text)).toEqual([
      "§ 1. Postanowienia ogólne",
      "1.1. Definicje umowne",
      "§ 2. Odpowiedzialność stron",
      "Załączniki",
    ]);
    expect(slowNodes.map((n) => n.text)).toEqual(fastNodes.map((n) => n.text));
  });

  it("handles clean_view renumbering, sequential batch edits, compare, and text-revision", async () => {
    const makeDoc = async (clauses: string[]): Promise<Uint8Array> => {
      const doc = await createTestDocument();
      addListNumberStyle(doc, "2");
      attachNumberingXml(
        doc,
        `<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>` +
          `<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>`,
      );
      for (const c of clauses) {
        addStyledListParagraph(doc, c);
      }
      return doc.save();
    };

    // 1. Sequential batch with item insertion + pre-insertion clause target
    const buf1 = await makeDoc(["First obligation.", "Second obligation.", "Liability cap is 100 PLN."]);
    const engine = new RedlineEngine(await DocumentObject.load(buf1), "Reviewer");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "1. First obligation.",
        new_text: "1. First obligation.\n\n1. Inserted obligation.",
      },
      {
        type: "modify",
        target_text: "3. Liability cap is 100 PLN.",
        new_text: "3. Liability cap is 500 PLN.",
      },
    ]);
    expect(stats.edits_applied).toBe(2);
    const saved1 = await engine.doc.save();
    const accepted1 = await extractTextFromBuffer(saved1, true, false);
    expect(accepted1).toBe(
      "1. First obligation.\n\n2. Inserted obligation.\n\n3. Second obligation.\n\n4. Liability cap is 500 PLN.",
    );

    // 2. DOCX-to-DOCX compare ignores downstream auto-renumbering
    const v1 = await makeDoc(["Clause Alpha.", "Clause Beta.", "Clause Gamma.", "Clause Delta."]);
    const v2 = await makeDoc([
      "Clause Alpha.",
      "Clause Inserted.",
      "Clause Beta.",
      "Clause Gamma.",
      "Clause Delta.",
    ]);
    const cmp = await build_redline_comparison(
      Buffer.from(v1),
      Buffer.from(v2),
      "CompareBot",
    );
    expect(cmp.applied).toBe(1);
    const rawCmp = await extractTextFromBuffer(cmp.data, false, false);
    expect(rawCmp).not.toContain("{--");
    const cleanCmp = await extractTextFromBuffer(cmp.data, true, false);
    expect(cleanCmp).toBe(
      "1. Clause Alpha.\n\n2. Clause Inserted.\n\n3. Clause Beta.\n\n4. Clause Gamma.\n\n5. Clause Delta.",
    );

    // 3. apply_text_revision_core handles deleting Clause 1 without manual renumbering
    const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "adeu-num-"));
    try {
      const srcPath = path.join(tmpDir, "numbered.docx");
      const outPath = path.join(tmpDir, "numbered_revised.docx");
      const revBuf = await makeDoc([
        "Clause Alpha with sufficient body prose to avoid major deletion guard.",
        "Clause Beta with sufficient body prose to keep length well above half.",
        "Clause Gamma with sufficient body prose to keep length well above half.",
      ]);
      await fs.writeFile(srcPath, revBuf);
      const extracted = await extractTextFromBuffer(revBuf, true, false);
      const revised = extracted.split("\n\n").slice(1).join("\n\n");
      expect(revised.startsWith("2. Clause Beta")).toBe(true);

      const res = await apply_text_revision_core({
        doc: await DocumentObject.load(Buffer.from(revBuf)),
        input_path: srcPath,
        revised_text: revised,
        output_path: outPath,
        author: "Editor",
      });
      expect(res.stats.edits_applied).toBe(1);
      const finalClean = await extractTextFromBuffer(res.out_bytes, true, false);
      expect(finalClean.startsWith("1. Clause Beta")).toBe(true);
      expect(finalClean).toContain("2. Clause Gamma");
    } finally {
      await fs.rm(tmpDir, { recursive: true, force: true });
    }
  });
});
