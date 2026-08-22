/**
 * CC-14 — a batch that applies cleanly must produce the document it was asked
 * for. Twin of python/tests/test_repro_redline_replay_wrong_document.py.
 *
 * The Python property suite (`test_p2_json_text_roundtrip_is_exact_or_loud`)
 * found two independent defects. Only ONE of them existed here, which is the
 * interesting part and is pinned below:
 *
 *   DEFECT 1 (Python only) — Python's rstrip "Smart Fallback" inserted a
 *   paragraph-splitting replacement BEFORE the target's trailing space,
 *   stranding that space at the head of the new paragraph ("0.\n\n 0.").
 *   `@adeu/core` has no such branch and was already correct, so this was a
 *   dual-engine parity break. Pinned here so the correct behaviour cannot
 *   regress into Python's.
 *
 *   DEFECT 2 (both engines) — a paragraph mark shared by the target and the
 *   replacement reached the apply layer, which track-deletes a target's
 *   trailing mark (a genuine merge needs that) but never re-creates the one
 *   the replacement asks for. Exactly one paragraph break vanished, with the
 *   batch reporting success.
 */
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";

/** Apply one edit by text match and return the accepted clean text. */
async function applyMatched(
  paras: string[],
  target: string,
  newText: string,
  comment?: string,
): Promise<{ got: string; want: string }> {
  const doc = await createTestDocument();
  for (const p of paras) addParagraph(doc, p);
  const engine = new RedlineEngine(doc, "Fuzz");
  await engine.process_batch([
    {
      type: "modify",
      target_text: target,
      new_text: newText,
      ...(comment ? { comment } : {}),
    } as any,
  ]);
  engine.accept_all_revisions(true);
  const buf = await doc.save();
  const got = await extractTextFromBuffer(Buffer.from(buf), true);
  return { got, want: paras.join("\n\n").replace(target, newText) };
}

/** Apply one edit pinned by index, bypassing match resolution. */
async function applyPinned(
  paras: string[],
  target: string,
  newText: string,
  comment?: string,
): Promise<{ got: string; want: string }> {
  const doc = await createTestDocument();
  for (const p of paras) addParagraph(doc, p);
  const engine = new RedlineEngine(doc, "Fuzz");
  const edit: any = {
    type: "modify",
    target_text: target,
    new_text: newText,
    ...(comment ? { comment } : {}),
  };
  edit._match_start_index = paras.join("\n\n").indexOf(target);
  await engine.process_batch([edit]);
  engine.accept_all_revisions(true);
  const buf = await doc.save();
  const got = await extractTextFromBuffer(Buffer.from(buf), true);
  return { got, want: paras.join("\n\n").replace(target, newText) };
}

describe("CC-14 defect 1 — parity pin (was broken in Python only)", () => {
  it.each([["with a comment", "Diff: Replacement"], ["without one", undefined]])(
    "splits a paragraph at a space without stranding it (%s)",
    async (_label, comment) => {
      const { got } = await applyMatched(["0 0."], "0 ", "0.\n\n", comment as string | undefined);
      expect(got).toBe("0.\n\n0.");
    },
  );
});

describe("CC-14 defect 2 — a shared trailing paragraph mark survives", () => {
  it.each(["Z.\n\n", "Z.\n\nY.\n\n", "Z.\n\nY.\n\nW.\n\n"])(
    "commented, resolved by match: %j",
    async (newText) => {
      const { got, want } = await applyMatched(["A.", "00."], "A.\n\n", newText, "C");
      expect(got).toBe(want);
    },
  );

  it.each(["Z.\n\n", "Z.\n\nY.\n\n", "Z.\n\nY.\n\nW.\n\n"])(
    "caller-pinned, which skips resolution: %j",
    async (newText) => {
      const { got, want } = await applyPinned(["A.", "00."], "A.\n\n", newText, "C");
      expect(got).toBe(want);
    },
  );
});

describe("CC-14 — the shapes the fix must NOT change", () => {
  it("a genuine paragraph merge still deletes the mark", async () => {
    // Only the SHARED mark is structural context. With no mark on the
    // replacement the caller is merging two paragraphs, and the deletion is
    // the entire point of the edit.
    const { got } = await applyPinned(["A.", "00."], "A.\n\n", "Z.", "C");
    expect(got).toBe("Z.00.");
  });

  it("a shared leading mark was never affected", async () => {
    const { got, want } = await applyPinned(["0.", "A.", "00."], "\n\nA.", "\n\nZ.\n\nY.", "C");
    expect(got).toBe(want);
  });

  it("splitting a paragraph without touching its mark is unchanged", async () => {
    const { got, want } = await applyPinned(["A.", "00."], "A.", "Z.\n\nY.", "C");
    expect(got).toBe(want);
  });

  it("the separator-space shortcut still works within one paragraph", async () => {
    const { got, want } = await applyMatched(
      ["Section 1 ends here."],
      "Section 1 ",
      "Section 1 Revised ",
    );
    expect(got).toBe(want);
  });
});
