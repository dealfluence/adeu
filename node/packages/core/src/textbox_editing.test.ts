import { describe, expect, it } from "vitest";
import { DocumentObject } from "./docx/bridge.js";
import { findChild, parseXml } from "./docx/dom.js";
import { collectFields, fieldSummary } from "./fields.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { addParagraph, createTestDocument } from "./test-utils.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006";
const WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
const A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main";
const WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape";
const V_NS = "urn:schemas-microsoft-com:vml";
const W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml";

export function makeAcTextboxRunXml(
  innerChoiceXml: string,
  innerFallbackXml?: string,
  opts: { docPrId?: number; shapeName?: string } = {},
): string {
  const fbXml = innerFallbackXml ?? innerChoiceXml;
  const docPrId = opts.docPrId ?? 7;
  const shapeName = opts.shapeName ?? "Text Box 7";
  return (
    `<w:r xmlns:w="${W_NS}" xmlns:mc="${MC_NS}" xmlns:wp="${WP_NS}" ` +
    `xmlns:a="${A_NS}" xmlns:wps="${WPS_NS}" xmlns:v="${V_NS}" xmlns:w14="${W14_NS}">` +
    `<mc:AlternateContent>` +
    `<mc:Choice Requires="wps">` +
    `<w:drawing>` +
    `<wp:anchor behindDoc="0" distT="0" distB="0" distL="0" distR="0" simplePos="0" ` +
    `relativeHeight="2" locked="0" layoutInCell="1" allowOverlap="1">` +
    `<wp:simplePos x="0" y="0"/>` +
    `<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>` +
    `<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>` +
    `<wp:extent cx="2540000" cy="1270000"/>` +
    `<wp:wrapNone/>` +
    `<wp:docPr id="${docPrId}" name="${shapeName}"/>` +
    `<a:graphic><a:graphicData uri="${WPS_NS}">` +
    `<wps:wsp><wps:txbx><w:txbxContent>${innerChoiceXml}</w:txbxContent></wps:txbx></wps:wsp>` +
    `</a:graphicData></a:graphic>` +
    `</wp:anchor>` +
    `</w:drawing>` +
    `</mc:Choice>` +
    `<mc:Fallback>` +
    `<w:pict>` +
    `<v:shape id="${shapeName}" style="position:absolute">` +
    `<v:textbox><w:txbxContent>${fbXml}</w:txbxContent></v:textbox>` +
    `</v:shape>` +
    `</w:pict>` +
    `</mc:Fallback>` +
    `</mc:AlternateContent>` +
    `</w:r>`
  );
}

function appendFragment(parent: Element, xml: string): Element {
  const parsed = parseXml(xml).documentElement;
  const setOwner = (node: any): void => {
    node.ownerDocument = parent.ownerDocument;
    if (node.childNodes) {
      for (const ch of node.childNodes) setOwner(ch);
    }
  };
  setOwner(parsed);
  parent.appendChild(parsed);
  return parsed;
}

function attachHeaderOrFooter(
  doc: DocumentObject,
  kind: "header" | "footer",
  rId: string,
  partname: string,
  bodyXml: string,
): Element {
  const rootTag = kind === "header" ? "w:hdr" : "w:ftr";
  const contentType =
    kind === "header"
      ? "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
      : "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml";
  const relType =
    kind === "header"
      ? "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
      : "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer";
  const xml =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<${rootTag} xmlns:w="${W_NS}" xmlns:mc="${MC_NS}" xmlns:wp="${WP_NS}" ` +
    `xmlns:a="${A_NS}" xmlns:wps="${WPS_NS}" xmlns:v="${V_NS}" xmlns:w14="${W14_NS}">${bodyXml}</${rootTag}>`;
  const part = doc.pkg.addPart(`/${partname}`, contentType, xml);
  const docRels = doc.pkg.getOrCreateRelsPart(doc.part.partname);
  const relTarget = partname.replace(/^word\//, "");
  docRels.addRelationship(rId, relType, relTarget, false);
  doc.part.addRelationship(rId, relType, relTarget, false);

  let sectPr = findChild(doc.element, "w:sectPr");
  if (!sectPr) {
    sectPr = appendFragment(
      doc.element,
      `<w:sectPr xmlns:w="${W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>`,
    );
  }
  const refTag = kind === "header" ? "w:headerReference" : "w:footerReference";
  appendFragment(
    sectPr,
    `<${refTag} xmlns:w="${W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" w:type="default" r:id="${rId}"/>`,
  );
  return part._element;
}

