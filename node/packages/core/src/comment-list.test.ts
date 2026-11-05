/**
 * `comment` on a modify accepts a list of comments, each with its own author.
 * Each entry becomes its own comment on the same anchor; an entry without an
 * author uses the engine author. A plain string comment behaves as before. A
 * malformed comment is a validation error, not a TypeError deep in the engine.
 *
 * Python twin: python/tests/test_comment_list.py
 */
import { describe, it, expect } from "vitest";
import { strFromU8, unzipSync } from "fflate";
import { RedlineEngine } from "./engine.js";
import { apply_edits_to_markdown } from "./markup.js";
import { addParagraph, createTestDocument } from "./test-utils.js";

async function apply(comment: unknown, paragraph = "The quick brown fox.", target = "quick", newText = "slow") {
  const doc = await createTestDocument();
  addParagraph(doc, paragraph);
  const engine = new RedlineEngine(doc, "Jane Doe");
  const result: any = engine.process_batch(
    [{ type: "modify", target_text: target, new_text: newText, comment } as any],
    undefined,
    true,
  );
  const files = unzipSync(new Uint8Array(await doc.save()));
  const body = strFromU8(files["word/document.xml"]);
  const comments = files["word/comments.xml"] ? strFromU8(files["word/comments.xml"]) : "";
  return { result, body, comments };
}

function authorsByText(commentsXml: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of commentsXml.matchAll(/<w:comment\b[^>]*w:author="([^"]*)"[^>]*>[\s\S]*?<w:t[^>]*>([^<]*)<\/w:t>/g)) {
    out[m[2]] = m[1];
  }
  return out;
}

const rangeStarts = (body: string) => [...body.matchAll(/<w:commentRangeStart[^>]*w:id="(\d+)"/g)].map((m) => m[1]);

describe("comment lists", () => {
  it("write one comment per entry under its own author", async () => {
    const { comments } = await apply([
      { text: "Internal: foxes vary.", author: "Jane Doe (Internal)" },
      { text: "Vendor: please confirm.", author: "Jane Doe (Vendor)" },
    ]);
    expect(authorsByText(comments)).toEqual({
      "Internal: foxes vary.": "Jane Doe (Internal)",
      "Vendor: please confirm.": "Jane Doe (Vendor)",
    });
  });

  it("share one anchor", async () => {
    const { body } = await apply([{ text: "One" }, { text: "Two" }]);
    const starts = rangeStarts(body);
    expect(starts.length).toBe(2);
    const between = body.split(`w:id="${starts[0]}"`)[1].split(`w:id="${starts[1]}"`)[0];
    expect(between).not.toContain("<w:t");
  });

  it("use the engine author for an entry without one", async () => {
    const { comments } = await apply([{ text: "No author given" }]);
    expect(authorsByText(comments)).toEqual({ "No author given": "Jane Doe" });
  });

  it("leave a string comment unchanged", async () => {
    const { comments } = await apply("Plain comment");
    expect(authorsByText(comments)).toEqual({ "Plain comment": "Jane Doe" });
  });

  it("survive a comment-only edit", async () => {
    const s = "The Supplier shall deliver all of the ordered goods to the Customer within thirty days.";
    const { body, comments } = await apply(
      [
        { text: "Internal note", author: "A" },
        { text: "Vendor note", author: "B" },
      ],
      s,
      s,
      s,
    );
    expect(authorsByText(comments)).toEqual({ "Internal note": "A", "Vendor note": "B" });
    expect(rangeStarts(body).length).toBe(2);
  });

  it("echo the list in the report", async () => {
    const { result } = await apply([{ text: "One", author: "A" }, { text: "Two" }]);
    expect(result.edits[0].comment).toEqual([{ text: "One", author: "A" }, { text: "Two" }]);
  });

  it("join the entries in the markdown preview", () => {
    const preview = apply_edits_to_markdown("The quick brown fox.", [
      { type: "modify", target_text: "quick", new_text: "slow", comment: [{ text: "One" }, { text: "Two" }] } as any,
    ]);
    expect(preview).toContain("{>>One\n\nTwo<<}");
  });

  it("reject a malformed comment with a validation error", async () => {
    for (const bad of [42, [{ author: "A" }], [{ text: "x", author: 7 }]]) {
      const { result } = await apply(bad);
      expect(result.edits_applied).toBe(0);
      expect(result.failed[0].reason).toContain("comment");
    }
  });

  it("reject control characters inside an entry", async () => {
    const { result } = await apply([{ text: "bad\x07bell" }]);
    expect(result.edits_applied).toBe(0);
    expect(result.failed[0].reason).toContain("control character");
  });
});
