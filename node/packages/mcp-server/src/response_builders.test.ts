// FILE: packages/mcp-server/src/response_builders.test.ts
import { describe, it, expect } from "vitest";
import { build_search_response } from "./response-builders.js";

describe("build_search_response — page-as-document-filter semantics", () => {
  // Small body: 3 matches all in one paragraph → all on document page 1.
  function makeSmallBody(count: number): string {
    const lines: string[] = ["# Section One"];
    for (let i = 0; i < count; i++) {
      lines.push(`Paragraph ${i} mentions the TARGET_PHRASE here.`);
    }
    return lines.join("\n\n");
  }

  // Large body designed to span multiple document pages (PAGE_TARGET_CHARS=19000).
  // Each filler paragraph is ~500 chars, so ~40 paragraphs per document page.
  // We seed TARGET_PHRASE at known intervals so we can predict page distribution.
  function makeMultiPageBody(): string {
    const filler =
      "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.";
    const blocks: string[] = [];
    // ~50 paragraphs of filler before each marker, with TARGET_PHRASE injected at known places.
    for (let i = 0; i < 200; i++) {
      if (i === 10 || i === 11 || i === 12) {
        blocks.push(`Paragraph ${i}: TARGET_PHRASE appears here.`);
      } else if (i === 80) {
        blocks.push(`Paragraph ${i}: TARGET_PHRASE appears here.`);
      } else if (i === 150) {
        blocks.push(`Paragraph ${i}: TARGET_PHRASE appears here.`);
      } else {
        blocks.push(`Paragraph ${i}: ${filler}`);
      }
    }
    return blocks.join("\n\n");
  }

  it("page omitted with single-page doc: searches all, shows all matches", () => {
    const body = makeSmallBody(3);
    const res = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      undefined,
      "dummy.docx",
    );
    const text = res.content[0].text;

    expect(text).toContain("Found 3 matches");
    expect(text).toContain("### Match 1 (p1)");
    expect(text).toContain("### Match 2 (p1)");
    expect(text).toContain("### Match 3 (p1)");
    // No clamp warning, no result-pagination hint.
    expect(text).not.toContain("exceeds available result pages");
    expect(text).not.toContain("Showing page");
  });

  it("page='all' explicit: same behavior as omitting", () => {
    const body = makeSmallBody(3);
    const res = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      "all",
      "dummy.docx",
    );
    expect(res.content[0].text).toContain("Found 3 matches");
  });

  it("page omitted with multi-page doc: shows distribution summary", () => {
    const body = makeMultiPageBody();
    const res = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      undefined,
      "dummy.docx",
    );
    const text = res.content[0].text;

    expect(text).toContain("Found 5 matches");
    expect(text).toContain("Distribution across");
    expect(text).toMatch(/p\d+: \d+/);
    // Parity with Python (_response_builders.py:1010-1013): the line counts
    // the pages that HAVE hits and ends at the counts — the `Pass page=N`
    // tail the Node builder used to append is not in the authority.
    expect(text).toMatch(/> Distribution across \d+ document pages — p\d+: \d+/);
  });

  it("page=N as document-page filter: only returns matches on that page", () => {
    const body = makeMultiPageBody();

    // First find out which doc pages actually have hits by doing an "all" search.
    const allRes = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      "all",
      "dummy.docx",
    );
    const allText = allRes.content[0].text;
    // Extract the first hit page from the Match annotations.
    const firstMatchPageMatch = allText.match(/### Match 1 \(p(\d+)\)/);
    expect(firstMatchPageMatch).not.toBeNull();
    const firstHitPage = parseInt(firstMatchPageMatch![1], 10);

    // Now filter to that page.
    const res = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      firstHitPage,
      "dummy.docx",
    );
    const text = res.content[0].text;

    expect(text).toContain(`on document page ${firstHitPage}`);
    // All shown matches must be on the filtered page.
    const matchPageRegex = /### Match \d+ \(p(\d+)\)/g;
    const matches = Array.from(text.matchAll(matchPageRegex));
    expect(matches.length).toBeGreaterThan(0);
    for (const m of matches) {
      expect(parseInt(m[1], 10)).toBe(firstHitPage);
    }
  });

  it("page=N where N has no hits but query exists elsewhere: helpful message", () => {
    const body = makeMultiPageBody();
    // Find a page that has no hits. Multi-page body has hits on a few pages
    // but several pages in between have none. Page 2 should be safe if hits
    // are on early pages or much later pages.
    // First, identify a hit-less page by checking the "all" output.
    const allRes = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      "all",
      "dummy.docx",
    );
    const allText = allRes.content[0].text;
    const hitPages = new Set<number>();
    const matchPageRegex = /### Match \d+ \(p(\d+)\)/g;
    for (const m of allText.matchAll(matchPageRegex)) {
      hitPages.add(parseInt(m[1], 10));
    }

    // Find the total page count from the distribution line.
    const distMatch = allText.match(/Distribution across (\d+) document pages/);
    expect(distMatch).not.toBeNull();
    const totalPages = parseInt(distMatch![1], 10);

    // Find a page with no hits.
    let emptyPage: number | null = null;
    for (let p = 1; p <= totalPages; p++) {
      if (!hitPages.has(p)) {
        emptyPage = p;
        break;
      }
    }
    expect(emptyPage).not.toBeNull();

    const res = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      emptyPage!,
      "dummy.docx",
    );
    const text = res.content[0].text;

    expect(text).toContain(
      `No matches on document page ${emptyPage} for query \`TARGET_PHRASE\``,
    );
    expect(text).toContain("The query DOES appear elsewhere");
    expect(text).toContain("Omit `page` or pass `page='all'`");
  });

  it("page=N where N exceeds total document pages: hard error", () => {
    const body = makeSmallBody(3); // Single-page document.
    expect(() =>
      build_search_response(
        body,
        "TARGET_PHRASE",
        false,
        true,
        99,
        "dummy.docx",
      ),
    ).toThrow(/Document page 99 is out of range/);
  });

  it("invalid page values throw with actionable message", () => {
    const body = makeSmallBody(3);
    expect(() =>
      build_search_response(
        body,
        "TARGET_PHRASE",
        false,
        true,
        0,
        "dummy.docx",
      ),
    ).toThrow(/Invalid page value/);
    expect(() =>
      build_search_response(
        body,
        "TARGET_PHRASE",
        false,
        true,
        "garbage",
        "dummy.docx",
      ),
    ).toThrow(/Invalid page value/);
  });

  it("no matches anywhere: standard empty message", () => {
    const body = makeSmallBody(3);
    const res = build_search_response(
      body,
      "NONEXISTENT_TOKEN",
      false,
      true,
      undefined,
      "dummy.docx",
    );
    expect(res.content[0].text).toContain("No matches found");
  });

  it("occurrence counts are global, not filtered", () => {
    const body = makeMultiPageBody();
    // Filter to one specific page and verify the occurrence count reflects the full document.
    const allRes = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      "all",
      "dummy.docx",
    );
    const totalMatches = (allRes.content[0].text.match(/### Match /g) || [])
      .length;
    expect(totalMatches).toBe(5);

    // Now filter to the first hit page.
    const firstPageMatch = allRes.content[0].text.match(
      /### Match 1 \(p(\d+)\)/,
    );
    const firstHitPage = parseInt(firstPageMatch![1], 10);

    const filteredRes = build_search_response(
      body,
      "TARGET_PHRASE",
      false,
      true,
      firstHitPage,
      "dummy.docx",
    );
    // The "Occurrences" line should still report the global count of 5.
    expect(filteredRes.content[0].text).toContain(
      "This exact phrasing appears 5 times in the document",
    );
  });
});
