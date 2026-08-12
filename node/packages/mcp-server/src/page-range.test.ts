import { describe, it, expect } from "vitest";
import { paginate } from "@adeu/core";
import {
  build_page_range_response,
  build_paginated_response,
} from "./response-builders.js";

function makeBodyWithPages(targetPages: number): string {
  const filler =
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.";
  // Each paragraph is ~450 chars. 40 paragraphs = ~18,000 chars = 1 page.
  const paragraphs: string[] = [];
  for (let i = 0; i < targetPages * 40; i++) {
    paragraphs.push(`Paragraph ${i + 1}: ${filler}`);
  }
  return paragraphs.join("\n\n");
}

describe("build_page_range_response — unit tests", () => {
  it("1. mid-range: (2, 4) renders pages 2, 3, 4 in order with banners and no page 1 banner", () => {
    const body = makeBodyWithPages(5);
    const res = build_page_range_response(body, 2, 4, "/fixtures/x.docx");
    const text = res.content[0].text;

    expect(text).toContain("> **Page 2 of 5**");
    expect(text).toContain("> **Page 3 of 5**");
    expect(text).toContain("> **Page 4 of 5**");
    expect(text).not.toContain("> **Page 1 of 5**");
    expect(text).not.toContain("> **Page 5 of 5**");

    const idx2 = text.indexOf("> **Page 2 of 5**");
    const idx3 = text.indexOf("> **Page 3 of 5**");
    const idx4 = text.indexOf("> **Page 4 of 5**");
    expect(idx2).toBeLessThan(idx3);
    expect(idx3).toBeLessThan(idx4);
  });

  it("2. cap: >=12-page body with (1, 12) renders exactly 8 pages and appends cap note", () => {
    const body = makeBodyWithPages(12);
    const res = build_page_range_response(body, 1, 12, "/fixtures/x.docx");
    const text = res.content[0].text;

    expect(text).toContain("> **Page 1 of ");
    expect(text).toContain("> **Page 8 of ");
    expect(text).not.toContain("> **Page 9 of ");
    expect(text).toContain(
      '> **Range capped at 8 pages.** Continue with `page="9-12"`.',
    );
  });

  it("3. early stop: 5-page body with (4, 20) renders pages 4-5 and appends early-stop note", () => {
    const body = makeBodyWithPages(5);
    const res = build_page_range_response(body, 4, 20, "/fixtures/x.docx");
    const text = res.content[0].text;

    expect(text).toContain("> **Page 4 of 5**");
    expect(text).toContain("> **Page 5 of 5**");
    expect(text).not.toContain("> **Page 3 of 5**");
    expect(text).toContain(
      "> **[range stopped at page 5: the document has 5 page(s)]**",
    );
  });

  it("4. cap note absent when last === end; early-stop note absent when end <= total_pages", () => {
    const body = makeBodyWithPages(5);
    const resExact = build_page_range_response(body, 2, 5, "/fixtures/x.docx");
    const textExact = resExact.content[0].text;

    expect(textExact).not.toContain("Range capped at");
    expect(textExact).not.toContain("[range stopped at page");

    const resPartial = build_page_range_response(body, 2, 4, "/fixtures/x.docx");
    const textPartial = resPartial.content[0].text;

    expect(textPartial).not.toContain("Range capped at");
    expect(textPartial).not.toContain("[range stopped at page");
  });

  it("5. start < 1 throws 'Invalid page number 0: page numbers must be positive integers.'", () => {
    const body = makeBodyWithPages(5);
    expect(() =>
      build_page_range_response(body, 0, 4, "/fixtures/x.docx"),
    ).toThrow("Invalid page number 0: page numbers must be positive integers.");
  });

  it("6. start > end throws 'end page (2) cannot be less than start page (6)'", () => {
    const body = makeBodyWithPages(5);
    expect(() =>
      build_page_range_response(body, 6, 2, "/fixtures/x.docx"),
    ).toThrow("end page (2) cannot be less than start page (6)");
  });

  it("7. start > total_pages throws 'Page 9 out of range (doc has 5 pages).'", () => {
    const body = makeBodyWithPages(5);
    expect(() =>
      build_page_range_response(body, 9, 12, "/fixtures/x.docx"),
    ).toThrow("Page 9 out of range (doc has 5 pages).");
  });

  it("8. appendix pointer: body with appendix marker yields trailing appendix pointer", () => {
    const bodyNoApp = makeBodyWithPages(5);
    const bodyWithApp = `${bodyNoApp}\n\n<!-- READONLY_BOUNDARY_START -->\n\n# Appendix\nDefined terms here.`;

    const resNoApp = build_page_range_response(bodyNoApp, 1, 2, "/fixtures/x.docx");
    expect(resNoApp.content[0].text).not.toContain("Appendix available");

    const resWithApp = build_page_range_response(
      bodyWithApp,
      1,
      2,
      "/fixtures/x.docx",
    );
    expect(resWithApp.content[0].text).toContain("Appendix available");
    expect(resWithApp.content[0].text).toContain("mode='appendix'");
  });

  it("9. no_chrome: true renders [p2/5] markers, no File-Path, no cap/appendix notes", () => {
    const body = `${makeBodyWithPages(5)}\n\n<!-- READONLY_BOUNDARY_START -->\n\nAppendix`;
    const res = build_page_range_response(
      body,
      2,
      4,
      "/fixtures/x.docx",
      undefined,
      true,
    );
    const text = res.content[0].text;

    expect(text).toContain("[p2/5]\n\n");
    expect(text).toContain("[p3/5]\n\n");
    expect(text).toContain("[p4/5]\n\n");
    expect(text).not.toContain("> **File Path:**");
    expect(text).not.toContain("synthetic page");
    expect(text).not.toContain("Appendix available");
  });

  it("10. token parity: range response (2-4) is shorter than three single calls, each page content byte-identical", () => {
    const body = makeBodyWithPages(5);
    const resRange = build_page_range_response(body, 2, 4, "/fixtures/x.docx");
    const textRange = resRange.content[0].text;

    const resP2 = build_paginated_response(body, 2, "/fixtures/x.docx");
    const resP3 = build_paginated_response(body, 3, "/fixtures/x.docx");
    const resP4 = build_paginated_response(body, 4, "/fixtures/x.docx");

    const textConcat = [
      resP2.content[0].text,
      resP3.content[0].text,
      resP4.content[0].text,
    ].join("\n\n");

    expect(textRange.length).toBeLessThan(textConcat.length);

    const pagination = paginate(body, "");
    for (let p = 2; p <= 4; p++) {
      const pageContent = pagination.pages[p - 1].page_content;
      expect(textRange).toContain(pageContent);
      expect(
        resP2.content[0].text.includes(pageContent) ||
          resP3.content[0].text.includes(pageContent) ||
          resP4.content[0].text.includes(pageContent),
      ).toBe(true);
    }
  });
});
