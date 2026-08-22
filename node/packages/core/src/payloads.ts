// FILE: src/payloads.ts
// Payload builders for error envelopes and response formatting. Ported
// function-for-function from python/src/adeu/payloads.py, Python names kept in
// snake_case so the two files diff side by side.
import { CRITICMARKUP_BLOCK_RE } from "./diff.js";
import { clamp_text } from "./utils/text.js";

// Ceiling for one applied edit in a minimal report, in the approx-token unit
// used by the report budget tests (len(json) // 4). It covers every field of an
// applied edit, engine advisories included: only a FAILED edit's error rides
// free, because that edit has no other content to explain itself with.
export const MINIMAL_EDIT_TOKEN_BUDGET = 40;

// A failed edit echoes just enough of the caller's target_text to identify
// which edit failed; the error message carries the diagnosis.
export const FAILED_TARGET_STUB_CAP = 80;

// The four CriticMarkup bubble forms. Every delimiter is exactly 3 characters
// ("{--"/"--}", "{++"/"++}", "{=="/"==}", "{>>"/"<<}"), which is what lets a
// bubble body be clamped in place without disturbing its delimiters.
const _CRITIC_DELIM_LEN = 3;
const _CRITIC_DELIMITERS = [
  "{--",
  "--}",
  "{++",
  "++}",
  "{==",
  "==}",
  "{>>",
  "<<}",
];

/** Whether text contains any CriticMarkup delimiter markers. */
function _has_critic_delimiters(text: string): boolean {
  return _CRITIC_DELIMITERS.some((delim) => text.includes(delim));
}

/** Whether text contains CriticMarkup delimiter markers outside complete bubbles. */
function _has_orphaned_critic_delimiters(text: string): boolean {
  const outside = text.replace(CRITICMARKUP_BLOCK_RE, "");
  return _has_critic_delimiters(outside);
}

// The one field exempt from the per-edit budget: a failed edit's error, which
// the agent must read in full to recover.
const _UNBUDGETED_FIELDS = ["error"];

// Smallest bubble body worth emitting — below this the preview stops being
// evidence of anything.
const _MIN_BUBBLE_BODY = 8;

// Smallest warning worth emitting. The engine's advisories lead with the
// problem and the token that caused it ("new_text contains '$1', …"), so a
// clamp here still tells the agent what to look at, and it leaves just enough
// budget for a bounded preview alongside; the remediation sentence that follows
// is what 40 approx-tokens cannot afford. The full text stays in the standard
// report.
const _MIN_WARNING_CHARS = 26;

// Stands in for document context dropped from a preview. Three dots ASCII
// indicator for dropped context in elisions.
const _ELISION = "...";

// Python's str.splitlines(), whose boundary set is wider than "\r\n|\n|\r":
// \v, \f, \x1c-\x1e, \u0085 (NEL), \u2028 (LS) and \u2029 (PS) break a line
// too. \u0085 and \u2028 are legal XML 1.0 characters, so the engine's
// illegal-character scrub leaves them in place and they reach these payloads
// inside real document text.
const _LINE_SPLIT_RE = /\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/;

/**
 * Python truthiness for the JSON-shaped values a report field can hold: an
 * EMPTY container is false. This is where a transliterated `if (value)`
 * diverges — `[]` and `{}` are truthy in JS — and `pages` is the field that
 * makes it ordinary rather than exotic: the engine sets `pages: _pages || []`
 * on every per-edit report, so `if (edit.pages)` would emit `"pages":[]` where
 * Python's `if edit.get("pages")` omits the key.
 */
function _truthy(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  if (value !== null && typeof value === "object") {
    return Object.keys(value).length > 0;
  }
  return Boolean(value);
}

