// FILE: node/packages/core/src/text-revision.ts
//
// Whole-text revision: diff the document's CLEAN view against the text the
// caller supplies, turn the difference into tracked changes, and refuse to
// hand back a document whose clean view does not then read exactly like that
// text. Port of python/src/adeu/text_revision.py minus its CLI-only parts.
//
// DELIBERATE structural difference from the Python twin: this module does no
// file IO. It takes a loaded document and returns bytes; the caller (the MCP
// server) already owns reading, writing, and the agent-facing shape of every
// filesystem error. The verification failure therefore travels as an exception
// CARRYING the diagnostic copy's bytes and its intended path — the handler
// writes it — instead of writing the sibling itself.
import { basename, dirname, extname, join } from "node:path";

import { DocumentObject } from "./docx/bridge.js";
import { generate_edits_via_paragraph_alignment } from "./diff.js";
import { _extractTextFromDoc } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import type { ModifyText } from "./models.js";

// Only the OPEN tokens: a bare closing token is ordinary prose far more often
// than it is markup ("A ~> B", "rate++}"), and markup view never emits one
// without its opener (verifier finding, Task 15 attempt 3).
const _CRITICMARKUP_TOKENS = ["{++", "{--", "{~~", "{==", "{>>"];

const _RE_FILE_PATH_BANNER = /^> \*\*File Path:\*\*[^\n]*\n+/;
const _RE_PAGE_BANNER = /^> \*\*Page (\d+) of (\d+)\*\*[^\n]*\n+(?:---\n+)?/;
const _RE_PAGE_FOOTER =
  /\n+---\n+> \*\*Continues on page (\d+) of (\d+)\.\*\*[^\n]*\s*$/;
const _RE_APPENDIX_POINTER = /\n+---\n+> \*\*Appendix available\.\*\*[^\n]*\s*$/;

// Documents at or above this many characters use the 50% deletion budget;
// shorter ones use the higher 75% floor (see check_major_deletions).
const _MAJOR_DELETION_MIN_ORIGINAL_CHARS = 2000;

/** Base error for every text-revision refusal (Python raises ValueError). */
export class TextRevisionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TextRevisionError";
  }
}

/** Raised when clean-text post-apply verification fails. */
export class TextRevisionVerificationError extends TextRevisionError {
  constructor(
    message: string,
    public unverified_path: string,
    public output_path: string,
    public stats: Record<string, any>,
    public unverified_bytes: Uint8Array,
  ) {
    super(message);
    this.name = "TextRevisionVerificationError";
  }
}

/**
 * The effective Track Changes author: explicit name, else `ADEU_AUTHOR`, else
 * the Node engine's own default.
 *
 * Python additionally falls back to the OS account name (`getpass.getuser()`,
 * filtered against machine accounts) because its CLI has a human user. This
 * server does not, so the last resort is the engine default the Node MCP
 * schema already advertises — a declared cosmetic difference, recorded in
 * shared/conformance/README.md.
 */
function get_default_author(author?: string | null): string {
  if (author && author.trim()) return author.trim();
  const env_author = (process.env.ADEU_AUTHOR || "").trim();
  if (env_author) return env_author;
  return "Adeu AI (TS)";
}

/** Refuses revised text input if it contains CriticMarkup syntax. */
export function check_criticmarkup(text: string): void {
  if (_CRITICMARKUP_TOKENS.some((tok) => text.includes(tok))) {
    throw new TextRevisionError(
      "Revised text contains CriticMarkup tokens ({++..++}, {--..--}, {~~..~>..~~}, {==..==}, " +
        "{>>..<<}). `apply_text_revision` compares text against the document's CLEAN view, " +
        "so CriticMarkup tokens would be diffed into the document as literal prose.",
    );
  }
}

/** Strips extract header, banners, and footers from text input. */
export function strip_page_chrome(text: string): {
  text: string;
  page: number | null;
  total: number | null;
} {
  text = text.replace(_RE_FILE_PATH_BANNER, "");
  let page: number | null = null;
  let total: number | null = null;
  const banner = _RE_PAGE_BANNER.exec(text);
  if (banner) {
    page = parseInt(banner[1], 10);
    total = parseInt(banner[2], 10);
    text = text.slice(banner[0].length);
  }
  text = text.replace(_RE_APPENDIX_POINTER, "");
  const footer = _RE_PAGE_FOOTER.exec(text);
  if (footer) {
    if (page === null) page = parseInt(footer[1], 10) - 1;
    if (total === null) total = parseInt(footer[2], 10);
    text = text.slice(0, footer.index);
  }
  return { text, page, total };
}

