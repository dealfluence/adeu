// FILE: node/packages/core/src/repro.comment-fragment-reject.test.ts
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extract_comments_data } from "./comments.js";
import { extractTextFromBuffer } from "./ingest.js";
import { BatchValidationError, RedlineEngine } from "./engine.js";

/**
 * Port of the Python reproduction for the CLI QA report (2026-07-22).
 *
 * Bug #1: a commented `modify` that word-level diffing fragments into several
 * Chg pairs anchored the comment to only ONE fragment; rejecting that fragment
 * silently destroyed the comment (and any reply thread). Fix: a commented
 * change is not word-split — it stays one contiguous tracked change so the
 * comment wraps the whole edit, and rejecting it reverts the whole edit.
 *
 * Bug #3: accept/reject on an invalid target_id emitted a terse, dead-end
 * error. Fix: a self-service message that lists the ids that exist.
 */

const COMMENT = "Client requested a longer notice period; please confirm with legal.";
const CLAUSE =
  "Either party may terminate this Agreement upon thirty (30) days' " +
  "written notice to the other party.";

function revisionIds(xml: string): { ins: Set<string>; del: Set<string> } {
  const ins = new Set<string>();
  const del = new Set<string>();
  for (const m of xml.matchAll(/<w:ins\b[^>]*\bw:id="(\d+)"/g)) ins.add(m[1]);
  for (const m of xml.matchAll(/<w:del\b[^>]*\bw:id="(\d+)"/g)) del.add(m[1]);
  return { ins, del };
}

async function buildEdited(comment: string | null): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(doc, CLAUSE);
  const engine = new RedlineEngine(doc, "Reviewer");
  engine.process_batch(
    [
      {
        type: "modify",
        target_text: "thirty (30) days'",
        new_text: "sixty (60) days'",
        ...(comment ? { comment } : {}),
      } as any,
    ],
    false,
  );
  return await doc.save();
}

describe("commented modify stays atomic (QA 2026-07-22 bug #1)", () => {
  it("lands as a single tracked change with the comment attached", async () => {
    const buf = await buildEdited(COMMENT);
    const reloaded = await DocumentObject.load(buf);

    const { ins, del } = revisionIds(reloaded.element.toString());
    expect(ins.size).toBe(1);
    expect(del.size).toBe(1);

    const comments = extract_comments_data(reloaded.pkg);
    expect(Object.values(comments).some((c: any) => c.text.includes("Client requested"))).toBe(true);
  });

  it("rejecting the change reverts the whole edit with no fragment left", async () => {
    const buf = await buildEdited(COMMENT);
    const reloaded = await DocumentObject.load(buf);
    const { ins, del } = revisionIds(reloaded.element.toString());
    const ids = Array.from(new Set([...ins, ...del])).map((x) => parseInt(x, 10));

    for (const rid of ids) {
      const doc = await DocumentObject.load(buf);
      const engine = new RedlineEngine(doc, "Reviewer");
      const res = engine.process_batch([{ type: "reject", target_id: `Chg:${rid}` } as any], false);
      expect(res.actions_applied).toBe(1);

      const clean = await extractTextFromBuffer(await doc.save(), true);
      expect(clean).toContain("thirty (30) days'");
      expect(clean).not.toContain("sixty");
      expect(clean).not.toContain("(60)");
    }
  });

  it("flags the removed comment instead of deleting it silently", async () => {
    const buf = await buildEdited(COMMENT);
    const reloaded = await DocumentObject.load(buf);
    const { ins, del } = revisionIds(reloaded.element.toString());
    const anId = Array.from(new Set([...ins, ...del])).map((x) => parseInt(x, 10)).sort((a, b) => a - b)[0];

    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Reviewer");
    const res = engine.process_batch([{ type: "reject", target_id: `Chg:${anId}` } as any], false);

    expect(res.actions_applied).toBe(1);
    expect(res.actions_skipped).toBe(0);
    const details = res.skipped_details.join("\n");
    expect(details).toContain("Com:");
    expect(details.toLowerCase()).toContain("removed");
  });

  it("control: an uncommented modify still word-splits into fragments", async () => {
    const buf = await buildEdited(null);
    const reloaded = await DocumentObject.load(buf);
    const { ins, del } = revisionIds(reloaded.element.toString());
    expect(ins.size).toBeGreaterThanOrEqual(2);
    expect(del.size).toBeGreaterThanOrEqual(2);
  });
});

describe("invalid action id gives a self-service error (QA 2026-07-22 bug #3)", () => {
  async function errorFor(action: any): Promise<string> {
    const buf = await buildEdited(COMMENT);
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Reviewer");
    try {
      engine.process_batch([action], false);
    } catch (e) {
      if (e instanceof BatchValidationError) return e.message;
      throw e;
    }
    throw new Error("expected the action to fail");
  }

  it("reject on a missing change lists the ids that exist", async () => {
    const msg = await errorFor({ type: "reject", target_id: "Chg:99" });
    expect(msg).toContain("no tracked change with that id exists");
    expect(msg).toContain("Chg:1");
    expect(msg).toMatch(/adeu (markup|extract)/);
  });

  it("reject on a comment id flags the kind mismatch", async () => {
    const msg = await errorFor({ type: "reject", target_id: "Com:1" });
    expect(msg).toContain("reject on Com:1");
    expect(msg).toContain("comment id");
  });

  it("is not the old terse 'Target ID not found' message", async () => {
    const msg = await errorFor({ type: "reject", target_id: "Chg:99" });
    expect(msg).not.toContain("Target ID Chg:99 not found");
  });
});
