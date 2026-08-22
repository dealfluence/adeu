// FILE: node/packages/core/src/cc_checkboxes.test.ts
/**
 * CC-1c — checkbox projection (A1.8).
 *
 * A `w14:checkbox` control projects as the three-character token `[x]` or
 * `[ ]`, never as the raw ballot glyph. The python twin is
 * `python/tests/test_cc_checkboxes.py` and asserts the same strings.
 *
 * Two facts these tests encode came out of the COM reconnaissance against real
 * Word 16.0 rather than from the spec, and both are load-bearing:
 *
 * - The mark is read from `w14:checked`, not from the glyph run. Word restores
 *   `w14:checked` when a checkbox toggle is REJECTED, so the attribute is the
 *   settled value; the glyph can lag it inside a tracked change.
 * - Word writes the glyph as literal `w:t` text, not `w:sym`. That is what
 *   makes the substitution one character for one character, so no offset
 *   arithmetic anywhere has to learn about a width difference.
 *
 * The corpus supplied the trap in the bare-glyph test: of ~7,700 checkboxes
 * across ten documents every one is `w14:checkbox` and not one is ticked — but
 * `odot_uic_drywell` also carries two bare `U+2610` runs sitting in ordinary
 * prose, outside any control. A substitution keyed on the character rather
 * than on the control would invent two checkboxes there.
 */

import { describe, it, expect } from "vitest";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const CHECKED_GLYPH = "\u2612";
const UNCHECKED_GLYPH = "\u2610";

const MS_GOTHIC =
  '<w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr>';

/** A `w14:checkbox` control shaped exactly as Word writes one. */
function checkbox(sdtId: number, checked: boolean, glyph?: string): string {
  const g = glyph === undefined ? (checked ? CHECKED_GLYPH : UNCHECKED_GLYPH) : glyph;
  const val = checked ? "1" : "0";
  const content = g ? `<w:r>${MS_GOTHIC}<w:t>${g}</w:t></w:r>` : "";
  return (
    `<w:sdt><w:sdtPr><w:tag w:val="cb${sdtId}"/><w:id w:val="${sdtId}"/>` +
    `<w14:checkbox><w14:checked w14:val="${val}"/>` +
    '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>' +
    '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>' +
    `</w14:checkbox></w:sdtPr><w:sdtContent>${content}</w:sdtContent></w:sdt>`
  );
}

const para = (...fragments: string[]) => `<w:p>${fragments.join("")}</w:p>`;
const text = (s: string) => `<w:r><w:t xml:space="preserve">${s}</w:t></w:r>`;

const docBytes = (body: string) => ccFixtureBytes(undefined, body);

const project = (data: Uint8Array, cleanView = false) =>
  extractTextFromBuffer(data, cleanView, false);

async function mapped(data: Uint8Array, cleanView = false): Promise<string> {
  return new DocumentMapper(await DocumentObject.load(data), cleanView).full_text;
}

/** A1.8's fixture variant: one checked and one unchecked control. */
const bothStates = docBytes(
  para(text("Confidential: "), checkbox(301, true)) +
    para(text("Urgent: "), checkbox(302, false)),
);

