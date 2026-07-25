// FILE: node/packages/core/src/docx/fast_xml.test.ts
// Line-ending conformance for the purpose-built parser (docs/PERFORMANCE.md
// §5.4b). fast-xml replaced @xmldom/xmldom on the hot path, so it owes the
// engine the same XML 1.0 text semantics — and it owes the Python twin
// byte-identical projections, since lxml applies these passes too.
//
// Each expectation below was taken from BOTH reference parsers (xmldom here,
// lxml checked against the same inputs) rather than from the spec alone.
import { describe, it, expect } from "vitest";
import { DOMParser } from "@xmldom/xmldom";
import { parseFastXml, serializeFastXml } from "./fast-xml.js";

const W = 'xmlns:w="urn:w"';
const textOf = (xml: string) => (parseFastXml(xml) as any).documentElement.textContent as string;
const xmldomTextOf = (xml: string) =>
  new DOMParser().parseFromString(xml, "text/xml").documentElement!.textContent as string;

describe("fast-xml line-ending normalization (XML 1.0 §2.11)", () => {
  it("collapses a literal CRLF in text to LF", () => {
    const xml = `<w:t ${W} xml:space="preserve">a\r\nb</w:t>`;
    expect(textOf(xml)).toBe("a\nb");
    expect(textOf(xml)).toBe(xmldomTextOf(xml));
  });

  it("converts a lone literal CR in text to LF", () => {
    const xml = `<w:t ${W} xml:space="preserve">a\rb</w:t>`;
    expect(textOf(xml)).toBe("a\nb");
    expect(textOf(xml)).toBe(xmldomTextOf(xml));
  });

  it("does NOT normalize a &#13; character reference", () => {
    // Escaping is the only conformant way to carry a real CR, so the
    // normalization pass must run before entity decoding.
    const xml = `<w:t ${W}>a&#13;b</w:t>`;
    expect(textOf(xml)).toBe("a\rb");
    expect(textOf(xml)).toBe(xmldomTextOf(xml));
  });

  it("normalizes CRLF inside a CDATA section", () => {
    const xml = `<w:t ${W}><![CDATA[a\r\nb]]></w:t>`;
    expect(textOf(xml)).toBe("a\nb");
    expect(textOf(xml)).toBe(xmldomTextOf(xml));
  });

  it("leaves LF-only text untouched", () => {
    const xml = `<w:t ${W} xml:space="preserve">a\nb</w:t>`;
    expect(textOf(xml)).toBe("a\nb");
  });

  it("keeps a paragraph's text length equal to what Word sees", () => {
    // The concrete failure: a CR left in the projection made every span
    // offset after it drift by one character.
    const xml = `<w:t ${W} xml:space="preserve">line one\r\nline two</w:t>`;
    expect(textOf(xml).length).toBe("line one\nline two".length);
  });
});

describe("fast-xml attribute-value normalization (XML 1.0 §3.3.3)", () => {
  const attrOf = (xml: string) =>
    (parseFastXml(xml) as any).documentElement.getAttribute("w:v") as string;
  const xmldomAttrOf = (xml: string) =>
    new DOMParser().parseFromString(xml, "text/xml").documentElement!.getAttribute("w:v");

  it("flattens a literal CRLF in an attribute to one space", () => {
    // §2.11 turns CRLF into a single LF, which §3.3.3 then turns into ONE
    // space — not two.
    const xml = `<w:t ${W} w:v="a\r\nb">x</w:t>`;
    expect(attrOf(xml)).toBe("a b");
    expect(attrOf(xml)).toBe(xmldomAttrOf(xml));
  });

  it("flattens a literal tab in an attribute to a space", () => {
    const xml = `<w:t ${W} w:v="a\tb">x</w:t>`;
    expect(attrOf(xml)).toBe("a b");
    expect(attrOf(xml)).toBe(xmldomAttrOf(xml));
  });

  it("preserves character references in attributes", () => {
    const xml = `<w:t ${W} w:v="a&#13;b">x</w:t>`;
    expect(attrOf(xml)).toBe("a\rb");
  });

  it("leaves ordinary attributes untouched", () => {
    const xml = `<w:t ${W} w:v="plain value" xml:space="preserve">x</w:t>`;
    expect(attrOf(xml)).toBe("plain value");
  });
});

describe("fast-xml CR round-trip fidelity", () => {
  it("re-escapes a real CR so parse->serialize->parse is idempotent", () => {
    // A raw CR in the output would be normalized to LF by the next parse,
    // silently rewriting the document. lxml escapes it; so must we.
    const doc = parseFastXml(`<w:t ${W}>a&#13;b</w:t>`) as any;
    const serialized = serializeFastXml(doc.documentElement);
    expect(serialized).toContain("&#13;");
    expect(serialized).not.toMatch(/\r/);
    expect(textOf(serialized)).toBe("a\rb");
  });

  it("a normalized CRLF stays LF across a round-trip", () => {
    const doc = parseFastXml(`<w:t ${W} xml:space="preserve">a\r\nb</w:t>`) as any;
    const serialized = serializeFastXml(doc.documentElement);
    expect(textOf(serialized)).toBe("a\nb");
  });
});
