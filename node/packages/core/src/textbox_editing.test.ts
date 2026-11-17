import { unzipSync, strFromU8 } from "fflate";
import { describe, expect, it } from "vitest";
import { DocumentObject } from "./docx/bridge.js";
import { findAllDescendants, findChild, parseXml } from "./docx/dom.js";
import { BatchValidationError, RedlineEngine } from "./engine.js";
import { collectFields, fieldSummary } from "./fields.js";
import { _extractTextFromDoc, extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { finalize_document } from "./sanitize/core.js";
import { count_tracked_changes } from "./sanitize/transforms.js";
import { addParagraph, createTestDocument, findDuplicateParaIds } from "./test-utils.js";
import { apply_text_revision_core } from "./text-revision.js";

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

describe("Textbox editing, container guards, and mc:Fallback sync (issue #194)", () => {
  it("modifies text inside a textbox, syncs mc:Fallback surgically, and avoids duplicate paraIds/w:ids", async () => {
    const doc = await createTestDocument();
    const host = addParagraph(doc, "Body anchor.");
    const box1 = makeAcTextboxRunXml(
      `<w:p w14:paraId="11111111" w14:textId="22222222">` +
        `<w:r><w:t>Notice: delivery obligations are suspended during force majeure.</w:t></w:r></w:p>`,
      `<w:p w14:paraId="11111112" w14:textId="22222223">` +
        `<w:r><w:t>Notice: delivery obligations are suspended during force majeure.</w:t></w:r></w:p>`,
      { docPrId: 7, shapeName: "Box One" },
    );
    const box2 = makeAcTextboxRunXml(
      `<w:p w14:paraId="33333333" w14:textId="44444444">` +
        `<w:r><w:t>Untouched sidebar box.</w:t></w:r></w:p>`,
      `<w:p w14:paraId="33333334" w14:textId="44444445">` +
        `<w:r><w:t>Untouched sidebar box.</w:t></w:r></w:p>`,
      { docPrId: 8, shapeName: "Box Two" },
    );
    appendFragment(host, box1);
    appendFragment(host, box2);
    const buf = await doc.save();

    const loaded = await DocumentObject.load(buf);
    const eng = new RedlineEngine(loaded, "Counsel");
    const stats = eng.process_batch(
      [{ type: "modify", target_text: "force majeure", new_text: "severe weather" }],
      false,
    );
    expect(stats.edits_applied).toBe(1);
    expect(stats.document_state.tracked_changes).toBe(2);
    expect(stats.edits[0].part).toBe("textbox (word/document.xml)");

    const saved = await eng.doc.save();
    expect(findDuplicateParaIds(saved)).toEqual([]);

    const raw = await extractTextFromBuffer(saved, false, false);
    const clean = await extractTextFromBuffer(saved, true, false);
    expect(raw).toContain("{--force majeure--}{++severe weather++}");
    expect(clean).toContain("Notice: delivery obligations are suspended during severe weather.");

    const xml = strFromU8(unzipSync(saved)["word/document.xml"]);
    const root = parseXml(xml).documentElement;
    const acs = findAllDescendants(root, "mc:AlternateContent");
    expect(acs.length).toBe(2);

    const c1 = findChild(acs[0], "mc:Choice")!;
    const f1 = findChild(acs[0], "mc:Fallback")!;
    const c1Ins = findAllDescendants(c1, "w:ins");
    const c1Del = findAllDescendants(c1, "w:del");
    const f1Ins = findAllDescendants(f1, "w:ins");
    const f1Del = findAllDescendants(f1, "w:del");
    expect(c1Ins.length).toBe(1);
    expect(c1Del.length).toBe(1);
    expect(f1Ins.length).toBe(1);
    expect(f1Del.length).toBe(1);

    const choiceIds = new Set([c1Ins[0].getAttribute("w:id"), c1Del[0].getAttribute("w:id")]);
    const fallbackIds = [f1Ins[0].getAttribute("w:id"), f1Del[0].getAttribute("w:id")];
    for (const fid of fallbackIds) {
      expect(choiceIds.has(fid)).toBe(false);
    }

    // Box 2 (untouched): paraId in Fallback stayed untouched
    const f2 = findChild(acs[1], "mc:Fallback")!;
    const f2P = findAllDescendants(f2, "w:p")[0];
    expect(f2P.getAttribute("w14:paraId")).toBe("33333334");
  });

  it("enforces cross-boundary and comment restrictions on textboxes", async () => {
    const doc = await createTestDocument();
    const host = addParagraph(doc, "Body tail sentence.");
    appendFragment(
      host,
      makeAcTextboxRunXml("<w:p><w:r><w:t>First box sentence.</w:t></w:r></w:p>", undefined, {
        docPrId: 7,
        shapeName: "Box A",
      }),
    );
    appendFragment(
      host,
      makeAcTextboxRunXml("<w:p><w:r><w:t>Second box sentence.</w:t></w:r></w:p>", undefined, {
        docPrId: 8,
        shapeName: "Box B",
      }),
    );
    const rawBytes = await doc.save();

    // 1. Cross body -> textbox boundary refused
    const d1 = await DocumentObject.load(rawBytes);
    expect(() =>
      new RedlineEngine(d1, "Counsel").process_batch(
        [{ type: "modify", target_text: "Body tail sentence.\n\nFirst box sentence.", new_text: "Merged." }],
        false,
      ),
    ).toThrowError(/spans a structural document-part boundary \(body → textbox\)/);

    // 2. Cross textbox -> textbox boundary refused
    const d2 = await DocumentObject.load(rawBytes);
    expect(() =>
      new RedlineEngine(d2, "Counsel").process_batch(
        [{ type: "modify", target_text: "First box sentence.\n\nSecond box sentence.", new_text: "Merged." }],
        false,
      ),
    ).toThrowError(/spans a structural document-part boundary \(textbox → textbox\)/);

    // 3. Comment-only edit inside textbox refused
    const d3 = await DocumentObject.load(rawBytes);
    expect(() =>
      new RedlineEngine(d3, "Counsel").process_batch(
        [
          {
            type: "modify",
            target_text: "First box sentence.",
            new_text: "First box sentence.",
            comment: "Cannot anchor here.",
          },
        ],
        false,
      ),
    ).toThrowError(/comments cannot be anchored inside a textbox part/);

    // 4. Modify + comment inside textbox applies edit, drops comment with warning
    const d4 = await DocumentObject.load(rawBytes);
    const eng = new RedlineEngine(d4, "Counsel");
    const stats = eng.process_batch(
      [
        {
          type: "modify",
          target_text: "First box sentence.",
          new_text: "Revised box sentence.",
          comment: "Dropped bubble.",
        },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(1);
    expect(eng.skipped_details.some((m) => m.includes("comments inside a textbox part"))).toBe(true);
    const saved = await eng.doc.save();
    const xml = strFromU8(unzipSync(saved)["word/document.xml"]);
    expect(xml).not.toContain("commentRangeStart");
  });

  it("handles paragraph split, review actions, and protects last <w:p> floor inside w:txbxContent", async () => {
    const doc = await createTestDocument();
    const host = addParagraph(doc, "Body paragraph.");
    appendFragment(
      host,
      makeAcTextboxRunXml("<w:p><w:r><w:t>Sole boxed paragraph.</w:t></w:r></w:p>", undefined, {
        docPrId: 7,
        shapeName: "Box Sole",
      }),
    );
    const baseBytes = await doc.save();

    // Split inside textbox -> accept single Chg id syncs Fallback too
    const engSplit = new RedlineEngine(await DocumentObject.load(baseBytes), "Counsel");
    engSplit.process_batch(
      [{ type: "modify", target_text: "Sole boxed paragraph.", new_text: "First line.\n\nSecond line." }],
      false,
    );
    const splitBytes = await engSplit.doc.save();
    expect(findDuplicateParaIds(splitBytes)).toEqual([]);

    const engReview = new RedlineEngine(await DocumentObject.load(splitBytes), "Reviewer");
    const chgIds = (engReview as any)._existing_change_ids() as string[];
    expect(chgIds.length).toBeGreaterThan(0);
    engReview.process_batch(
      chgIds.map((cid) => ({ type: "accept" as const, target_id: `Chg:${cid}` })),
      false,
    );
    const reviewedBytes = await engReview.doc.save();
    const revRoot = parseXml(strFromU8(unzipSync(reviewedBytes)["word/document.xml"])).documentElement;
    expect(findAllDescendants(revRoot, "w:ins").length).toBe(0);
    expect(findAllDescendants(revRoot, "w:del").length).toBe(0);

    // Sole paragraph carrying pilcrow deletion <w:pPr><w:rPr><w:del/>: CT_TxbxContent keeps >= 1 <w:p>
    const solePilcrowDelInner =
      `<w:p>` +
      `<w:pPr><w:rPr><w:del w:id="1" w:author="A" w:date="2026-11-17T09:00:00Z"/></w:rPr></w:pPr>` +
      `<w:del w:id="2" w:author="A" w:date="2026-11-17T09:00:00Z"><w:r><w:delText>Gone</w:delText></w:r></w:del>` +
      `</w:p>`;
    const docPilcrow = await createTestDocument();
    const pPilcrow = addParagraph(docPilcrow, "Anchor.");
    appendFragment(pPilcrow, makeAcTextboxRunXml(solePilcrowDelInner));
    const engAcc = new RedlineEngine(await DocumentObject.load(await docPilcrow.save()), "Counsel");
    const counts = engAcc.accept_all_revisions();
    expect(counts.accepted_deletions).toBe(2);
    const accBytes = await engAcc.doc.save();
    const accRoot = parseXml(strFromU8(unzipSync(accBytes)["word/document.xml"])).documentElement;
    for (const tc of findAllDescendants(accRoot, "w:txbxContent")) {
      expect(findAllDescendants(tc, "w:p").length).toBeGreaterThanOrEqual(1);
    }

    // RejectChange restores both Choice and Fallback cleanly
    const engRejSetup = new RedlineEngine(await DocumentObject.load(baseBytes), "Counsel");
    engRejSetup.process_batch(
      [{ type: "modify", target_text: "Sole boxed paragraph.", new_text: "Changed boxed paragraph." }],
      false,
    );
    const rejIn = await engRejSetup.doc.save();
    const engRej = new RedlineEngine(await DocumentObject.load(rejIn), "Reviewer");
    const firstCid = ((engRej as any)._existing_change_ids() as string[])[0];
    engRej.process_batch([{ type: "reject", target_id: `Chg:${firstCid}` }], false);
    const rejOut = await engRej.doc.save();
    expect(await extractTextFromBuffer(rejOut, false, false)).toContain("Sole boxed paragraph.");
    const rejRoot = parseXml(strFromU8(unzipSync(rejOut)["word/document.xml"])).documentElement;
    expect(findAllDescendants(rejRoot, "w:ins").length).toBe(0);
    expect(findAllDescendants(rejRoot, "w:del").length).toBe(0);
  });

  it("does not double-count mc:Fallback revisions in accept_all_revisions or sanitize", async () => {
    const doc = await createTestDocument();
    const host = addParagraph(doc, "Body clause.");
    appendFragment(host, makeAcTextboxRunXml("<w:p><w:r><w:t>10 Old Road</w:t></w:r></w:p>"));
    const eng = new RedlineEngine(await DocumentObject.load(await doc.save()), "Counsel");
    eng.process_batch([{ type: "modify", target_text: "10 Old Road", new_text: "25 New Road" }], false);
    const redlinedBytes = await eng.doc.save();

    const engAcc = new RedlineEngine(await DocumentObject.load(redlinedBytes), "Counsel");
    const res = engAcc.accept_all_revisions();
    expect(res.accepted_insertions).toBe(1);
    expect(res.accepted_deletions).toBe(1);

    const sanDoc = await DocumentObject.load(redlinedBytes);
    const fin = await finalize_document(sanDoc, {
      filename: "redlined.docx",
      sanitize_mode: "full",
      accept_all: true,
    });
    expect(fin.reportText).toContain("Tracked changes auto-accepted: 2");
    expect(fin.reportText).toContain("Result: CLEAN (2 changes resolved");
    expect(fin.outBuffer).toBeDefined();
    const outDoc = await DocumentObject.load(fin.outBuffer!);
    expect(count_tracked_changes(outDoc)).toEqual([0, 0, 0]);
    const sanRoot = parseXml(strFromU8(unzipSync(fin.outBuffer!)["word/document.xml"])).documentElement;
    expect(findAllDescendants(sanRoot, "w:ins").length).toBe(0);
    expect(findAllDescendants(sanRoot, "w:del").length).toBe(0);
  });

  it("supports header textboxes, table row insertion, set_field, and apply_text_revision_core", async () => {
    const doc = await createTestDocument();
    attachHeaderOrFooter(
      doc,
      "header",
      "rIdHdrEdit1",
      "word/header1.xml",
      `<w:p><w:r><w:t>Header Banner</w:t></w:r>${makeAcTextboxRunXml(
        "<w:p><w:r><w:t>Suite 100, Old Tower</w:t></w:r></w:p>",
        undefined,
        { docPrId: 31, shapeName: "HeaderAddr" },
      )}</w:p>`,
    );
    const bodyP = addParagraph(doc, "Main body text.");
    const boxInner =
      `<w:sdt><w:sdtPr><w:alias w:val="SignerName"/><w:text/></w:sdtPr>` +
      `<w:sdtContent><w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p></w:sdtContent></w:sdt>` +
      `<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>` +
      `<w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Fee</w:t></w:r></w:p></w:tc></w:tr>` +
      `<w:tr><w:tc><w:p><w:r><w:t>Setup</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>$100</w:t></w:r></w:p></w:tc></w:tr>` +
      `</w:tbl>`;
    appendFragment(bodyP, makeAcTextboxRunXml(boxInner, undefined, { docPrId: 32, shapeName: "BodyTableBox" }));
    const baseBytes = await doc.save();

    const eng = new RedlineEngine(await DocumentObject.load(baseBytes), "Counsel");
    const stats = eng.process_batch(
      [
        { type: "modify", target_text: "Suite 100, Old Tower", new_text: "Suite 500, New Tower" },
        { type: "set_field", field: "CC:1", value: "John Smith" },
        { type: "insert_row", target_text: "Setup", position: "below", cells: ["Support", "$250"] },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(3);
    const outBytes = await eng.doc.save();
    const clean = await extractTextFromBuffer(outBytes, true, false);
    expect(clean).toContain("Suite 500, New Tower");
    expect(clean).toContain("John Smith");
    expect(clean).toContain("Support | $250");

    const origClean = await extractTextFromBuffer(baseBytes, true, false);
    const revisedClean = origClean.replace("Suite 100, Old Tower", "Suite 900, Final Tower");
    const revRes = await apply_text_revision_core({
      doc: await DocumentObject.load(baseBytes),
      input_path: "in.docx",
      revised_text: revisedClean,
      author: "Counsel",
    });
    expect(revRes.stats.edits_applied).toBeGreaterThanOrEqual(1);
    expect(await extractTextFromBuffer(revRes.out_bytes, true, false)).toContain("Suite 900, Final Tower");
  });
});