// The two-call recovery every batch failure teaches (spec B2). A batch is
// transactional, so the reflex — resubmit the whole batch — repeats every edit
// that already validated; splitting the failures out is what converges.
// The re-read sentence names no command on purpose: this text also travels
// inside MCP responses, where a CLI-ism is advice the caller cannot run
// (QA 2026-07-23 F11).
export const BATCH_RECOVERY_PROTOCOL =
  "Nothing was written. Recover in two calls: (1) re-apply this batch WITHOUT the failing edit(s); " +
  "(2) fix the failing edit(s) in a separate small batch. " +
  "Copy target_text verbatim from a fresh read of the CURRENT file, not from another tool's view of it.";

// Hint appended when model serializes JSON object/array markers into the 'type' field (Item B7).
export const FUSED_JSON_HINT =
  "This looks like two edits fused during generation — resubmit this edit alone, correctly formed.";

/** Whether an invalid type string contains markers indicating fused JSON ({, }, or ":"). */
export function has_fused_json_marker(text: unknown): boolean {
  if (typeof text !== "string") return false;
  return ["{", "}", '":'].some((marker) => text.includes(marker));
}

// The only failures the recovery protocol can help with: a rejected BATCH. A
// missing file, an unreadable DOCX or a failed write has no failing edit to
// split out, so the protocol would be advice the caller cannot act on.
export const BATCH_ERROR_CODES: ReadonlySet<string> = new Set([
  "invalid_changes_file",
  "batch_validation_failed",
]);

export interface FailureEnvelope {
  error: string;
  failed: { index: number; reason: string }[];
  message: string;
  errors?: string[];
}

/**
 * Builds a uniform machine-readable failure envelope.
 *
 * A batch code (see BATCH_ERROR_CODES) additionally carries
 * BATCH_RECOVERY_PROTOCOL at the end of "message".
 *
 * @param code Stable error code string (e.g. "invalid_changes_file", "batch_validation_failed").
 * @param failed List of [0-based_batch_index, reason_string] tuples.
 * @param message Human-readable error message.
 * @param errors Optional list of raw prose error strings for backward compatibility.
 */
export function failure_envelope(
  code: string,
  failed: [number, string][],
  message: string,
  errors?: string[],
): FailureEnvelope {
  let clean_message = message
    .split(_LINE_SPLIT_RE)
    .map((line) => line.trim())
    .filter(Boolean)
    .join(" ");
  if (
    BATCH_ERROR_CODES.has(code) &&
    !clean_message.includes(BATCH_RECOVERY_PROTOCOL)
  ) {
    clean_message = clean_message
      ? `${clean_message} ${BATCH_RECOVERY_PROTOCOL}`
      : BATCH_RECOVERY_PROTOCOL;
  }
  const res: FailureEnvelope = {
    error: code,
    failed: failed.map(([index, reason]) => ({ index, reason })),
    message: clean_message,
  };
  if (errors !== undefined && errors !== null) {
    res.errors = errors;
  }
  return res;
}

/**
 * The CriticMarkup bubbles of a preview with the surrounding document context
 * dropped. Context is the cheapest thing to give up: it repeats text the
 * caller can read from the document, whereas the bubbles ARE the evidence
 * that the edit landed as asked.
 */
function _changed_span(markup: string): string {
  const bubbles = [...markup.matchAll(CRITICMARKUP_BLOCK_RE)];
  if (bubbles.length === 0) return markup;
  const first = bubbles[0];
  const last = bubbles[bubbles.length - 1];
  return markup.slice(first.index, last.index + last[0].length);
}

/** Shortens a bubble's body, leaving its opening and closing delimiter intact. */
function _clamp_bubble(bubble: string, body_cap: number): string {
  const body = bubble.slice(_CRITIC_DELIM_LEN, -_CRITIC_DELIM_LEN);
  return (
    bubble.slice(0, _CRITIC_DELIM_LEN) +
    clamp_text(body, body_cap) +
    bubble.slice(-_CRITIC_DELIM_LEN)
  );
}