/**
 * Refuses to silently delete the majority of a document. 2000 chars ≈ one
 * page of prose; above that, losing half the document is almost never
 * intentional. Short documents matter too (QA 2026-07-19 v8 F-12): below the
 * threshold the guard still arms, at a higher 75% floor so that deliberately
 * halving a small draft stays a one-command workflow while near-total
 * truncation requires the explicit flag.
 *
 * The budget is measured in CHARACTERS only: a document made of many short
 * paragraphs legitimately loses dozens of them in an ordinary edit.
 * `source_name` names the revised text's origin (the CLI's text file) in the
 * refusal message when there is one.
 */
export function check_major_deletions(
  original_text: string,
  revised_text: string,
  allow_major_deletions: boolean = false,
  source_name?: string | null,
): void {
  if (allow_major_deletions) return;

  const orig_len = original_text.length;
  const rev_len = revised_text.length;
  if (orig_len === 0) return;

  const char_deletion_ratio = (orig_len - rev_len) / orig_len;
  const threshold =
    orig_len >= _MAJOR_DELETION_MIN_ORIGINAL_CHARS ? 0.5 : 0.75;
  if (char_deletion_ratio <= threshold) return;

  const subject = source_name ? `'${source_name}'` : "The revised text";
  throw new TextRevisionError(
    `${subject} is ~${Math.floor(char_deletion_ratio * 100)}% shorter than the document's clean text ` +
      `(${rev_len.toLocaleString("en-US")} vs ${orig_len.toLocaleString("en-US")} characters, ` +
      `threshold is >${Math.floor(threshold * 100)}% deletion). ` +
      "Applying it would delete the majority of the document as tracked deletions.\n" +
      "   If the text is a partial extract, re-extract the ENTIRE document with " +
      "`--page all --clean-view` and edit that.\n" +
      "   If the mass deletion is intentional, re-run with --allow-major-deletions " +
      "(over MCP: allow_major_deletions=True).",
  );
}

/** Extracts clean accepted text from a document (no generated appendix). */
function _extract_clean_text_from_doc(doc: DocumentObject): string {
  return _extractTextFromDoc(doc, true, false) as string;
}

/** Normalizes Markdown heading chrome for clean-text verification. */
function _normalize_virtual_projection_text(text: string): string {
  return text.replace(/^#+\s*/gm, "");
}

// Python's `str.isprintable()` is False for every code point in category Other
// (C) or Separator (Z), the ASCII space alone excepted — so this is the exact
// set `repr()` escapes.
const _RE_NONPRINTABLE = /\p{C}|\p{Z}/u;

/**
 * Python's `repr()` of a short string, for the divergence message. NOT
 * `JSON.stringify`: Python prefers single quotes and only switches to double
 * quotes when the text holds a single quote but no double quote, and it sizes
 * each escape to the code point (`\xNN`, `\uNNNN`, `\UNNNNNNNN`) rather than
 * always emitting `\uNNNN`. The two engines' failure text has to read
 * identically (shared/conformance/README.md), and this string is quoted from a
 * real document, so tabs, breaks (`\x0b`) and apostrophes all turn up.
 *
 * The escape set is the FULL non-printable set, not just C0/C1 (verifier
 * finding, Task 19 attempt 2): NBSP, soft hyphen and zero-width space are
 * everyday Word characters, and left raw they render the message useless —
 * both excerpts read "Fee 1000" while differing by an invisible code point,
 * which is the one thing quoting the excerpts is for.
 */
function _repr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let body = "";
  // By code point, so astral characters are escaped whole, not per surrogate.
  for (const ch of text) {
    if (ch === "\\") body += "\\\\";
    else if (ch === "\n") body += "\\n";
    else if (ch === "\r") body += "\\r";
    else if (ch === "\t") body += "\\t";
    else if (ch === quote) body += `\\${quote}`;
    else if (ch !== " " && _RE_NONPRINTABLE.test(ch)) {
      const cp = ch.codePointAt(0)!;
      const [prefix, width] =
        cp < 0x100 ? ["x", 2] : cp < 0x10000 ? ["u", 4] : ["U", 8];
      body += `\\${prefix}${cp.toString(16).padStart(width, "0")}`;
    } else body += ch;
  }
  return `${quote}${body}${quote}`;
}

