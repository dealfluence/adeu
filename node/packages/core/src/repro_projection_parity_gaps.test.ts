// FILE: node/packages/core/src/repro_projection_parity_gaps.test.ts
/**
 * Two divergences that kept the engines' projections from being byte-identical
 * on real documents. Both were found by running the FedRAMP SSP Moderate rev4
 * template (shared/corpus) through both engines and diffing; neither is
 * reachable from the synthetic fixtures the suites used before.
 *
 * 1. EMPHASIS COALESCING. Adjacent runs with identical formatting must project
 *    as ONE marker span. Both engines already elided the markers, but node
 *    tested `pending_text.endsWith(closing_marker)` against the literal tail —
 *    and boundary whitespace is hoisted OUT of the marker, so the pending group
 *    typically ends `"**A** "`, not `"**A**"`. The test therefore always missed
 *    and node emitted `**A** **B**` where python emitted `**A B**`.
 *
 * 2. HEADER/FOOTER ENUMERATION. Node listed every header/footer PART in the
 *    package. Word renders only the parts a section actually references, and
 *    python walks `w:sectPr` accordingly — honouring Link-to-Previous,
 *    `w:titlePg` and `w:evenAndOddHeaders`. Node projected orphan parts,
 *    first-page headers in sections without `w:titlePg`, and even-page headers
 *    in documents without `w:evenAndOddHeaders`.
 *
 * Both are pinned against python's output.
 */

import { describe, it, expect } from "vitest";
import { parseFastXml } from "./docx/fast-xml.js";
import {
  createTestDocument,
  addParagraph,
  attachHeaderFooter,
  enableEvenAndOddHeaders,
  corpusBuffer,
  corpusSkipReason,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';

function appendRawXml(doc: DocumentObject, xml: string): void {
  const target = doc.element.ownerDocument!;
  const build = (src: any): any => {
    if (src.nodeType === 3 || src.nodeType === 4) {
      return target.createTextNode(src.data ?? src.nodeValue ?? "");
    }
    const el = target.createElement(src.tagName);
    const attrs = src.attributes;
    if (attrs) {
      for (let i = 0; i < attrs.length; i++) {
        const a = attrs[i] ?? attrs.item?.(i);
        if (a) el.setAttribute(a.name ?? a.nodeName, a.value ?? a.nodeValue);
      }
    }
    const kids = src.childNodes ?? [];
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      if (k.nodeType === 1 || k.nodeType === 3 || k.nodeType === 4)
        el.appendChild(build(k));
    }
    return el;
  };
  doc.element.appendChild(build(parseFastXml(xml).documentElement));
}

/** A paragraph of runs, each `[text, rPrXml]`. */
function paragraphOf(runs: [string, string][]): string {
  const body = runs
    .map(
      ([t, rPr]) =>
        `<w:r>${rPr}<w:t xml:space="preserve">${t}</w:t></w:r>`,
    )
    .join("");
  return `<w:p ${NS}>${body}</w:p>`;
}

const BOLD = "<w:rPr><w:b/></w:rPr>";
const ITALIC = "<w:rPr><w:i/></w:rPr>";

async function project(xml: string[]): Promise<string> {
  const doc = await createTestDocument();
  for (const x of xml) appendRawXml(doc, x);
  return extractTextFromBuffer(await doc.save(), true, false);
}

