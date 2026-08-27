import { describe, it, expect } from "vitest";
import { generate_edits_from_text } from "./diff.js";

// The word-level tokenizer (diff.ts _words_to_chars) memoizes token -> code in a
// dictionary keyed on raw document words. Words like "constructor" and
// "__proto__" are ordinary English/technical prose, and must not be confused
// with keys inherited from Object.prototype.
describe("diff tokenizer: document words that collide with Object.prototype keys", () => {
  it("redlines the prototype-named word itself, not the document's first token", () => {
    const original = "Alpha constructor beta.";
    const modified = "Alpha replacement beta.";

    const edits = generate_edits_from_text(original, modified);

    const edit = edits.find((e) => e.new_text.includes("replacement"));
    expect(edit).toBeDefined();
    // Bug: the deleted token decoded as token_array[0] ("Alpha"), so the engine
    // would have redlined the wrong word.
    expect(edit!.target_text).toContain("constructor");
    expect(edit!.target_text).not.toContain("Alpha");
  });

  it("keeps _match_start_index aligned when a prototype-named word precedes the edit", () => {
    const original = "The constructor shall deliver the goods.";
    const modified = "The constructor shall deliver the widgets.";

    const edits = generate_edits_from_text(original, modified);

    const edit = edits.find((e) => e.new_text.includes("widgets"));
    expect(edit).toBeDefined();
    const idx = edit!._match_start_index!;
    // The index must point at the text it claims to target.
    expect(original.slice(idx, idx + edit!.target_text.length)).toBe(
      edit!.target_text,
    );
    expect(idx).toBe(original.indexOf("goods"));
  });

  it("keeps _match_start_index aligned after a __proto__ token", () => {
    const original = "Alpha __proto__ beta.";
    const modified = "Alpha __proto__ gamma.";

    const edits = generate_edits_from_text(original, modified);

    const edit = edits.find((e) => e.new_text.includes("gamma"));
    expect(edit).toBeDefined();
    expect(edit!.target_text).toContain("beta");
    const idx = edit!._match_start_index!;
    expect(original.slice(idx, idx + edit!.target_text.length)).toBe(
      edit!.target_text,
    );
  });
});
