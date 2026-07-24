// FILE: node/packages/core/src/engine.snapshot.test.ts
// Rollback equivalence for the lazy transactional snapshot
// (docs/PERFORMANCE.md §5.2): blob-restored parts must reproduce the exact
// pre-batch state on every failure path the old deep-clone handled.
import { describe, it, expect } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";
import { _extractTextFromDoc } from "./ingest.js";
import { DocumentObject } from "./docx/bridge.js";

async function buildDoc() {
  const doc = await createTestDocument();
  addParagraph(doc, "Alpha paragraph about the agreement.");
  addParagraph(doc, "Beta paragraph with obligations to review.");
  const t = addTable(doc, 2, 2);
  setCellText(t, 0, 0, "Cell content one");
  // (0,1), (1,0), (1,1) stay empty -> anchor stamping occurs during mapper
  // builds, which must NOT flip parts to dirty (deterministic stamps).
  addParagraph(doc, "Gamma closing paragraph.");
  return doc;
}

const project = (doc: DocumentObject) =>
  _extractTextFromDoc(doc, false, false) as string;

describe("lazy transactional snapshot rollback", () => {
  it("failed batch on a fresh engine restores the exact pre-batch projection", async () => {
    const doc = await buildDoc();
    const before = project(doc);
    const engine = new RedlineEngine(doc, "Snap");

    expect(() =>
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "Alpha paragraph about the agreement.",
            new_text: "Alpha paragraph about the amended agreement.",
          },
          {
            type: "modify",
            target_text: "THIS TEXT DOES NOT EXIST ANYWHERE",
            new_text: "irrelevant",
          },
        ],
        false,
      ),
    ).toThrow(BatchValidationError);

    expect(project(doc)).toBe(before);

    // The engine must remain fully usable after rollback (fresh mapper over
    // re-parsed parts).
    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Beta paragraph with obligations to review.",
          new_text: "Beta paragraph with obligations to review carefully.",
        },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(1);
    // Surgical word-diff inserts only the delta ("{++ carefully++}"), so
    // assert on the ACCEPTED view where the sentence reads contiguously.
    expect(_extractTextFromDoc(doc, true, false) as string).toContain(
      "review carefully",
    );
  });

  it("rollback of batch 2 preserves the applied result of batch 1 (dirty-part clone path)", async () => {
    const doc = await buildDoc();
    const engine = new RedlineEngine(doc, "Snap");

    const s1 = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Alpha paragraph about the agreement.",
          new_text: "Alpha paragraph about the restated agreement.",
        },
      ],
      false,
    );
    expect(s1.edits_applied).toBe(1);
    const afterBatch1 = project(doc);
    expect(afterBatch1).toContain("restated");

    expect(() =>
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "Gamma closing paragraph.",
            new_text: "Gamma closing paragraph, as amended.",
          },
          {
            type: "modify",
            target_text: "NO SUCH TARGET IN THE DOCUMENT",
            new_text: "x",
          },
        ],
        false,
      ),
    ).toThrow(BatchValidationError);

    // Batch 1's tracked change must survive; batch 2 must be fully undone.
    expect(project(doc)).toBe(afterBatch1);
  });

  it("rollback removes parts added mid-batch (comments infrastructure)", async () => {
    const doc = await buildDoc();
    const engine = new RedlineEngine(doc, "Snap");
    const before = project(doc);
    const partCountBefore = doc.pkg.parts.length;
    const relCountBefore = doc.part.rels.size;

    expect(() =>
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "Beta paragraph with obligations to review.",
            new_text: "Beta paragraph with obligations to inspect.",
            comment: "Margin note that forces comments.xml creation",
          },
          {
            type: "modify",
            target_text: "ABSENT TARGET FOR TRANSACTIONAL FAILURE",
            new_text: "x",
          },
        ],
        false,
      ),
    ).toThrow(BatchValidationError);

    expect(project(doc)).toBe(before);
    expect(doc.pkg.parts.length).toBe(partCountBefore);
    expect(doc.part.rels.size).toBe(relCountBefore);

    // And the same comment edit APPLIES cleanly afterwards.
    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Beta paragraph with obligations to review.",
          new_text: "Beta paragraph with obligations to inspect.",
          comment: "Margin note that forces comments.xml creation",
        },
      ],
      false,
    );
    expect(stats.edits_applied).toBe(1);
  });

  it("dry_run leaves the document byte-identical and re-usable", async () => {
    const doc = await buildDoc();
    const engine = new RedlineEngine(doc, "Snap");
    const before = project(doc);

    const stats = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Gamma closing paragraph.",
          new_text: "Gamma closing paragraph (dry).",
        },
      ],
      true,
    );
    expect(stats.edits_applied).toBe(1);
    expect(project(doc)).toBe(before);

    const wet = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Gamma closing paragraph.",
          new_text: "Gamma closing paragraph (wet).",
        },
      ],
      false,
    );
    expect(wet.edits_applied).toBe(1);
    expect(project(doc)).toContain("(wet)");
  });

  it("rollback after an intermediate save() restores the SAVED state, not the load state", async () => {
    // save() re-baselines part.blob, so the post-save document is "clean"
    // again and batch-2's rollback takes the cheap blob-restore path — it
    // must land exactly on the saved (post-batch-1) content.
    const doc = await buildDoc();
    const engine = new RedlineEngine(doc, "Snap");
    const s1 = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Alpha paragraph about the agreement.",
          new_text: "Alpha paragraph about the renegotiated agreement.",
        },
      ],
      false,
    );
    expect(s1.edits_applied).toBe(1);
    await doc.save(); // re-baselines blobs
    const afterSave = project(doc);
    expect(afterSave).toContain("renegotiated");

    expect(() =>
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "Gamma closing paragraph.",
            new_text: "Gamma closing paragraph, amended.",
          },
          { type: "modify", target_text: "NOT PRESENT AT ALL", new_text: "x" },
        ],
        false,
      ),
    ).toThrow(BatchValidationError);

    expect(project(doc)).toBe(afterSave);

    // And the engine still applies cleanly afterwards.
    const s3 = engine.process_batch(
      [
        {
          type: "modify",
          target_text: "Gamma closing paragraph.",
          new_text: "Gamma closing paragraph, finalized.",
        },
      ],
      false,
    );
    expect(s3.edits_applied).toBe(1);
  });

  it("save/reload after rollback round-trips the pre-batch state", async () => {
    const doc = await buildDoc();
    const before = project(doc);
    const engine = new RedlineEngine(doc, "Snap");
    expect(() =>
      engine.process_batch(
        [
          {
            type: "modify",
            target_text: "Cell content one",
            new_text: "Cell content 1",
          },
          { type: "modify", target_text: "MISSING", new_text: "x" },
        ],
        false,
      ),
    ).toThrow(BatchValidationError);

    const reloaded = await DocumentObject.load(await doc.save());
    expect(project(reloaded)).toBe(before);
  });
});