// ---------------------------------------------------------------------------
// 1. Emphasis coalescing
// ---------------------------------------------------------------------------
describe("adjacent same-formatted runs coalesce into one emphasis span", () => {
  it("merges bold runs separated by a hoisted space", async () => {
    // The corpus shape: "**Name of Organization** **CSP Name...**" was wrong.
    const text = await project([
      paragraphOf([
        ["Name of Organization", BOLD],
        [" CSP Name System Connects To", BOLD],
      ]),
    ]);
    expect(text.trim()).toBe("**Name of Organization CSP Name System Connects To**");
  });

  it("merges three italic runs, the corpus header shape", async () => {
    // python: "_Version #.#,  Date_"   node before the fix:
    // "_Version_ _#.#,_  _Date_"
    const text = await project([
      paragraphOf([
        ["Version", ITALIC],
        [" #.#,", ITALIC],
        ["  Date", ITALIC],
      ]),
    ]);
    expect(text.trim()).toBe("_Version #.#,  Date_");
  });

  it("merges runs with no whitespace between them", async () => {
    // A fully-bold paragraph also trips heading detection, hence the "## "
    // prefix; the emphasis span is what matters here.
    const text = await project([paragraphOf([["A", BOLD], ["B", BOLD]])]);
    expect(text).toContain("**AB**");
    expect(text).not.toContain("**A****B**");
  });

  it("does NOT merge across differing formatting", async () => {
    const text = await project([
      paragraphOf([["bold", BOLD], [" and ", ""], ["italic", ITALIC]]),
    ]);
    expect(text.trim()).toBe("**bold** and _italic_");
  });

  it("keeps markers balanced when a whitespace-only same-style run follows", async () => {
    // The second historical fault: the closing marker was popped without
    // confirming the incoming run opens with the prefix, losing the closer.
    const text = await project([paragraphOf([["March 2012", BOLD], ["  ", BOLD]])]);
    const opens = (text.match(/\*\*/g) || []).length;
    expect(opens % 2, `unbalanced emphasis markers: ${JSON.stringify(text)}`).toBe(0);
  });

  it("keeps ingest and the mapper in agreement", async () => {
    const doc = await createTestDocument();
    appendRawXml(
      doc,
      paragraphOf([["Name of Organization", BOLD], [" CSP Name", BOLD]]),
    );
    const buf = await doc.save();
    const projected = await extractTextFromBuffer(buf, true, false);
    const mapped = new DocumentMapper(await DocumentObject.load(buf), true).full_text;
    expect(mapped).toBe(projected);
  });
});