/**
 * A preview's bubbles in document order, each carrying an elision marker in
 * place of the context that separated it from the previous bubble (adjacent
 * bubbles — the {--old--}{++new++} of one occurrence — stay welded together).
 *
 * Joining these is what bounds a match_mode="all" fan-out: its preview is up
 * to ten windows of 30-chars-a-side context, joined by separators and
 * interleaved with whole clauses of untouched text. None of that is reachable
 * by clamping bubble bodies, so dropping the outer context alone left such a
 * preview essentially unshrinkable.
 */
function _bubble_segments(markup: string): string[] {
  const segments: string[] = [];
  let prev_end: number | null = null;
  for (const match of markup.matchAll(CRITICMARKUP_BLOCK_RE)) {
    const separator =
      prev_end === null || prev_end === match.index ? "" : _ELISION;
    segments.push(separator + match[0]);
    prev_end = match.index + match[0].length;
  }
  return segments;
}

/**
 * Bounds a CriticMarkup preview to roughly `cap` characters without ever
 * cutting a bubble open. Context is surrendered first — outside the bubbles,
 * then between them — next the trailing bubbles, counted off in a
 * "(+N more spans)" note so the preview never implies the edit marked up less
 * than it did, and only then are the surviving bodies clamped in place. The
 * first bubble is kept whichever rung is reached, and every
 * {--…--}/{++…++}/{==…==}/{>>…<<} stays balanced: a bare delimiter fragment
 * corrupts the markup for every consumer, including this package's own
 * preview regexes (AI_CONTEXT.md).
 */
function _shrink_critic_markup(markup: string, cap: number): string {
  const span = _changed_span(markup);
  if (_has_orphaned_critic_delimiters(span)) return "";
  if (span.length <= cap) return span;
  const segments = _bubble_segments(span);
  if (segments.length === 0) {
    if (_has_critic_delimiters(span)) return "";
    // No markup to protect: a plain-text preview is safe to cut.
    return clamp_text(span, cap);
  }

  let kept = segments.length;
  let shrunk = segments.join("");
  while (shrunk.length > cap && kept > 1) {
    kept -= 1;
    shrunk =
      segments.slice(0, kept).join("") +
      `${_ELISION}(+${segments.length - kept} more spans)`;
  }
  if (shrunk.length <= cap) {
    return !_has_orphaned_critic_delimiters(shrunk) ? shrunk : "";
  }

  const body_cap = Math.max(
    _MIN_BUBBLE_BODY,
    Math.floor(cap / kept) - 2 * _CRITIC_DELIM_LEN,
  );
  const res = shrunk.replace(CRITICMARKUP_BLOCK_RE, (bubble) =>
    _clamp_bubble(bubble, body_cap),
  );
  return !_has_orphaned_critic_delimiters(res) ? res : "";
}

/**
 * Whether an edit report fits MINIMAL_EDIT_TOKEN_BUDGET, measured the way the
 * report budget is specified: approx-tokens (len(json) // 4) over the
 * serialized edit, ignoring the fields exempt from the budget.
 *
 * Measured over THIS engine's own emission: JSON.stringify writes no space
 * after ":" or "," where Python's json.dumps does, so an edit is scored a few
 * chars lighter here than in the Python engine. Both engines hold the same
 * ceiling over what they actually put on the wire.
 */
function _within_budget(edit: Record<string, unknown>): boolean {
  const budgeted: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(edit)) {
    if (!_UNBUDGETED_FIELDS.includes(key)) budgeted[key] = value;
  }
  return (
    Math.floor(JSON.stringify(budgeted).length / 4) <=
    MINIMAL_EDIT_TOKEN_BUDGET
  );
}

/**
 * Clamps one free-prose field of an edit toward `floor` characters, stopping
 * the moment the edit fits. Each step re-clamps the ORIGINAL value at a
 * smaller cap and re-measures the real serialized JSON, so actual serialized
 * JSON size is accounted for rather than predicted.
 */