describe("checkbox projection (CC-1c)", () => {
  it("A1.8 — tokens replace glyphs in both directions", async () => {
    const raw = await project(bothStates);
    expect(raw).toContain("Confidential: [x]");
    expect(raw).toContain("Urgent: [ ]");
    expect(raw).not.toContain(CHECKED_GLYPH);
    expect(raw).not.toContain(UNCHECKED_GLYPH);
  });

  for (const cleanView of [false, true]) {
    it(`ingest and the mapper agree (clean=${cleanView})`, async () => {
      // The Virtual Text contract. The whole substitution is worthless if the
      // two projections disagree by even one character, because every offset
      // the redline engine computes against mapper text would then be wrong.
      expect(await mapped(bothStates, cleanView)).toBe(
        await project(bothStates, cleanView),
      );
    });
  }

  it("checkbox tokens persist in the clean view", async () => {
    // Spec §6 — checkbox tokens are structural, like anchors, not commentary.
    // The clean view is the accepted-changes view, and an accepted document
    // still has checkboxes in it.
    const clean = await project(bothStates, true);
    expect(clean).toContain("Confidential: [x]");
    expect(clean).toContain("Urgent: [ ]");
  });

  it("the token is exactly three characters", async () => {
    // A3.8's edit surface depends on the token's width being fixed.
    const raw = await project(bothStates);
    for (const [line, token] of [
      ["Confidential: ", "[x]"],
      ["Urgent: ", "[ ]"],
    ]) {
      const start = raw.indexOf(line) + line.length;
      expect(raw.slice(start, start + 3)).toBe(token);
    }
  });

  it("the mark follows w14:checked, not the glyph", async () => {
    // Read the attribute, not the picture — the COM battery's finding. Word
    // restores `w14:checked` when a toggle is rejected, so a document can
    // legitimately hold `checked=1` while the glyph run still shows the
    // unchecked box inside a pending revision. Projecting the glyph would
    // render a confident `[ ]` over a box that is, once the review settles,
    // ticked. This fixture forces the disagreement directly.
    const data = docBytes(
      para(text("Disagreeing: "), checkbox(303, true, UNCHECKED_GLYPH)),
    );
    const raw = await project(data);
    expect(raw).toContain("Disagreeing: [x]");
    expect(raw).not.toContain(UNCHECKED_GLYPH);
  });

  it("bare glyphs outside a control are left alone", async () => {
    // The corpus trap: `odot_uic_drywell` has 21 ballot glyphs but 19
    // controls. The other two are symbol runs in ordinary prose. They are not
    // checkboxes, nothing can toggle them, and rewriting them to `[ ]` would
    // fabricate two controls in a document with 19 real ones to hide among.
    // A1.8's "no glyphs" clause is therefore scoped to control CONTENT.
    const data = docBytes(
      para(text(`See the box ${UNCHECKED_GLYPH} in the margin.`)) +
        para(text("Real: "), checkbox(304, false)),
    );
    const raw = await project(data);
    expect(raw).toContain(`See the box ${UNCHECKED_GLYPH} in the margin.`);
    expect(raw).toContain("Real: [ ]");
    expect(raw.split(UNCHECKED_GLYPH).length - 1).toBe(1);
  });

  it("an empty checkbox still projects three characters", async () => {
    // Robustness: Word always writes the glyph run, but a generator might not.
    // Falling back to a virtual mark keeps the token three characters wide
    // instead of degrading to a two-character `[]` that no edit surface
    // expects.
    const data = docBytes(para(text("Empty: "), checkbox(305, true, "")));
    expect(await project(data)).toContain("Empty: [x]");
    expect(await mapped(data)).toBe(await project(data));
  });

  it("the mark carries no emphasis markers", async () => {
    // A bold glyph run must not project `[**x**]`. The mark is chrome, not
    // prose. Emphasis on it would hand every marker-stripping pass (outline,
    // search snippets) something to mangle, which is the QA F4/F22b failure
    // class the anchor work already guards. This is not hypothetical: 13 of
    // `odot_uic_drywell`'s 19 control glyphs arrived wrapped in `**`, and
    // dropping those markers is the whole of that document's -14 char delta.
    const boldGlyph =
      '<w:r><w:rPr><w:b/><w:rFonts w:ascii="MS Gothic"/></w:rPr>' +
      `<w:t>${CHECKED_GLYPH}</w:t></w:r>`;
    const data = docBytes(
      para(
        text("Bold box: "),
        '<w:sdt><w:sdtPr><w:tag w:val="cb306"/><w:id w:val="306"/>' +
          '<w14:checkbox><w14:checked w14:val="1"/>' +
          '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>' +
          '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>' +
          `</w14:checkbox></w:sdtPr><w:sdtContent>${boldGlyph}</w:sdtContent></w:sdt>`,
      ),
    );
    const raw = await project(data);
    expect(raw).toContain("Bold box: [x]");
    expect(raw).not.toContain("**");
    expect(await mapped(data)).toBe(await project(data));
  });
});