// ---------------------------------------------------------------------------
// 2. Header / footer enumeration
// ---------------------------------------------------------------------------
describe("only header/footer parts a section references are projected", () => {
  it("projects a referenced default header and footer", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>HEAD</w:t></w:r></w:p>");
    attachHeaderFooter(doc, "footer", "<w:p><w:r><w:t>FOOT</w:t></w:r></w:p>");
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).toContain("HEAD");
    expect(text).toContain("Body text.");
    expect(text).toContain("FOOT");
  });

  it("ignores an orphan header part no section references", async () => {
    // Word never renders this; before the fix node projected it.
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    doc.pkg.addPart(
      "/word/header9.xml",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
        `<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
        `<w:p><w:r><w:t>ORPHAN HEADER</w:t></w:r></w:p></w:hdr>`,
    );
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).not.toContain("ORPHAN HEADER");
    expect(text).toContain("Body text.");
  });

  it("ignores a first-page header when the section omits w:titlePg", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>DEFAULT HEAD</w:t></w:r></w:p>");
    // Reference a "first" header WITHOUT setting w:titlePg, by attaching one
    // and then stripping the toggle the helper adds.
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>FIRST HEAD</w:t></w:r></w:p>", {
      type: "first",
    });
    const body = doc.element;
    for (let i = body.childNodes.length - 1; i >= 0; i--) {
      const c = body.childNodes[i] as any;
      if (c.nodeType === 1 && c.tagName === "w:sectPr") {
        for (let j = c.childNodes.length - 1; j >= 0; j--) {
          const t = c.childNodes[j] as any;
          if (t.nodeType === 1 && t.tagName === "w:titlePg") c.removeChild(t);
        }
      }
    }
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).toContain("DEFAULT HEAD");
    expect(text).not.toContain("FIRST HEAD");
  });

  it("projects a first-page header once the section sets w:titlePg", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>DEFAULT HEAD</w:t></w:r></w:p>");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>FIRST HEAD</w:t></w:r></w:p>", {
      type: "first",
    });
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).toContain("DEFAULT HEAD");
    expect(text).toContain("FIRST HEAD");
  });

  it("ignores an even-page header without w:evenAndOddHeaders", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>DEFAULT HEAD</w:t></w:r></w:p>");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>EVEN HEAD</w:t></w:r></w:p>", {
      type: "even",
    });
    // Drop the settings toggle the helper enabled.
    const settings = doc.pkg.getPartByPath("word/settings.xml");
    if (settings) {
      const el = settings._element;
      for (let i = el.childNodes.length - 1; i >= 0; i--) {
        const c = el.childNodes[i] as any;
        if (c.nodeType === 1 && c.tagName === "w:evenAndOddHeaders") el.removeChild(c);
      }
    }
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).toContain("DEFAULT HEAD");
    expect(text).not.toContain("EVEN HEAD");
  });

  it("projects an even-page header once the document opts in", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>DEFAULT HEAD</w:t></w:r></w:p>");
    attachHeaderFooter(doc, "header", "<w:p><w:r><w:t>EVEN HEAD</w:t></w:r></w:p>", {
      type: "even",
    });
    enableEvenAndOddHeaders(doc);
    const text = await extractTextFromBuffer(await doc.save(), true, false);
    expect(text).toContain("EVEN HEAD");
  });
});

// ---------------------------------------------------------------------------
// 3. Run-level elements that used to fall through silently
// ---------------------------------------------------------------------------
describe("run-level elements project their glyph", () => {
  it.each([
    // A real hyphen glyph: dropping it merged the words either side.
    ["noBreakHyphen", "<w:r><w:t>e</w:t><w:noBreakHyphen/><w:t>mail</w:t></w:r>", "e-mail"],
    // Absolute-position tab: separates content, like w:tab.
    [
      "ptab",
      '<w:r><w:t>A</w:t><w:ptab w:relativeTo="margin" w:alignment="left" w:leader="none"/><w:t>B</w:t></w:r>',
      "A B",
    ],
    // Optional break hint. Word shows it only when the line actually breaks,
    // so projecting nothing is CORRECT — pinned so nobody "fixes" it into a
    // visible character.
    ["softHyphen", "<w:r><w:t>co</w:t><w:softHyphen/><w:t>operate</w:t></w:r>", "cooperate"],
  ])("%s", async (_name, runXml, expected) => {
    expect((await project([`<w:p ${NS}>${runXml}</w:p>`])).trim()).toBe(expected);
  });

  it("still drops w:sym deliberately", async () => {
    // Symbol fonts map glyphs into the Unicode private-use area (Wingdings
    // F0FE is a checked box), so the code point alone does not identify the
    // character and guessing corrupts text. CC-1 owns checkbox projection and
    // needs a font-aware decision; this pins the status quo so the loss is a
    // recorded choice rather than an oversight.
    const text = await project([
      `<w:p ${NS}><w:r><w:sym w:font="Wingdings" w:char="F0FE"/></w:r></w:p>`,
    ]);
    expect(text.trim()).toBe("");
  });
});

// ---------------------------------------------------------------------------
// 4. The regression guard for all of the above: python and node must project
//    real documents identically, character for character.
//
//    This is the assertion A5.1 specifies ("Engines: python + node — identical
//    counts"). The true comparison needs both runtimes, so it lives in the
//    parity harness; these pinned counts are the node-side tripwire and are
//    the SAME numbers python pins in
//    python/tests/test_repro_projection_parity_gaps.py. Change one and you
//    must change the other, after re-running the harness.
//
//    Verified byte-identical on 2026-08-21 across 4 fixtures x 2 views and
//    4 corpus documents x 2 views (16/16), zero DocumentMapper drift.
// ---------------------------------------------------------------------------
const CORPUS_PROJECTION_SIZES: Record<string, [number, number]> = {
  // key: [raw_view_chars, clean_view_chars]
  // CC-1c moved both views by +7,762 = 3,881 checkboxes x 2, the width a
  // `w14:checkbox` gains going from a one-character ballot glyph to the
  // three-character `[ ]` token. No emphasis markers are involved here,
  // unlike odot_uic_drywell below.
  // CC-17: +365 in both views. Two of this document's nine `sectPr` sit at
  // `body/sdt/sdtContent/p/pPr`, so the sections were invisible and the headers
  // they reference were never walked. The growth is header2's running-header
  // content — a part that was missing, not a duplicate of one already there.
  fedramp_ssp_rev4: [621_040, 521_089],
  dau_acquisition_plan: [19_611, 17_254],
  wawd_esi_agreement: [15_978, 15_891],
  on_juries_form1: [5_505, 3_199],
  ca_talent_recruitment: [5_613, 5_109],
  // A .dotx. Absent from this table until CC-11, because python could not open
  // one at all and there was nothing to pin against; both engines project the
  // same 7,221 chars in both views (verified 2026-08-21).
  //
  // CC-1c then moved it DOWN, 7,449 -> 7,435, which looks wrong for a change
  // that widens glyphs into tokens and is not. Attributed exactly: of this
  // document's 21 ballot glyphs, 19 sit in controls and 13 of those arrived
  // wrapped in emphasis markers, projecting as `**<glyph>**`. The mark is
  // chrome, so it now carries no markers: -52 for the 13 x 4 dropped marker
  // characters, +38 for 19 x 2 of token width, net -14. The two surviving
  // glyphs are bare prose outside any control and are deliberately untouched.
  odot_uic_drywell: [7_435, 7_435],
};

describe("corpus projection sizes are pinned to the python engine", () => {
  for (const [key, [rawChars, cleanChars]] of Object.entries(CORPUS_PROJECTION_SIZES)) {
    for (const cleanView of [false, true]) {
      const expected = cleanView ? cleanChars : rawChars;
      it(`${key} ${cleanView ? "clean" : "raw"} view projects ${expected} chars`, async (ctx) => {
        const buf = corpusBuffer(key);
        if (!buf) return ctx.skip(corpusSkipReason(key));

        const text = await extractTextFromBuffer(buf, cleanView, false);
        expect(
          text.length,
          `${key} ${cleanView ? "clean" : "raw"} view projects ${text.length} chars, ` +
            `expected ${expected}. If intentional, re-run the parity harness and update ` +
            `BOTH engines' pinned values — a node-only change re-opens the divergence.`,
        ).toBe(expected);

        // No markup may reach the character stream (CC-10).
        expect(text).not.toContain("<w:");
      });
    }
  }

  for (const key of Object.keys(CORPUS_PROJECTION_SIZES)) {
    it(`${key}: ingest and the mapper agree`, async (ctx) => {
      const buf = corpusBuffer(key);
      if (!buf) return ctx.skip(corpusSkipReason(key));
      for (const cleanView of [false, true]) {
        const projected = await extractTextFromBuffer(buf, cleanView, false);
        const mapped = new DocumentMapper(
          await DocumentObject.load(buf),
          cleanView,
        ).full_text;
        expect(mapped, `${key}: mapper drifted (clean=${cleanView})`).toBe(projected);
      }
    });
  }
});