function _shrink_prose(
  edit: Record<string, unknown>,
  key: string,
  value: string,
  floor: number,
): void {
  let cap = value.length;
  while (cap > floor && !_within_budget(edit)) {
    cap = Math.max(floor, Math.floor((cap * 4) / 5));
    edit[key] = clamp_text(value, cap);
  }
}

/** Spends the per-edit budget in priority order, in place. */
function _fit_to_budget(edit: Record<string, unknown>): void {
  // A string local: plain truthiness already IS Python's for `str`, and it
  // narrows away the `undefined` for _changed_span.
  let markup = edit.critic_markup as string | undefined;
  if (markup) {
    const span = _changed_span(markup);
    if (_has_orphaned_critic_delimiters(span)) {
      delete edit.critic_markup;
      markup = undefined;
    } else {
      edit.critic_markup = span;
      markup = span;
    }
  }

  if (_within_budget(edit)) return;

  const path = edit.heading_path as string | undefined;
  if (path && path.includes(" > ")) {
    // Deepest heading only: the ancestors are the least specific part.
    edit.heading_path = path.slice(path.lastIndexOf(" > ") + 3);
    if (_within_budget(edit)) return;
  }
  if ("heading_path" in edit) {
    delete edit.heading_path;
    if (_within_budget(edit)) return;
  }

  const warning = edit.warning;
  if (_truthy(warning)) {
    _shrink_prose(edit, "warning", String(warning), _MIN_WARNING_CHARS);
    if (_within_budget(edit)) return;
  }

  if (_truthy(edit.critic_markup)) {
    const preview = edit.critic_markup as string;
    // Measure the real JSON (escaping included) rather than predicting it.
    let preview_cap = preview.length;
    while (preview_cap > _MIN_BUBBLE_BODY && !_within_budget(edit)) {
      preview_cap = Math.floor((preview_cap * 4) / 5);
      const shrunk = _shrink_critic_markup(preview, preview_cap);
      if (!shrunk) {
        delete edit.critic_markup;
        break;
      }
      edit.critic_markup = shrunk;
    }
  }

  if ("pages" in edit && !_within_budget(edit)) {
    // Dropped whole, never truncated: a shortened page list would claim the
    // edit landed on fewer pages than it did, whereas an absent one claims
    // nothing and `occurrences_modified` still reports the fan-out size.
    delete edit.pages;
  }

  if (markup && !_within_budget(edit)) {
    // Last rung (see above): a valid preview or none, never a fragment.
    delete edit.critic_markup;
  }
}

/** Rebuilds one edit report with the caller's echoes dropped. */
function _minimal_edit(edit: Record<string, unknown>): Record<string, unknown> {
  const status = edit.status;
  const minimal: Record<string, unknown> = {};
  if (status !== undefined && status !== null) minimal.status = status;
  if ("type" in edit) minimal.type = edit.type;

  if (status === "failed") {
    if (edit.target_text !== undefined && edit.target_text !== null) {
      minimal.target_text = clamp_text(
        String(edit.target_text),
        FAILED_TARGET_STUB_CAP,
      );
    }
  } else if (_truthy(edit.critic_markup)) {
    minimal.critic_markup = edit.critic_markup;
  }

  if (_truthy(edit.pages)) minimal.pages = edit.pages;
  const heading_path = String(edit.heading_path || "").trim();
  if (heading_path) minimal.heading_path = heading_path;
  if (
    edit.occurrences_modified !== undefined &&
    edit.occurrences_modified !== null
  ) {
    minimal.occurrences_modified = edit.occurrences_modified;
  }
  const match_mode = edit.match_mode;
  if (
    match_mode !== undefined &&
    match_mode !== null &&
    match_mode !== "strict"
  ) {
    minimal.match_mode = match_mode;
  }
  if (_truthy(edit.warning)) minimal.warning = edit.warning;
  if (_truthy(edit.error)) minimal.error = edit.error;

  if (status !== "failed") _fit_to_budget(minimal);
  return minimal;
}

