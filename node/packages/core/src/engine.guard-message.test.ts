import { describe, it, expect } from "vitest";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { createTestDocumentWithComments as buildDoc, WORD_XMLNS } from "./test-utils.js";

function runBatch(engine: RedlineEngine, edits: any[]) {
  try {
    const res = engine.process_batch(edits);
    return { edits_applied: res.edits_applied, errors: [] as string[] };
  } catch (err: any) {
    if (err instanceof BatchValidationError) {
      return { edits_applied: 0, errors: err.errors };
    }
    throw err;
  }
}

const approxTokens = (text: string) => Math.floor(text.length / 4);

describe("Task 15 — C1: multi-author guard message", () => {
  it("1. A straddling modify fails with exact refusal message", async () => {
    const body = `
      <w:p>
        <w:r><w:t xml:space="preserve">Prefix text. </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
        <w:r><w:t xml:space="preserve"> suffix text.</w:t></w:r>
      </w:p>`;
    const doc = await buildDoc(body);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "text. INSERTED",
        new_text: "replacement",
      },
    ]);
    expect(res.edits_applied).toBe(0);
    expect(res.errors.length).toBe(1);
    expect(res.errors[0]).toBe(
      '- Edit 1 Failed: Modification targets an active insertion from another author (Jane Doe (e.g. Chg:1)). Accept first with {"type": "accept", "target_id": "Chg:1"} or scope your edit outside of it.',
    );
  });

  it("2. match_mode: 'all' wholly inside a foreign insertion gets extra advice", async () => {
    const body = `
      <w:p>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>`;
    const doc = await buildDoc(body);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "INSERTED",
        new_text: "replacement",
        match_mode: "all",
      },
    ]);
    expect(res.edits_applied).toBe(0);
    expect(res.errors.length).toBe(1);
    expect(res.errors[0]).toBe(
      '- Edit 1 Failed: Modification targets an active insertion from another author (Jane Doe (e.g. Chg:1)). Accept first with {"type": "accept", "target_id": "Chg:1"} or use match_mode="strict" or "first", or scope your edit outside of it.',
    );
  });

  it("3. Two ids from one author render as (e.g. Chg:1, Chg:3) and a second author adds (+1 more)", async () => {
    // 3a. Two ids from Jane Doe
    const bodyA = `
      <w:p>
        <w:r><w:t xml:space="preserve">Prefix </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INS1</w:t></w:r></w:ins>
        <w:r><w:t xml:space="preserve"> middle </w:t></w:r>
        <w:ins w:id="3" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INS3</w:t></w:r></w:ins>
      </w:p>`;
    const docA = await buildDoc(bodyA);
    const engineA = new RedlineEngine(docA, "Adeu AI (TS)");
    const resA = runBatch(engineA, [
      {
        type: "modify",
        target_text: "Prefix INS1 middle INS3",
        new_text: "replacement",
      },
    ]);
    expect(resA.errors[0]).toContain("(Jane Doe (e.g. Chg:1, Chg:3))");

    // 3b. Jane Doe (Chg:1, Chg:3) + John Doe (Chg:2)
    // Sorted authors: ["Jane Doe", "John Doe"]. First is "Jane Doe".
    const bodyB = `
      <w:p>
        <w:r><w:t xml:space="preserve">Prefix </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t xml:space="preserve">INS1 </w:t></w:r></w:ins>
        <w:ins w:id="2" w:author="John Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t xml:space="preserve">INS2 </w:t></w:r></w:ins>
        <w:ins w:id="3" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INS3</w:t></w:r></w:ins>
      </w:p>`;
    const docB = await buildDoc(bodyB);
    const engineB = new RedlineEngine(docB, "Adeu AI (TS)");
    const resB = runBatch(engineB, [
      {
        type: "modify",
        target_text: "Prefix INS1 INS2 INS3",
        new_text: "replacement",
      },
    ]);
    expect(resB.errors[0]).toContain("(Jane Doe (e.g. Chg:1, Chg:3) (+1 more))");
  });

  it("4. 400-char author name clamps author name with clamp_text and total length <= GUARD_MESSAGE_CAP (280)", async () => {
    const longAuthor = "A".repeat(400);
    const body = `
      <w:p>
        <w:r><w:t xml:space="preserve">Prefix </w:t></w:r>
        <w:ins w:id="1" w:author="${longAuthor}" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>`;
    const doc = await buildDoc(body);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "Prefix INSERTED",
        new_text: "replacement",
      },
    ]);
    expect(res.errors.length).toBe(1);
    const msg = res.errors[0];
    expect(msg.length).toBeLessThanOrEqual(280);
    expect(msg).toContain("Modification targets an active insertion from another author");
    expect(msg).toContain("Accept first with");
    expect(msg).toContain("AAA...");
  });

  it("5. Wholly-inside strict/first is still allowed (no error)", async () => {
    const body = `
      <w:p>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>`;
    const doc = await buildDoc(body);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "INSERTED",
        new_text: "REPLACED",
        match_mode: "strict",
      },
    ]);
    expect(res.edits_applied).toBe(1);
    expect(res.errors.length).toBe(0);
  });

  it("6. The comment-range guard names ids", async () => {
    const commentsXml = `
      <w:comments ${WORD_XMLNS}>
        <w:comment w:id="2" w:author="Bob Smith" w:date="2026-01-01T00:00:00Z">
          <w:p><w:r><w:t>Comment text</w:t></w:r></w:p>
        </w:comment>
      </w:comments>`;
    const body = `
      <w:p>
        <w:commentRangeStart w:id="2"/>
        <w:r><w:t>Commented</w:t></w:r>
        <w:commentRangeEnd w:id="2"/>
        <w:r><w:commentReference w:id="2"/></w:r>
      </w:p>`;
    const doc = await buildDoc(body, commentsXml);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "Commented",
        new_text: "Replaced",
        match_mode: "all",
      },
    ]);
    expect(res.edits_applied).toBe(0);
    expect(res.errors.length).toBe(1);
    expect(res.errors[0]).toBe(
      '- Edit 1 Failed: match_mode="all" would sweep through a comment range from another author (Bob Smith (e.g. Com:2)). Target the commented text deliberately with match_mode "strict" or "first", or scope your edit outside of it.',
    );
  });

  it("7. Budget: approxTokens(message) <= 70 for common single-author, single-id case", async () => {
    const body = `
      <w:p>
        <w:r><w:t xml:space="preserve">Prefix text. </w:t></w:r>
        <w:ins w:id="1" w:author="Jane Doe" w:date="2026-01-01T00:00:00Z"><w:r><w:t>INSERTED</w:t></w:r></w:ins>
      </w:p>`;
    const doc = await buildDoc(body);
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const res = runBatch(engine, [
      {
        type: "modify",
        target_text: "text. INSERTED",
        new_text: "replacement",
      },
    ]);
    const msg = res.errors[0];
    expect(approxTokens(msg)).toBeLessThanOrEqual(70);
  });
});
