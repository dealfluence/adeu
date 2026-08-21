/**
 * CC-1d / A1.6 (outline half) — anchor tokens survive chrome-stripping.
 *
 * Twin of python/tests/test_cc_anchor_chrome_protection.py. The outline's
 * emphasis stripper already protected `{#...}` tokens (QA 2026-07-23 F4) and
 * that covers CC-1's `{#cc:N}` anchors unchanged. The gap A1.6 exposed is the
 * 200-char TRUNCATION, which used to slice straight through a token and emit
 * `{#cc:` — worse than dropping it, because an agent reads a plausible target
 * that resolves to nothing.
 *
 * Driven through the public `extract_outline` rather than the module-private
 * helpers: the cut site is only reachable via a real >200-char heading, and
 * exporting internals just to test them would widen the API for no one.
 */
import { describe, it, expect } from "vitest";
import { createTestDocument } from "./test-utils.js";
import { parseFastXml } from "./docx/fast-xml.js";
import { DocumentObject } from "./docx/bridge.js";
import { extract_outline } from "./outline.js";
import { _extractTextFromDoc } from "./ingest.js";

// Local copy, as in the three sibling repro suites: this helper is duplicated
// per test file in this package rather than living in test-utils.
function appendRawXml(doc: DocumentObject, xml: string): void {
  const target = doc.element.ownerDocument!;
  const parsed = parseFastXml(xml);

  const importNode = (src: any): any => {
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
      if (k.nodeType === 1 || k.nodeType === 3 || k.nodeType === 4) {
        el.appendChild(importNode(k));
      }
    }
    return el;
  };

  doc.element.appendChild(importNode(parsed.documentElement));
}

/** A Heading 1 whose text runs past the 200-char cap, with an inline control
 *  placed so the cut lands inside its `{#cc:N}` anchor.
 *
 *  Uses `w:outlineLvl` rather than a `Heading1` pStyle: the minimal test
 *  document has no styles.xml entry for Heading1, so a style-based heading
 *  would never be recognised and every assertion here would pass vacuously. */
function headingXml(padBefore: number, padAfter = 60): string {
  const before = "X".repeat(padBefore);
  const after = "Z".repeat(padAfter);
  return `
    <w:p>
      <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
      <w:r><w:t xml:space="preserve">${before}</w:t></w:r>
      <w:sdt>
        <w:sdtPr><w:alias w:val="Party"/><w:tag w:val="party"/><w:text/></w:sdtPr>
        <w:sdtContent><w:r><w:t xml:space="preserve">ACME</w:t></w:r></w:sdtContent>
      </w:sdt>
      <w:r><w:t xml:space="preserve">${after}</w:t></w:r>
    </w:p>`;
}

/**
 * Outline entry texts via the FAST path — the one production runs.
 *
 * `extract_outline` has two heading-text derivations. The legacy path rebuilds
 * text with `build_paragraph_text`, which carries no sdt anchors at all, so it
 * satisfies A1.6 by omission. The fast path SLICES the projected body, which
 * since CC-1b contains `{#cc:N}`, and is what both MCP servers and the CLI
 * use (they pass `paragraph_offsets`). Testing the legacy path here would have
 * been a green suite over the one code path that cannot exhibit the bug.
 */
async function outlineTexts(padBefore: number): Promise<string[]> {
  const doc = await createTestDocument();
  appendRawXml(doc, headingXml(padBefore));
  const data = await doc.save();
  const loaded = await DocumentObject.load(data);
  const res = _extractTextFromDoc(loaded, false, false, true) as {
    text: string;
    paragraph_offsets: Map<any, [number, number]>;
  };
  const body = res.text;
  expect(body, "fixture must project the anchor at all").toContain("{#cc:");
  const nodes = extract_outline(
    loaded,
    body,
    [body],
    [0],
    res.paragraph_offsets,
  );
  // Guard against a vacuous pass: if the fixture stopped producing a heading,
  // every loop below would iterate zero times and the suite would go green
  // while testing nothing.
  expect(nodes.length, "fixture produced no outline entry").toBeGreaterThan(0);
  return nodes.map((n: any) => n.text ?? "");
}

/** A dangling `{#` with no closing brace — the fragment A1.6 forbids. */
const SPLIT_HEAD_RE = /\{#[^}\n]*$/;

describe("CC-1d / A1.6 — anchors survive outline chrome-stripping", () => {
  // Sweep the cut across the anchor: no offset may produce a fragment.
  for (const pad of [190, 193, 195, 197, 199, 200, 205]) {
    it(`truncation never splits an anchor (padBefore=${pad})`, async () => {
      for (const text of await outlineTexts(pad)) {
        expect(
          SPLIT_HEAD_RE.test(text),
          `split anchor (dangling opener) in ${JSON.stringify(text)}`,
        ).toBe(false);
        // A1.6: the whole token, or omitted entirely — never halved.
        if (text.includes("{#cc")) expect(text).toMatch(/\{#cc:\d+[^}]*\}/);
      }
    });
  }

  it("keeps an anchor that fits inside the cap", async () => {
    const texts = await outlineTexts(20);
    expect(texts.some((t) => t.includes("{#cc:1}"))).toBe(true);
  });

  it("still truncates — the fix must not disable the 200-char cap", async () => {
    // QA 2026-07-23 F13b: an outline is a navigation map, not the document.
    for (const text of await outlineTexts(300)) {
      expect(text.length).toBeLessThanOrEqual(201); // 200 + the ellipsis
      expect(text.endsWith("…")).toBe(true);
    }
  });
});