/**
 * Python's `str(item).strip()` in its role here: a key that is equal for equal
 * entries and distinct for distinct ones. `String(item)` cannot do that job —
 * it collapses every object to "[object Object]", so any two non-string
 * entries would compare equal, and each one after the first would be dropped
 * as a duplicate. The spelling itself never reaches the caller (the ORIGINAL
 * entry is what gets emitted), so JSON is free to stand in for repr.
 */
function _dedupe_key(value: unknown): string {
  return typeof value === "string"
    ? value.trim()
    : JSON.stringify(value ?? null);
}

/**
 * Every form in which a batch may repeat an edit's error: the whole message,
 * or one of its lines.
 */
function _error_lines(error: unknown): string[] {
  const text = _dedupe_key(error);
  return [
    text,
    ...text
      .split(_LINE_SPLIT_RE)
      .map((line) => line.trim())
      .filter(Boolean),
  ];
}

/**
 * Batch-level skipped details repeat the per-edit errors verbatim; a minimal
 * report states each reason once.
 */
function _dedupe_skipped(details: unknown[], edit_errors: Set<string>): unknown[] {
  const deduped: unknown[] = [];
  const seen = new Set<string>();
  for (const item of details) {
    const key = _dedupe_key(item);
    if (edit_errors.has(key) || seen.has(key)) continue;
    seen.add(key);
    deduped.push(item);
  }
  return deduped;
}

/**
 * Reshapes standard batch stats into the minimal report.
 *
 * Two classes of field share an edit report: echoes of caller input
 * (`target_text`, `new_text`, `clean_text`, `comment`) and engine-produced
 * verification evidence (`critic_markup`, `pages`, `heading_path`,
 * `occurrences_modified`). Minimal mode drops the echoes — the caller wrote
 * that text in the same turn and gains nothing by being sold it back — and
 * keeps the evidence, bounded to MINIMAL_EDIT_TOKEN_BUDGET approx-tokens per
 * applied edit. `clean_text` goes as a duplicate of `critic_markup`, which
 * already shows the same span with the change marked up.
 *
 * A failed edit keeps its full error plus a target stub of at most
 * FAILED_TARGET_STUB_CAP chars, so the agent can tell which edit failed and
 * why. Batch level: `engine` goes (a constant per binary), `version` stays,
 * and skipped details are deduplicated against the per-edit errors. Keys
 * absent from `stats` are never invented.
 */
export function shrink_batch_stats(
  stats: Record<string, any>,
): Record<string, any> {
  const res = { ...stats };
  delete res.engine;

  const edit_errors = new Set<string>();
  if ("edits" in stats) {
    const shrunk_edits: unknown[] = [];
    for (const edit of stats.edits) {
      if (edit === null || typeof edit !== "object" || Array.isArray(edit)) {
        shrunk_edits.push(edit);
        continue;
      }
      if (_truthy(edit.error)) {
        for (const line of _error_lines(edit.error)) edit_errors.add(line);
      }
      shrunk_edits.push(_minimal_edit(edit));
    }
    res.edits = shrunk_edits;
  }

  if ("skipped_details" in stats) {
    res.skipped_details = _dedupe_skipped(stats.skipped_details, edit_errors);
  }
  return res;
}

/**
 * Returns the maximum allowed response character count for unbounded whole-document reads.
 * Defaults to 76,000 characters (~19,000 tokens), overridable via ADEU_MAX_RESPONSE_CHARS.
 */
export function response_budget_limit(): number {
  // Python's int() is the reference: an optionally signed run of digits with
  // surrounding whitespace forgiven, plus underscores as digit-group
  // separators BETWEEN digits ("1_000" -> 1000, while "_1", "1_" and "1__0"
  // stay errors). So "1e3", "0x10" and "  " fall back rather than resolving to
  // a number JS alone would have parsed. One divergence kept on purpose:
  // int() also reads non-ASCII decimal digits (int("\uFF11\uFF10") == 10), and
  // JS exposes no digit-value API to match that without shipping a Unicode
  // table that would then drift against Python's Unicode version — such values
  // fall back to the default here.
  const text = (process.env.ADEU_MAX_RESPONSE_CHARS ?? "").trim();
  if (/^[+-]?\d+(?:_\d+)*$/.test(text)) return Number(text.replace(/_/g, ""));
  return 76000;
}

