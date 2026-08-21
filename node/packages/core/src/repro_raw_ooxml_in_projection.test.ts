// FILE: node/packages/core/src/repro_raw_ooxml_in_projection.test.ts
/**
 * Parity twin of python/tests/test_repro_raw_ooxml_in_projection.py.
 *
 * Until CC-10 the engines disagreed on how a manual page break projects:
 * python emitted 22 characters of literal `<w:br w:type="page"/>` markup as a
 * deliberate in-band sentinel for its paginator, node emitted "\n". Both now
 * emit U+000C FORM FEED, the conventional plain-text page separator, so the
 * signal survives without markup in the character stream.
 *
 * Node's paginator now acts on the token too, so a manual page break starts a
 * new virtual page in both engines and their page numbers agree.
 */

import { describe, it, expect } from "vitest";
import { parseFastXml } from "./docx/fast-xml.js";
import { createTestDocument } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";
import { PAGE_BREAK_TOKEN } from "./utils/docx.js";
import { paginate } from "./pagination.js";

const NS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';

const PAGE_BREAK_RUN = `<w:p ${NS}><w:r><w:t>A</w:t><w:br w:type="page"/><w:t>B</w:t></w:r></w:p>`;
const LINE_BREAK_RUN = `<w:p ${NS}><w:r><w:t>C</w:t><w:br/><w:t>D</w:t></w:r></w:p>`;

/**
 * Appends raw XML by re-creating nodes through the target document's factory:
 * the fast-xml shim implements neither importNode nor adoptNode.
 */
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
      if (k.nodeType === 1 || k.nodeType === 3 || k.nodeType === 4) {
        el.appendChild(build(k));
      }
    }
    return el;
  };
  doc.element.appendChild(build(parseFastXml(xml).documentElement));
}

async function project(xml: string[], cleanView = true): Promise<string> {
  const doc = await createTestDocument();
  for (const x of xml) appendRawXml(doc, x);
  return extractTextFromBuffer(await doc.save(), cleanView, false);
}

describe("page breaks project as a character, never as markup", () => {
  it.each([false, true])(
    "emits no OOXML for a page break (cleanView=%s)",
    async (cleanView) => {
      const text = await project([PAGE_BREAK_RUN], cleanView);
      expect(text, `raw OOXML reached the projection: ${text}`).not.toContain("<w:");
      expect(text).not.toContain("w:br");
      expect(text.trim()).toBe(`A${PAGE_BREAK_TOKEN}B`);
    },
  );

  it("uses U+000C, matching python's PAGE_BREAK_TOKEN", async () => {
    expect(PAGE_BREAK_TOKEN).toBe("\f");
    expect((await project([PAGE_BREAK_RUN])).trim()).toBe("A\fB");
  });

  it('leaves a soft break as a newline (only w:type="page" is special)', async () => {
    expect((await project([LINE_BREAK_RUN])).trim()).toBe("C\nD");
  });

  it("keeps ingest and the mapper in agreement", async () => {
    const doc = await createTestDocument();
    appendRawXml(doc, PAGE_BREAK_RUN);
    appendRawXml(doc, LINE_BREAK_RUN);
    const buf = await doc.save();
    const projected = await extractTextFromBuffer(buf, true, false);
    const mapped = new DocumentMapper(await DocumentObject.load(buf), true).full_text;
    expect(mapped).toBe(projected);
  });
});

describe("manual page breaks start a new virtual page", () => {
  // Values pinned against the python engine, which has honoured manual breaks
  // since before the TS port existed:
  //   paginate("First page body.\fSecond page body.", "")
  //     -> 2 pages, offsets [0, 17]
  it("splits on the token and reports python's offsets", () => {
    const r = paginate(`First page body.${PAGE_BREAK_TOKEN}Second page body.`, "");
    expect(r.total_pages).toBe(2);
    expect(r.body_pages[0]).toBe("First page body.");
    expect(r.body_pages[1]).toBe("Second page body.");
    // Second page starts after the 1-char token, not 22 chars later.
    expect(r.body_page_offsets).toEqual([0, "First page body.".length + 1]);
  });

  it("does not split a document without manual breaks", () => {
    expect(paginate("Just one short body.", "").total_pages).toBe(1);
  });

  it("emits a leading break's empty page, matching python", () => {
    // Python yields TWO pages here, the first empty — a break before any
    // content still ends a page. Arguably surprising, but it is the shipped
    // behaviour and parity is the contract; pinned so neither engine drifts.
    //   paginate("\fOnly real content.", "") -> 2 pages, offsets [0, 1]
    const r = paginate(`${PAGE_BREAK_TOKEN}Only real content.`, "");
    expect(r.total_pages).toBe(2);
    expect(r.body_pages[0]).toBe("");
    expect(r.body_pages[1]).toBe("Only real content.");
    expect(r.body_page_offsets).toEqual([0, 1]);
  });
});
