// FILE: node/packages/core/src/repro.commented-word-granularity.test.ts
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph, addTable, setCellText } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extract_comments_data } from "./comments.js";
import { RedlineEngine } from "./engine.js";

/**
 * Port of python/tests/test_repro_commented_word_granularity.py.
 *
 * commented_change_granularity="word": a commented modify keeps the word-level
 * redline an uncommented modify gets. Its single comment spans the first
 * fragment to the last, with the range markers outside every w:ins/w:del, so
 * rejecting any one fragment cannot remove the comment's whole anchor (the
 * QA 2026-07-22 bug #1 that made "atomic" the default).
 */

const COMMENT = "Client requested a longer notice period; please confirm with legal.";
const CLAUSE =
  "Either party may terminate this Agreement upon thirty (30) days' " +
  "written notice to the other party.";

function revisionIds(xml: string): { ins: string[]; del: string[] } {
  const ins = new Set<string>();
  const del = new Set<string>();
  for (const m of xml.matchAll(/<w:ins\b[^>]*\bw:id="(\d+)"/g)) ins.add(m[1]);
  for (const m of xml.matchAll(/<w:del\b[^>]*\bw:id="(\d+)"/g)) del.add(m[1]);
  return { ins: [...ins], del: [...del] };
}

function anchoredSequence(xml: string): string[] {
  const out: string[] = [];
  for (const m of xml.matchAll(/<w:commentRangeStart w:id="\d+"\/>|<w:commentRangeEnd w:id="\d+"\/>|<w:(ins|del)\b[^>]*>/g)) {
    out.push(m[0].startsWith("<w:commentRangeStart") ? "start" : m[0].startsWith("<w:commentRangeEnd") ? "end" : m[1]);
  }
  return out;
}

function commentTexts(doc: DocumentObject): string[] {
  return Object.values(extract_comments_data(doc.pkg)).map((c: any) => c.text);
}

async function applyTo(
  build: (doc: DocumentObject) => void,
  edits: any[],
  granularity: "atomic" | "word" | null = "word",
): Promise<{ doc: DocumentObject; buf: Buffer; res: any }> {
  const doc = await createTestDocument();
  build(doc);
  const engine = new RedlineEngine(doc, "Reviewer", granularity ? { commented_change_granularity: granularity } : {});
  const res = engine.process_batch(edits);
  const buf = await doc.save();
  return { doc: await DocumentObject.load(buf), buf, res };
}

const commentedClause = () =>
  applyTo((d) => addParagraph(d, CLAUSE), [
    { type: "modify", target_text: "thirty (30) days'", new_text: "sixty (60) days'", comment: COMMENT },
  ]);

describe("commented_change_granularity", () => {
  it("defaults to atomic: one deletion and one insertion", async () => {
    const { doc } = await applyTo((d) => addParagraph(d, CLAUSE), [
      { type: "modify", target_text: "thirty (30) days'", new_text: "sixty (60) days'", comment: COMMENT },
    ], null);
    const { ins, del } = revisionIds(doc.element.toString());
    expect([ins.length, del.length]).toEqual([1, 1]);
  });

  it("refuses an unknown granularity", async () => {
    const doc = await createTestDocument();
    expect(() => new RedlineEngine(doc, "Reviewer", { commented_change_granularity: "sentence" as any })).toThrow(
      /commented_change_granularity/,
    );
  });

  it("word-splits a commented modify under one comment", async () => {
    const { doc } = await commentedClause();
    const xml = doc.element.toString();
    const { ins, del } = revisionIds(xml);
    expect(ins.length).toBeGreaterThanOrEqual(2);
    expect(del.length).toBeGreaterThanOrEqual(2);
    expect(commentTexts(doc)).toEqual([COMMENT]);
    const seq = anchoredSequence(xml);
    expect(seq[0]).toBe("start");
    expect(seq[seq.length - 1]).toBe("end");
    expect(seq.filter((t) => t === "start").length).toBe(1);
  });

  for (const position of ["first", "last"] as const) {
    it(`keeps the comment when the ${position} fragment is rejected`, async () => {
      const { doc, buf } = await commentedClause();
      const { ins, del } = revisionIds(doc.element.toString());
      const ids = [...new Set([...ins, ...del])].map(Number).sort((a, b) => a - b);
      const target = position === "first" ? ids[0] : ids[ids.length - 1];

      const reloaded = await DocumentObject.load(buf);
      const res = new RedlineEngine(reloaded, "Reviewer").process_batch([
        { type: "reject", target_id: `Chg:${target}` } as any,
      ]);
      const after = await DocumentObject.load(await reloaded.save());

      expect(res.actions_applied).toBe(1);
      expect(commentTexts(after)).toEqual([COMMENT]);
      expect(res.skipped_details.join("\n").toLowerCase()).not.toContain("removed");
    });
  }

  it("spans every paragraph of a multi-paragraph modify", async () => {
    const first = "The Supplier shall deliver the goods within ten days.";
    const second = "The Customer shall pay within thirty days of delivery.";
    const { doc, res } = await applyTo(
      (d) => {
        addParagraph(d, first);
        addParagraph(d, second);
      },
      [
        {
          type: "modify",
          target_text: `${first}\n\n${second}`,
          new_text: `${first.replace("ten", "five")}\n\n${second.replace("thirty", "forty-five")}`,
          comment: "Faster delivery, longer payment.",
        },
      ],
    );
    const paragraphs = doc.element.toString().match(/<w:p[ >][\s\S]*?<\/w:p>/g) || [];
    const withText = paragraphs.filter((p) => p.includes("w:ins") || p.includes("w:del"));

    expect(res.edits_applied).toBe(1);
    expect(withText[0]).toContain("commentRangeStart");
    expect(withText[0]).not.toContain("commentRangeEnd");
    expect(withText[1]).toContain("commentRangeEnd");
    expect(commentTexts(doc)).toEqual(["Faster delivery, longer payment."]);
  });

  it("word-splits a table-cell modify under one comment", async () => {
    const { doc } = await applyTo(
      (d) => {
        const table = addTable(d, 1, 2);
        setCellText(table, 0, 0, "Fee: one hundred dollars");
        setCellText(table, 0, 1, "Due: on signing");
      },
      [{ type: "modify", target_text: "Fee: one hundred dollars", new_text: "Fee: two hundred US dollars", comment: "Rate card." }],
    );
    const xml = doc.element.toString();
    expect(revisionIds(xml).ins.length).toBeGreaterThanOrEqual(2);
    expect(commentTexts(doc)).toEqual(["Rate card."]);
    expect(anchoredSequence(xml)[0]).toBe("start");
  });

  it("gives every match_mode=all occurrence its own comment", async () => {
    const { doc } = await applyTo(
      (d) => {
        addParagraph(d, "Acme Corp shall pay.");
        addParagraph(d, "Acme Corp shall deliver.");
      },
      [{ type: "modify", target_text: "Acme Corp", new_text: "Acme Holdings Inc", comment: "Name change.", match_mode: "all" }],
    );
    expect(commentTexts(doc)).toEqual(["Name change.", "Name change."]);
  });
});