/** Verifies that the clean view of `doc` matches `expected_text`. */
export function verify_clean_text(
  doc: DocumentObject,
  expected_text: string,
): [boolean, string | null] {
  const actual_clean = _extract_clean_text_from_doc(doc);
  const actual_norm = _normalize_virtual_projection_text(actual_clean.trim());
  const expected_norm = _normalize_virtual_projection_text(expected_text.trim());

  if (actual_norm !== expected_norm) {
    const shared = Math.min(actual_norm.length, expected_norm.length);
    let div = shared;
    for (let k = 0; k < shared; k++) {
      if (actual_norm[k] !== expected_norm[k]) {
        div = k;
        break;
      }
    }
    const msg =
      "Post-apply verification failed: the applied document's clean text does not match " +
      `the supplied text (first divergence at character ${div}: ` +
      `applied reads ${_repr(actual_norm.slice(div, div + 40))}, supplied text reads ` +
      `${_repr(expected_norm.slice(div, div + 40))}). The document structure could not fully realize ` +
      "the requested text (e.g. headings or table cells cannot be deleted via text replacement).";
    return [false, msg];
  }
  return [true, null];
}

/** `x.docx` → `x_redlined.docx`; an already-suffixed artifact stays in place. */
function _default_output_path(input_path: string): string {
  const stem = basename(input_path, extname(input_path));
  // Idempotency guard (parity with text_revision.py:243-246): a second pass
  // over an artifact must not compound the suffix into
  // contract_redlined_redlined.docx and fragment the agent's document state.
  if (stem.endsWith("_redlined") || stem.endsWith("_processed")) {
    return input_path;
  }
  // The extension is REPLACED, not inherited (`with_name(f"{stem}_redlined.docx")`,
  // text_revision.py:246): what gets written is a DOCX, so `contract` and
  // `contract.DOCX` both become contract_redlined.docx — inheriting the input's
  // extension would write a DOCX to an extensionless path.
  return join(dirname(input_path), `${stem}_redlined.docx`);
}

/** `<target stem>.unverified.docx`, the diagnostic sibling's path. */
function _unverified_path(output_path: string): string {
  const stem = basename(output_path, extname(output_path));
  return join(dirname(output_path), `${stem}.unverified.docx`);
}

/**
 * Core whole-text diff→tracked-changes primitive with a clean-text
 * verification gate. Returns the bytes to write; throws
 * TextRevisionVerificationError (carrying the diagnostic copy) when the
 * applied document cannot be proven to match `revised_text`.
 */
export async function apply_text_revision_core(opts: {
  doc: DocumentObject;
  input_path: string;
  revised_text: string;
  output_path?: string | null;
  author?: string | null;
  allow_major_deletions?: boolean;
}): Promise<{
  stats: Record<string, any>;
  output_path: string;
  out_bytes: Uint8Array;
  unverified?: { path: string; bytes: Uint8Array };
}> {
  const { doc, input_path, revised_text } = opts;
  const { text: text_clean_input, page, total } = strip_page_chrome(revised_text);
  if (total !== null && total > 1) {
    throw new TextRevisionError(
      `Text revision looks like page ${page || "?"} of ${total} of a paginated extract — ` +
        "it contains only part of the document, and applying it would delete every page " +
        "not present. Re-extract the ENTIRE document first with --page all --clean-view.",
    );
  }

  check_criticmarkup(text_clean_input);

  const text_orig = _extract_clean_text_from_doc(doc);

  check_major_deletions(
    text_orig,
    text_clean_input,
    opts.allow_major_deletions ?? false,
  );

  const changes: ModifyText[] = generate_edits_via_paragraph_alignment(
    text_orig,
    text_clean_input,
  );

  const engine = new RedlineEngine(doc, get_default_author(opts.author));
  const stats = engine.process_batch(changes as any);

  const target_output = opts.output_path
    ? opts.output_path
    : _default_output_path(input_path);

  const [verified, err_msg] = verify_clean_text(engine.doc, text_clean_input);

  if (!verified) {
    const unverified_path = _unverified_path(target_output);
    const unverified_bytes = await engine.doc.save();

    const full_err =
      `${err_msg} Nothing was written to '${target_output}'; a diagnostic copy was kept ` +
      `at '${unverified_path}' — it is NOT the requested document.`;
    stats.verified = false;
    stats.verification_error = full_err;
    stats.error = "verification_failed";
    stats.edits_skipped = (stats.edits_applied || 0) + (stats.edits_skipped || 0);
    stats.edits_applied = 0;
    stats.actions_skipped =
      (stats.actions_applied || 0) + (stats.actions_skipped || 0);
    stats.actions_applied = 0;
    stats.output_path = null;
    stats.unverified_output_path = unverified_path;
    if (stats.edits) {
      for (const report of stats.edits) {
        report.status = "failed";
        report.error = "Not applied: post-apply verification failed.";
        report.critic_markup = null;
        report.clean_text = null;
      }
    }

    throw new TextRevisionVerificationError(
      full_err,
      unverified_path,
      target_output,
      stats,
      unverified_bytes,
    );
  }

  const out_bytes = await engine.doc.save();
  stats.output_path = target_output;
  stats.verified = true;

  return { stats, output_path: target_output, out_bytes };
}