describe("Textbox projection and mapping (issue #194)", () => {
  it("projects modern mc:AlternateContent textboxes in full without truncation or image marker", async () => {
    const paraOne =
      "Cover Box Paragraph One: This Master Services Agreement is entered into by " +
      "Alpha Holdings GmbH and Beta Operations Ltd. with registered offices in Berlin " +
      "and London respectively, subject to the schedules attached hereto and all " +
      "ancillary data-processing addenda executed on the Effective Date.";
    const longTail =
      "Tail obligation beyond three hundred characters: the Supplier shall maintain " +
      "escrow deposits in Zurich until final acceptance is countersigned.";
    expect(paraOne.length + longTail.length).toBeGreaterThan(350);

    const inner =
      `<w:p><w:r><w:t xml:space="preserve">${paraOne}</w:t></w:r></w:p>` +
      `<w:p><w:r><w:t xml:space="preserve">${longTail}</w:t></w:r></w:p>`;

    const doc = await createTestDocument();
    const host = addParagraph(doc, "Body anchor paragraph.");
    appendFragment(host, makeAcTextboxRunXml(inner));
    addParagraph(doc, "Body closing paragraph.");

    const buf = await doc.save();
    const text = await extractTextFromBuffer(buf, false, false);

    expect(text).toContain(paraOne);
    expect(text).toContain(longTail);
    expect(text).toContain(`${paraOne}\n\n${longTail}`);
    expect(text.split(paraOne).length - 1).toBe(1);
    expect(text).not.toContain("docx-image:");
    expect(text).not.toContain("![Text box");

    const reloaded = await DocumentObject.load(buf);
    const mapper = new DocumentMapper(reloaded);
    expect(mapper.full_text).toBe(text);
    const boxIdx = mapper.full_text.indexOf(paraOne);
    expect(mapper.part_kind_at(boxIdx)).toBe("textbox");
    const spans = mapper.spans.filter((s) => s.run !== null && s.start <= boxIdx && boxIdx < s.end);
    expect(spans.length).toBeGreaterThan(0);
  });

  it("projects existing redlines and formatting inside textboxes in CriticMarkup and cleanView", async () => {
    const choiceInner =
      `<w:p>` +
      `<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Notice:</w:t></w:r>` +
      `<w:r><w:t xml:space="preserve"> Address is </w:t></w:r>` +
      `<w:del w:id="1" w:author="Counterparty" w:date="2026-11-17T09:00:00Z">` +
      `<w:r><w:delText>10 Old Road</w:delText></w:r>` +
      `</w:del>` +
      `<w:ins w:id="2" w:author="Counterparty" w:date="2026-11-17T09:00:00Z">` +
      `<w:r><w:t>25 New Avenue</w:t></w:r>` +
      `</w:ins>` +
      `</w:p>`;
    const fbInner = choiceInner.replace('w:id="1"', 'w:id="3"').replace('w:id="2"', 'w:id="4"');

    const doc = await createTestDocument();
    const p = addParagraph(doc, "Main clause.");
    appendFragment(p, makeAcTextboxRunXml(choiceInner, fbInner));
    const buf = await doc.save();

    const raw = await extractTextFromBuffer(buf, false, false);
    const clean = await extractTextFromBuffer(buf, true, false);

    expect(raw).toContain("**Notice:**");
    expect(raw).toContain("{--10 Old Road--}");
    expect(raw).toContain("{++25 New Avenue++}");
    expect(clean).not.toContain("10 Old Road");
    expect(clean).toContain("**Notice:** Address is 25 New Avenue");

    const loaded = await DocumentObject.load(buf);
    expect(new DocumentMapper(loaded, false).full_text).toBe(raw);
    expect(new DocumentMapper(loaded, true).full_text).toBe(clean);
  });

  it("projects bare VML textboxes and header/footer textboxes in projection order", async () => {
    const doc = await createTestDocument();
    const bodyP = addParagraph(doc, "Main body covenant.");
    appendFragment(
      bodyP,
      `<w:r xmlns:w="${W_NS}" xmlns:v="${V_NS}">` +
        `<w:pict><v:shape id="LegacyBox" style="position:absolute">` +
        `<v:textbox><w:txbxContent><w:p><w:r><w:t>Legacy VML callout obligation.</w:t></w:r></w:p></w:txbxContent></v:textbox>` +
        `</v:shape></w:pict></w:r>`,
    );

    attachHeaderOrFooter(
      doc,
      "header",
      "rIdHdrBox1",
      "word/header1.xml",
      `<w:p><w:r><w:t>Running Header</w:t></w:r>${makeAcTextboxRunXml(
        "<w:p><w:r><w:t>Letterhead Box Suite 400</w:t></w:r></w:p>",
        undefined,
        { docPrId: 11, shapeName: "Header Box" },
      )}</w:p>`,
    );
    attachHeaderOrFooter(
      doc,
      "footer",
      "rIdFtrBox1",
      "word/footer1.xml",
      `<w:p><w:r><w:t>Running Footer</w:t></w:r>${makeAcTextboxRunXml(
        "<w:p><w:r><w:t>Footer Registered Office</w:t></w:r></w:p>",
        undefined,
        { docPrId: 12, shapeName: "Footer Box" },
      )}</w:p>`,
    );

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const text = _extractTextFromDoc(reloaded, false, false) as string;
    const mapper = new DocumentMapper(reloaded);

    expect(mapper.full_text).toBe(text);
    const expectedOrder = [
      "Running Header",
      "Letterhead Box Suite 400",
      "Main body covenant.",
      "Legacy VML callout obligation.",
      "Running Footer",
      "Footer Registered Office",
    ];
    const positions = expectedOrder.map((frag) => text.indexOf(frag));
    expect(positions.every((pos) => pos >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));

    for (const frag of ["Letterhead Box Suite 400", "Legacy VML callout obligation.", "Footer Registered Office"]) {
      expect(mapper.part_kind_at(mapper.full_text.indexOf(frag))).toBe("textbox");
    }
  });

  it("assigns unique content-control ordinals inside textboxes without mc:Fallback duplication", async () => {
    const doc = await createTestDocument();
    appendFragment(
      doc.element,
      `<w:sdt xmlns:w="${W_NS}">` +
        `<w:sdtPr><w:alias w:val="BodyParty"/><w:text/></w:sdtPr>` +
        `<w:sdtContent><w:p><w:r><w:t>Acme Corp</w:t></w:r></w:p></w:sdtContent>` +
        `</w:sdt>`,
    );

    const boxInner =
      `<w:sdt>` +
      `<w:sdtPr><w:alias w:val="BoxAddress"/><w:text/></w:sdtPr>` +
      `<w:sdtContent><w:p><w:r><w:t>123 Main St</w:t></w:r></w:p></w:sdtContent>` +
      `</w:sdt>` +
      `<w:tbl>` +
      `<w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>` +
      `<w:tr><w:tc><w:p><w:r><w:t>Key</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Val</w:t></w:r></w:p></w:tc></w:tr>` +
      `<w:tr><w:tc><w:p><w:r><w:t>City</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Zurich</w:t></w:r></w:p></w:tc></w:tr>` +
      `</w:tbl>`;
    const host = addParagraph(doc, "Anchor");
    appendFragment(host, makeAcTextboxRunXml(boxInner));

    attachHeaderOrFooter(
      doc,
      "footer",
      "rIdFtrSdt1",
      "word/footer1.xml",
      `<w:sdt><w:sdtPr><w:alias w:val="FooterRef"/><w:text/></w:sdtPr>` +
        `<w:sdtContent><w:p><w:r><w:t>REF-9</w:t></w:r></w:p></w:sdtContent></w:sdt>`,
    );

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const raw = _extractTextFromDoc(reloaded, false, false) as string;
    const mapper = new DocumentMapper(reloaded);

    expect(mapper.full_text).toBe(raw);
    const [total] = fieldSummary(reloaded);
    expect(total).toBe(3);

    const entries = collectFields(reloaded, raw);
    expect(entries.map((e) => [e.ordinal, e.alias, e.value])).toEqual([
      [1, "BodyParty", "Acme Corp"],
      [2, "BoxAddress", "123 Main St"],
      [3, "FooterRef", "REF-9"],
    ]);
    expect(raw).toContain("Key | Val\n--- | ---\nCity | Zurich");
  });

  it("hides textboxes anchored inside deleted or inserted runs in cleanView or original_view", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "Host prose.");
    const delBox = makeAcTextboxRunXml("<w:p><w:r><w:t>Deleted Box Text</w:t></w:r></w:p>", undefined, {
      docPrId: 21,
      shapeName: "DelBox",
    });
    const insBox = makeAcTextboxRunXml("<w:p><w:r><w:t>Inserted Box Text</w:t></w:r></w:p>", undefined, {
      docPrId: 22,
      shapeName: "InsBox",
    });
    appendFragment(
      p,
      `<w:del xmlns:w="${W_NS}" w:id="10" w:author="A" w:date="2026-11-17T09:00:00Z">${delBox}</w:del>`,
    );
    appendFragment(
      p,
      `<w:ins xmlns:w="${W_NS}" w:id="11" w:author="A" w:date="2026-11-17T09:00:00Z">${insBox}</w:ins>`,
    );

    const buf = await doc.save();
    const reloaded = await DocumentObject.load(buf);
    const raw = _extractTextFromDoc(reloaded, false, false) as string;
    const clean = _extractTextFromDoc(reloaded, true, false) as string;

    expect(raw).toContain("Deleted Box Text");
    expect(raw).toContain("Inserted Box Text");
    expect(raw).not.toContain("docx-image:");
    expect(clean).not.toContain("Deleted Box Text");
    expect(clean).toContain("Inserted Box Text");

    expect(new DocumentMapper(reloaded, false).full_text).toBe(raw);
    expect(new DocumentMapper(reloaded, true).full_text).toBe(clean);

    const origMapper = new DocumentMapper(reloaded, false, true);
    expect(origMapper.full_text).toContain("Deleted Box Text");
    expect(origMapper.full_text).not.toContain("Inserted Box Text");
  });
});
