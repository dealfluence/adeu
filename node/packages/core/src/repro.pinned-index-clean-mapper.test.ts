import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";
import { generate_edits_via_paragraph_alignment } from "./diff.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Reproduction for AP-05 (mirrors the Python regression test in
// python/tests/test_cli_bug_repro.py):
// Diff-generated edits carry _match_start_index pinned into the CLEAN-view
// text the diff was computed against. apply_edits' pre-resolve phase used to
// leave _active_mapper_ref unset for pinned edits, so dispatch fell back to
// the RAW-view mapper. On a document whose views differ (tracked changes,
// comments), the clean-view offset lands on the wrong runs, insertions
// degrade to the paragraph-start fallback, and the applied text reads
// "MODIFIEDTyping some..." instead of "...Typing some text MODIFIED".
// The fix binds pinned edits to the initial clean-view mapper.
describe("Pinned-index edits bind to the clean-view mapper", () => {
  it("round-trips a clean-view diff apply on a tracked-changes document (dirty_sample.docx)", async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/dirty_sample.docx",
    );
    const buf = readFileSync(fixturePath);

    // Canonical apply baseline: the CLEAN (accepted) view, no appendix —
    // the same text the CLI diffs a user's modified text file against.
    const text_orig = await extractTextFromBuffer(buf, true, false);
    expect(text_orig).toContain("Typing some text");

    const text_mod = text_orig.replaceAll(
      "Typing some text",
      "Typing some text MODIFIED",
    );

    const edits = generate_edits_via_paragraph_alignment(text_orig, text_mod);
    expect(edits.length).toBeGreaterThan(0);

    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "T");
    const stats = engine.process_batch(edits);
    expect(stats.edits_skipped).toBe(0);
    expect(stats.edits_applied).toBeGreaterThan(0);

    const out = await doc.save();
    const clean = await extractTextFromBuffer(out, true, false);

    // The post-apply verification contract: the accepted view of the applied
    // document reads exactly as the supplied text.
    expect(clean.trim()).toBe(text_mod.trim());

    // The specific raw-mapper failure mode: the insertion dropped at the
    // paragraph start instead of after its anchor.
    expect(clean).toContain("Typing some text MODIFIED");
    expect(clean).not.toContain("MODIFIEDTyping");
  });
});
