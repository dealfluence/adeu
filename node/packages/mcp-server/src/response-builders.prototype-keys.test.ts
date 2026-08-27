import { describe, it, expect } from "vitest";
import { build_search_response } from "./response-builders.js";

describe("build_search_response: queries that collide with Object.prototype keys", () => {
  it("counts occurrences of the word 'constructor' as a number", () => {
    const body =
      "The constructor of the entity signed.\n\nA second constructor clause follows.";

    const res = build_search_response(
      body,
      "constructor",
      false,
      true,
      undefined,
      "dummy.docx",
    );
    const text = res.content[0].text;

    expect(text).toContain("Found 2 matches");
    expect(text).toContain(
      "This exact phrasing appears 2 times in the document",
    );
    expect(text).not.toContain("native code");
  });
});
