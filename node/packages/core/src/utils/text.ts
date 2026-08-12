// FILE: src/utils/text.ts
// Small text helpers shared by engine output paths. Mirrors the Python
// engine's adeu/utils/text.py so both engines bound their report output
// identically (QA C2).
import { diff_match_patch } from "diff-match-patch";

// Default cap for echoing caller-supplied strings (target_text/new_text) back
// in batch reports and error messages. Reports feed straight into LLM context
// windows via MCP, so an oversized edit value must never be reflected in full
// (QA C2: a 2MB new_text was echoed twice, unbounded, in the apply report).
export const REPORT_ECHO_CAP = 500;

// Tighter cap for the inline redline preview snippets ({--...--}{++...++}),
// which additionally carry surrounding document context.
export const PREVIEW_TEXT_CAP = 200;

// CriticMarkup delimiters that must never appear verbatim inside a {>>…<<}
// meta bubble: a comment body containing e.g. "{--del--}" would nest raw
// markup inside the annotation, and its "<<}"/"--}" terminates the outer
// bubble early for every CriticMarkup consumer — including this package's
// own preview/tidy regexes (QA round 3, findings 3.7/3.8).
const CRITIC_TOKENS = ["{++", "++}", "{--", "--}", "{==", "==}", "{>>", "<<}"];

/**
 * Defangs CriticMarkup delimiters in projection-embedded free text (comment
 * bodies) by spacing the brace/marker apart: "{>>x<<}" renders as
 * "{ >>x<< }". The content stays readable while no delimiter sequence
 * survives for a parser to misinterpret. Mirrors Python escape_critic_tokens.
 */
export function escape_critic_tokens(text: string): string {
  if (!text || (!text.includes("{") && !text.includes("}"))) return text;
  for (const token of CRITIC_TOKENS) {
    if (text.includes(token)) {
      const escaped = token.startsWith("{")
        ? "{ " + token.slice(1)
        : token.slice(0, -1) + " }";
      text = text.split(token).join(escaped);
    }
  }
  return text;
}

/**
 * Hard-caps `text` to at most `cap` characters, marking the elision with an
 * ASCII "...". Use this wherever the cap is a real ceiling: truncate_middle
 * keeps head AND tail plus a "[N chars omitted]" note, so its result
 * routinely runs LONGER than `cap` — fine for a 500-char echo budget, fatal
 * for the minimal report's per-edit token budget.
 * Mirrors python/src/adeu/utils/text.py clamp_text.
 */
export function clamp_text(text: string, cap: number): string {
  if (text.length <= cap) return text;
  return text.slice(0, Math.max(1, cap - 3)) + "...";
}

/**
 * Bounds `text` to roughly `cap` visible characters, keeping the head and
 * tail and stating how much was omitted. Returns short strings unchanged.
 */
export function truncate_middle(text: string, cap: number): string {
  if (text === null || text === undefined || text.length <= cap) return text;
  const head = Math.max(1, Math.floor((cap * 2) / 3));
  const tail = Math.max(1, cap - head);
  const omitted = text.length - head - tail;
  return `${text.slice(0, head)}… [${omitted.toLocaleString("en-US")} chars omitted] …${text.slice(-tail)}`;
}

// ---------------------------------------------------------------------------
// Typographic normalization (matcher/writer symmetry)
// ---------------------------------------------------------------------------

// The EXACT set DocumentMapper._replace_smart_quotes forgives when matching a
// target against the projection. The writer must forgive the same set and no
// more, or the two halves of one edit disagree about what the caller meant
// (BUG_comment_threading_anchoring_and_typography.md B4). Each entry maps one
// character to one character, so normalization is length-preserving — the
// alignment in restore_document_typography relies on that.
export const SMART_QUOTE_MAP: Record<string, string> = {
  "\u201c": '"', // left double quotation mark
  "\u201d": '"', // right double quotation mark
  "\u2018": "'", // left single quotation mark
  "\u2019": "'", // right single quotation mark
};

const SMART_QUOTE_RE = /[\u201c\u201d\u2018\u2019]/g;

/** Folds curly quotes/apostrophes onto their ASCII equivalents. */
export function normalize_smart_quotes(text: string): string {
  if (!text) return text;
  return text.replace(SMART_QUOTE_RE, (ch) => SMART_QUOTE_MAP[ch]);
}

/** True when `text` carries at least one curly quote/apostrophe. */
export function has_smart_quotes(text: string): boolean {
  return !!text && /[\u201c\u201d\u2018\u2019]/.test(text);
}

/**
 * Rewrites `new_text` so every position the caller did NOT intentionally
 * change keeps the DOCUMENT's own characters.
 *
 * The matcher is smart-quote-insensitive: an LLM that writes
 * `parties' Master` matches a document reading `parties’ Master`, which is the
 * forgiving behaviour we want. The writer then word-diffs the document's real
 * slice against the caller's literal `new_text`, so each such difference used
 * to land as a genuine tracked change on a provision nobody touched — four of
 * eight change chunks in the reported run were pure punctuation rewrites (B4).
 *
 * Both strings are normalized (length-preserving, see SMART_QUOTE_MAP) and
 * aligned character-by-character; runs the alignment calls EQUAL adopt
 * `doc_text`'s characters, runs that genuinely differ keep the caller's. When
 * the two differ ONLY by normalized punctuation the result is `doc_text`
 * verbatim, i.e. zero tracked changes. Mirrors Python
 * restore_document_typography.
 */
export function restore_document_typography(
  doc_text: string,
  new_text: string,
): string {
  if (!doc_text || !new_text) return new_text;
  if (!has_smart_quotes(doc_text)) return new_text;

  const norm_doc = normalize_smart_quotes(doc_text);
  const norm_new = normalize_smart_quotes(new_text);
  // Differ ONLY by forgiven punctuation: the correct number of tracked changes
  // is zero, so hand back the document verbatim.
  if (norm_doc === norm_new) return doc_text;

  const dmp = new diff_match_patch();
  const diffs = dmp.diff_main(norm_doc, norm_new);

  const out: string[] = [];
  let doc_pos = 0;
  let new_pos = 0;
  for (const [op, chunk] of diffs) {
    if (op === 0) {
      // EQUAL -> the caller changed nothing here
      out.push(doc_text.substr(doc_pos, chunk.length));
      doc_pos += chunk.length;
      new_pos += chunk.length;
    } else if (op === -1) {
      // DELETE -> present in the document, dropped by the caller
      doc_pos += chunk.length;
    } else {
      // INSERT -> the caller's own text
      out.push(new_text.substr(new_pos, chunk.length));
      new_pos += chunk.length;
    }
  }
  return out.join("");
}

/**
 * Undoes the typographic drift a forgiving MATCH introduces into a literal
 * WRITE. The guard is the asymmetry itself: only when the document slice
 * carries smart typography that the caller's own target does NOT is the match
 * known to have been forgiving. A caller quoting the document's real
 * characters (`“Confidential”` → `"Confidential"`) is asking for the change
 * and still gets it. Mirrors Python RedlineEngine._restore_matched_typography.
 */
export function restore_matched_typography(
  actual_doc_text: string,
  caller_target: string,
  new_text: string,
): string {
  if (!new_text || !actual_doc_text) return new_text;
  if (!has_smart_quotes(actual_doc_text)) return new_text;
  if (has_smart_quotes(caller_target || "")) return new_text;
  return restore_document_typography(actual_doc_text, new_text);
}
