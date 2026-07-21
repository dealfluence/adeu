import { describe, it, expect } from "vitest";
import { build_search_response } from "./response-builders.js";

describe("Adeu MCP QA Report - Issue 4: Unbounded search results", () => {
  it("TC 4.1: search results are capped to at most 20 matches", () => {
    // Construct a document with 50 matches for "fox"
    const body = Array(50)
      .fill("The quick brown fox jumps over the lazy dog.")
      .join("\n\n");

    const res = build_search_response(
      body,
      "fox",
      false,
      true,
      undefined,
      "doc.docx",
    );

    const md = res.structuredContent!.markdown as string;

    // We count how many "Match N" headers are in the returned markdown
    const matches = md.match(/### Match \d+/g) || [];

    // Under current implementation, matches.length is 50.
    // It should be capped at 20.
    expect(matches.length).toBeLessThanOrEqual(20);
  });
});
