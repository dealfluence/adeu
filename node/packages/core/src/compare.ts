/**
 * One-step comparison: the difference between two DOCX packages written as a
 * tracked-changes redline of the original. Mirrors python/src/adeu/compare.py.
 *
 * Transactional: a computed change that cannot be applied throws and no bytes
 * are returned. A partial redline silently shows the skipped passages as
 * unchanged, the false-success shape `process_document_batch` and sanitize
 * already refuse. Refusing an output path that aliases an input is the
 * caller's job (the MCP server owns the filesystem).
 */
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { _extractTextFromDoc } from "./ingest.js";
import { generate_structured_edits, collect_media_difference_warnings } from "./diff.js";

export class ComparisonError extends Error {
  details: string[];

  constructor(message: string, details: string[] = []) {
    super(message);
    this.name = "ComparisonError";
    this.details = details;
  }
}

export interface RedlineComparison {
  data: Buffer;
  applied: number;
  warnings: string[];
}

export async function build_redline_comparison(
  original: Buffer,
  modified: Buffer,
  author: string,
  compare_clean = true,
): Promise<RedlineComparison> {
  // Appendix excluded: its generated text is not writable content (QA 2026-07-18 H1).
  const extract = async (buf: Buffer) =>
    _extractTextFromDoc(await DocumentObject.load(buf), compare_clean, false, false, true) as any;
  const { text: t1, structure: s1 } = await extract(original);
  const { text: t2, structure: s2 } = await extract(modified);
  const { edits, warnings } = generate_structured_edits(t1, s1, t2, s2);

  const doc = await DocumentObject.load(original);
  const engine = new RedlineEngine(doc, author);
  const [applied, skipped] = engine.apply_edits(edits as any);
  if (skipped > 0) {
    throw new ComparisonError(
      `${skipped} of ${edits.length} computed change(s) could not be written as tracked changes, so no ` +
        "redline was saved: a partial redline would show those passages as unchanged.",
      [...engine.skipped_details],
    );
  }
  return {
    data: await doc.save(),
    applied,
    warnings: [
      ...warnings,
      ...collect_media_difference_warnings(new Uint8Array(original), new Uint8Array(modified)),
    ],
  };
}