// Ceiling for what a surface actually EMITS for the guard, not for the raw
// message: the CLI --json envelope is the largest form (envelope chrome plus
// JSON escaping of every Windows path separator), so a message that fits inside
// it also fits on stderr and over MCP. 3,100 chars is ~775 approx tokens, held
// under the 800-token contract with room for the emitting write's newline.
export const GUARD_EMITTED_MAX_CHARS = 3100;

// Longest file path echoed back in a guard message. The caller supplied the
// path, so the tail (which names the file) is the part worth keeping.
const _GUARD_PATH_MAX_CHARS = 160;

/**
 * Length of `message` as the CLI emits it under --json: the largest surface
 * form. JSON.stringify leaves non-ASCII unescaped, exactly as Python's
 * json.dumps(..., ensure_ascii=False) does, so a smart-quoted path costs the
 * same in both engines.
 */
function _guard_emitted_length(message: string): number {
  return JSON.stringify(
    failure_envelope("response_budget_exceeded", [], message),
  ).length;
}

/**
 * Builds the refusal message for an oversized unbounded whole-document read.
 *
 * `outline` is a rendered L1 heading list (one heading per line), or "" when
 * the document has no L1 headings — no placeholder section is emitted for a
 * document without headings.
 *
 * The budget is enforced on the EMITTED response (see GUARD_EMITTED_MAX_CHARS)
 * by dropping whole outline entries from the tail and saying how many were
 * dropped. The prose and the recipe are never sliced, so every flag the
 * message advertises stays complete and runnable.
 */
export function whole_doc_guard_message(
  total_chars: number,
  limit: number,
  file_path: string = "",
  outline: string = "",
  page_count: number | null = null,
): string {
  const est_tokens = Math.floor(total_chars / 4);
  const shown_path =
    file_path.length <= _GUARD_PATH_MAX_CHARS
      ? file_path
      : "..." + file_path.slice(-_GUARD_PATH_MAX_CHARS);
  const file_info = shown_path ? ` for '${shown_path}'` : "";
  const page_info = page_count ? ` (${page_count} pages)` : "";

  const head = [
    `Refused unbounded full document read${file_info}${page_info}: ` +
      `total size (${total_chars.toLocaleString("en-US")} chars, ~${est_tokens.toLocaleString("en-US")} tokens) exceeds ` +
      `response budget limit (${limit.toLocaleString("en-US")} chars).`,
    "",
    "Recipe to read bounded sections:",
    "  - One page or a page range: --page 3 / --page 1-5 (MCP page=3 / page='1-5')",
    '  - Find a passage: --search-query "text" (MCP search_query=\'text\')',
    "  - Heading map: --mode outline (MCP mode='outline')",
    "  - Tracked changes ledger: --mode changes (MCP mode='changes')",
    "  - Read it all anyway: --force (MCP force=True)",
  ];

  const entries = outline
    .split(_LINE_SPLIT_RE)
    .filter((line) => line.trim().length > 0);
  const kept = [...entries];
  for (;;) {
    const lines = [...head];
    if (kept.length > 0) {
      lines.push("", "Outline (L1 Headings):", ...kept);
      if (kept.length < entries.length) {
        lines.push(
          `  (${entries.length - kept.length} more headings: --mode outline / MCP mode='outline')`,
        );
      }
    }
    const msg = lines.join("\n");
    if (
      kept.length === 0 ||
      _guard_emitted_length(msg) <= GUARD_EMITTED_MAX_CHARS
    ) {
      return msg;
    }
    kept.pop();
  }
}