// ---------------------------------------------------------------------------
// 5 — shapes the corpus cannot reach
//
// The corpus is published documents: no tracked changes, so a whole class of
// clean-view behaviour never gets exercised by the size pins above. Both cases
// below were already correct in python and both were WRONG here — node's
// ingest and mapper were consistently wrong TOGETHER, so they agreed with each
// other and only cross-engine comparison caught them. Pinned against python's
// output.
// ---------------------------------------------------------------------------

const DEL_ATTRS = 'w:id="900" w:author="A" w:date="2026-01-01T00:00:00Z"';

async function docWithDeletedParagraphMark(): Promise<Uint8Array> {
  const doc = await createTestDocument();
  appendRawXml(doc, `<w:p ${NS}><w:r><w:t>Alpha</w:t></w:r></w:p>`);
  appendRawXml(
    doc,
    `<w:p ${NS}><w:pPr><w:rPr><w:del ${DEL_ATTRS}/></w:rPr></w:pPr>` +
      `<w:del ${DEL_ATTRS}><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>`,
  );
  appendRawXml(doc, `<w:p ${NS}><w:r><w:t>Beta</w:t></w:r></w:p>`);
  return doc.save();
}

describe("shapes the corpus cannot reach", () => {
  it("clean view drops a paragraph whose mark is deleted", async () => {
    const buf = await docWithDeletedParagraphMark();

    // Accepting a paragraph-mark deletion merges the paragraph away. When
    // nothing visible survives inside it the accepted view must render no
    // container at all — not an empty one. An empty container costs a whole
    // "\n\n" block separator, so the bug showed up as a doubled blank line
    // ("Alpha\n\n\n\nBeta").
    const clean = await extractTextFromBuffer(buf, true, false);
    expect(clean, "clean view kept an empty container for a deleted mark").toBe(
      "Alpha\n\nBeta",
    );

    // The raw view still shows the deletion — this is a clean-view-only skip.
    const raw = await extractTextFromBuffer(buf, false, false);
    expect(raw).toContain("{--gone--}");

    for (const cleanView of [false, true]) {
      const projected = await extractTextFromBuffer(buf, cleanView, false);
      const mapped = new DocumentMapper(
        await DocumentObject.load(buf),
        cleanView,
      ).full_text;
      expect(mapped, `mapper drifted (clean=${cleanView})`).toBe(projected);
    }
  });

  it("an empty styled run contributes no style markers", async () => {
    // A bold run whose only child is a footnote reference or a drawing would
    // otherwise leave a dangling "****" pair that the reader never emits,
    // since apply_formatting_to_segments("") is "".
    const doc = await createTestDocument();
    appendRawXml(
      doc,
      `<w:p ${NS}><w:r><w:rPr><w:b/></w:rPr><w:footnoteReference w:id="2"/></w:r>` +
        `<w:r><w:rPr><w:b/></w:rPr><w:t>Visible</w:t></w:r></w:p>`,
    );
    const buf = await doc.save();

    const text = await extractTextFromBuffer(buf, false, false);
    expect(text, `empty styled run emitted a dangling marker pair`).not.toContain(
      "****",
    );

    const mapped = new DocumentMapper(await DocumentObject.load(buf), false)
      .full_text;
    expect(mapped, "mapper drifted from ingest").toBe(text);
  });
});

