import { describe, it, expect } from "vitest";
import {
  build_paginated_response,
  build_full_document_response,
  build_appendix_response,
  render_outline_tree,
} from "./response-builders.js";
import { approxTokens } from "./conformance-utils.js";
import type { OutlineNode } from "@adeu/core";

// Generate a body text large enough to produce multiple pages (PAGE_TARGET_CHARS = 19000 per page)
function makeMultiPageText(pagesCount: number = 5): string {
  const paragraph =
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.\n\n";
  // Paragraph is 446 chars. ~42 paragraphs = 18732 chars (approx 1 page).
  let text = "";
  for (let i = 1; i <= pagesCount; i++) {
    text += `# Page ${i}\n\n` + paragraph.repeat(42);
  }
  return text;
}

const FP = "test_doc.docx";

describe("no_chrome on response builders", () => {
  it("1. build_paginated_response(..., { no_chrome: true }): output is [p2/5]\\n\\n<page_content>", () => {
    const multiPage = makeMultiPageText(5);
    const res = build_paginated_response(multiPage, 2, FP, { no_chrome: true });
    const text = res.content[0].text;

    expect(text).toMatch(/^\[p2\/5\]\n\n/);
    expect(text).not.toContain("> **File Path:**");
    expect(text).not.toContain("> **Page 2 of 5**");
    expect(text).not.toContain("Continues on page");
    expect(text).not.toContain("Appendix available");
  });

  it("1b. build_paginated_response(..., { no_chrome: true }) on single-page doc gets no marker at all", () => {
    const singlePage = "Just a short single page document text.";
    const res = build_paginated_response(singlePage, 1, FP, { no_chrome: true });
    const text = res.content[0].text;

    expect(text).toBe("Just a short single page document text.");
    expect(text).not.toContain("[p1/1]");
    expect(text).not.toContain("> **File Path:**");
  });

  it("2. Byte-identity of payload: remaining text contains page's page_content byte-identically to chromed response", () => {
    const multiPage = makeMultiPageText(5);
    const chromed = build_paginated_response(multiPage, 2, FP);
    const terse = build_paginated_response(multiPage, 2, FP, { no_chrome: true });

    const marker = "[p2/5]\n\n";
    expect(terse.content[0].text.startsWith(marker)).toBe(true);
    const terseBody = terse.content[0].text.slice(marker.length);

    // Chromed contains banner, footer, appendix pointer, file path header.
    // The page_content inside chromed must match terseBody byte-for-byte.
    expect(chromed.content[0].text).toContain(terseBody);
  });

  it("3. Token diff: approxTokens(chromed) - approxTokens(terse) >= 20 for multi-page document", () => {
    const multiPage = makeMultiPageText(5);
    const chromedText = build_paginated_response(multiPage, 2, FP).content[0].text;
    const terseText = build_paginated_response(multiPage, 2, FP, {
      no_chrome: true,
    }).content[0].text;

    const tokenDiff = approxTokens(chromedText) - approxTokens(terseText);
    expect(tokenDiff).toBeGreaterThanOrEqual(20);
  });

  it("4. build_full_document_response with no_chrome: true drops only the File-Path line", () => {
    const body = "This is full document body content.";
    const chromed = build_full_document_response(body, FP);
    const terse = build_full_document_response(body, FP, { no_chrome: true });

    expect(chromed.content[0].text).toContain("> **File Path:**");
    expect(terse.content[0].text).not.toContain("> **File Path:**");
    expect(terse.content[0].text).toBe(body);
  });

  it("5. render_outline_tree visible-empty case with no_chrome drops hint sentence", () => {
    const nodes: OutlineNode[] = [
      { text: "Deeper 1", level: 3, page: 1, end_page: 1, style: "Heading 3", has_table: false, footnote_ids: [] },
      { text: "Deeper 2", level: 4, page: 2, end_page: 2, style: "Heading 4", has_table: false, footnote_ids: [] },
    ];

    const chromedTree = render_outline_tree(nodes, 2, false, false);
    const terseTree = render_outline_tree(nodes, 2, false, true);

    expect(chromedTree).toContain("Call read_docx with mode='outline'");
    expect(terseTree).not.toContain("Call read_docx with mode='outline'");
    expect(terseTree).toBe(
      "# (No headings at level <= 2)\n\nDocument has 2 headings, all at deeper levels."
    );
  });

  it("6. build_appendix_response: matches Python behavior with no_chrome", () => {
    const appendixText =
      "Section 1\n\n<!-- READONLY_BOUNDARY_START -->\n\n## APPENDIX\n\nDefined Terms:\n- Term A: Definition A";
    const terse = build_appendix_response(appendixText, 1, FP, { no_chrome: true });
    expect(terse.content[0].text).not.toContain("> **File Path:**");
    expect(terse.content[0].text).not.toContain("> **Appendix**");
    expect(terse.content[0].text).toContain("Defined Terms:");

    const noAppendix = "Just body text";
    const terseEmpty = build_appendix_response(noAppendix, 1, FP, { no_chrome: true });
    expect(terseEmpty.content[0].text).not.toContain("> **File Path:**");
    expect(terseEmpty.content[0].text).toBe(
      "# Appendix\n\nThis document has no structural appendix (no defined terms, named anchors, or diagnostics detected)."
    );
  });
});
