import { trim_common_context } from "./diff.js";
import { ModifyText } from "./models.js";
import { RegexTimeoutError, userFindAllMatches } from "./utils/safe-regex.js";

/** One entry per input edit in apply_edits_to_markdown's edit_reports. */
export interface MarkupEditReport {
  index: number;
  status: "applied" | "failed";
  error: string | null;
  occurrences: number;
}
export const AMBIGUITY_EXAMPLES_CAP = 5;
export const AMBIGUITY_CONTEXT_CHARS = 50;
function _should_strip_markers(text: string, marker: string): boolean {
  if (!text.startsWith(marker) || !text.endsWith(marker)) return false;
  if (text.length < marker.length * 2) return false;

  const inner = text.substring(marker.length, text.length - marker.length);
  if (!inner) return false;

  if (inner.includes(marker)) return false;
  if (!/[a-zA-Z]/.test(inner)) return false;

  if (marker === "__" && /^\w+$/.test(inner)) return false;
  if (marker === "_") {
    if (inner.includes("_")) return false;
    if (/^[0-9_]+$/.test(inner)) return false;
  }

  return true;
}

function _strip_balanced_markers(text: string): [string, string, string] {
  let prefix_markup = "";
  let suffix_markup = "";
  let clean_text = text;

  const markers = ["**", "__", "_", "*"];

  for (const marker of markers) {
    if (_should_strip_markers(clean_text, marker)) {
      prefix_markup += marker;
      suffix_markup = marker + suffix_markup;
      clean_text = clean_text.substring(
        marker.length,
        clean_text.length - marker.length,
      );
      break;
    }
  }

  return [prefix_markup, clean_text, suffix_markup];
}

export function _replace_smart_quotes(text: string): string {
  return text
    .replace(/“/g, '"')
    .replace(/”/g, '"')
    .replace(/‘/g, "'")
    .replace(/’/g, "'");
}

/**
 * Strips markdown formatting markers and builds a position map.
 * Returns [stripped_text, position_map] where position_map[i] = original
 * index. Mirrors Python markup._strip_markdown_for_matching.
 */
function _strip_markdown_for_matching(text: string): [string, number[]] {
  const result: string[] = [];
  const position_map: number[] = [];
  let i = 0;

  while (i < text.length) {
    // Skip ** or __
    const pair = text.substring(i, i + 2);
    if (i < text.length - 1 && (pair === "**" || pair === "__")) {
      i += 2;
      continue;
    }
    // Skip single * or _ that look like markdown (at word boundaries)
    if (text[i] === "*" || text[i] === "_") {
      const prev_char = i > 0 ? text[i - 1] : " ";
      const next_char = i < text.length - 1 ? text[i + 1] : " ";
      // If at boundary (space or start/end), likely markdown
      if (
        [" ", "\n", "\t"].includes(prev_char) ||
        [" ", "\n", "\t"].includes(next_char)
      ) {
        i += 1;
        continue;
      }
    }

    position_map.push(i);
    result.push(text[i]);
    i += 1;
  }

  return [result.join(""), position_map];
}

function _find_safe_boundaries(
  text: string,
  start: number,
  end: number,
): [number, number] {
  let new_start = start;
  let new_end = end;

  const expand_if_unbalanced = (marker: string) => {
    const current_match = text.substring(new_start, new_end);
    const count = (
      current_match.match(new RegExp(marker.replace(/\*/g, "\\*"), "g")) || []
    ).length;

    if (count % 2 !== 0) {
      const suffix = text.substring(new_end);
      if (suffix.startsWith(marker)) {
        new_end += marker.length;
        return;
      }
      const prefix = text.substring(0, new_start);
      if (prefix.endsWith(marker)) {
        new_start -= marker.length;
        return;
      }
    }
  };

  for (let i = 0; i < 2; i++) {
    expand_if_unbalanced("**");
    expand_if_unbalanced("__");
    expand_if_unbalanced("_");
    expand_if_unbalanced("*");
  }

  return [new_start, new_end];
}

function _refine_match_boundaries(
  text: string,
  start: number,
  end: number,
): [number, number] {
  const markers = ["**", "__", "*", "_"];
  let current_text = text.substring(start, end);
  let best_start = start;
  let best_end = end;

  const countMarker = (str: string, mk: string) =>
    (str.match(new RegExp(mk.replace(/\*/g, "\\*"), "g")) || []).length;

  for (const marker of markers) {
    if (current_text.startsWith(marker)) {
      const current_score = countMarker(current_text, marker) % 2;
      const trimmed_text = current_text.substring(marker.length);
      const trimmed_score = countMarker(trimmed_text, marker) % 2;

      if (current_score === 1 && trimmed_score === 0) {
        best_start += marker.length;
        current_text = trimmed_text;
      }
    }
  }

  for (const marker of markers) {
    if (current_text.endsWith(marker)) {
      const current_score = countMarker(current_text, marker) % 2;
      const trimmed_text = current_text.substring(
        0,
        current_text.length - marker.length,
      );
      const trimmed_score = countMarker(trimmed_text, marker) % 2;

      if (current_score === 1 && trimmed_score === 0) {
        best_end -= marker.length;
        current_text = trimmed_text;
      }
    }
  }

  return [best_start, best_end];
}

export function _make_fuzzy_regex(target_text: string): string {
  target_text = _replace_smart_quotes(target_text);

  const parts: string[] = [];
  const token_pattern = /(_+)|(\s+)|(['"])|([.,;:\/\-\[\](){}+=$?*!|#^<>\\%&@~`])/g;

  // Note: JS does not support atomic groups (?>...).
  // However, because we only match markdown characters * and _,
  // we can use a character class `[*_]*` which is mathematically equivalent
  // to `(?:\*\*|__|\*|_)*` but fundamentally immune to catastrophic backtracking!
  const md_noise = "[*_]*";
  const structural_noise = "(?:\\s*(?:[*+\\->]|\\d+\\.)\\s+|\\s*\\n\\s*)";

  const start_list_marker = "(?:[ \\t]*(?:[*+\\->]|\\d+\\.)\\s+)?";
  parts.push(start_list_marker);
  parts.push(md_noise);

  let last_idx = 0;
  let match;

  const escapeRegExp = (str: string) =>
    str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  while ((match = token_pattern.exec(target_text)) !== null) {
    const literal = target_text.substring(last_idx, match.index);
    if (literal) {
      parts.push(escapeRegExp(literal));
      parts.push(md_noise);
    }

    const g_underscore = match[1];
    const g_space = match[2];
    const g_quote = match[3];
    const g_punct = match[4];

    if (g_underscore) {
      parts.push("_+");
    } else if (g_space) {
      if (g_space.includes("\n")) {
        parts.push(`(?:${structural_noise}|\\s+)+`);
      } else {
        parts.push("\\s+");
      }
    } else if (g_quote) {
      if (g_quote === "'") parts.push("[\u2018\u2019']");
      else parts.push('["\u201c\u201d]');
    } else if (g_punct) {
      parts.push(escapeRegExp(g_punct));
    }

    parts.push(md_noise);
    last_idx = token_pattern.lastIndex;
  }

  const remaining = target_text.substring(last_idx);
  if (remaining) parts.push(escapeRegExp(remaining));

  return parts.join("");
}

export function _find_match_in_text(
  text: string,
  target: string,
): [number, number] {
  const matches = _find_all_matches_in_text(text, target);
  if (matches.length > 0) return matches[0];
  return [-1, -1];
}

/**
 * Every non-overlapping match of `target` in `text` as [start, end] pairs,
 * using the SAME strategy ladder as the apply engine's
 * DocumentMapper.find_all_match_indices: regex (when requested) or
 * exact → smart-quote-normalized → fuzzy. Markup previews must resolve
 * matching identically to apply, or the preview lies (QA 2026-07-18 M1).
 */
export function _find_all_matches_in_text(
  text: string,
  target: string,
  is_regex = false,
): [number, number][] {
  if (!target) return [];

  if (is_regex) {
    // Same semantics as the mapper: budgeted user regex; an invalid pattern
    // simply produces no matches (surfaced as "not found").
    // RegexTimeoutError propagates for a clean per-edit error.
    try {
      return userFindAllMatches(target, text).map((m) => [m.start, m.end]);
    } catch (e) {
      if (e instanceof RegexTimeoutError) throw e;
      return [];
    }
  }

  // Literal indexOf scans (no RegExp over an arbitrarily long escaped
  // target, mirroring the mapper's exact tiers).
  const findAllLiteral = (
    haystack: string,
    needle: string,
  ): [number, number][] => {
    const out: [number, number][] = [];
    let from = 0;
    while (true) {
      const idx = haystack.indexOf(needle, from);
      if (idx === -1) break;
      out.push([idx, idx + needle.length]);
      from = idx + needle.length;
    }
    return out;
  };

  // 1. Exact matches
  let spans = findAllLiteral(text, target);
  if (spans.length > 0) {
    return spans.map(([s, e]) => _find_safe_boundaries(text, s, e));
  }

  // 2. Smart quote normalization
  const norm_text = _replace_smart_quotes(text);
  const norm_target = _replace_smart_quotes(target);
  spans = findAllLiteral(norm_text, norm_target);
  if (spans.length > 0) {
    return spans.map(([s, e]) => _find_safe_boundaries(text, s, e));
  }

  // 3. Markdown-stripped match, mirroring the mapper's strip-markdown and
  // plain-projection rungs: a plain target must find text whose projection
  // carries **bold**/_italic_ markers (even mid-word), and a marked target
  // must find plain text.
  const [stripped_text, pos_map] = _strip_markdown_for_matching(norm_text);
  const [stripped_target] = _strip_markdown_for_matching(norm_target);
  if (
    stripped_target &&
    (stripped_text !== norm_text || stripped_target !== norm_target)
  ) {
    const results: [number, number][] = [];
    for (const [p_start, p_end] of findAllLiteral(stripped_text, stripped_target)) {
      const raw_start = pos_map[p_start];
      const raw_end = pos_map[p_end - 1] + 1;
      results.push(_find_safe_boundaries(text, raw_start, raw_end));
    }
    if (results.length > 0) return results;
  }

  // 4. Fuzzy regex matches (handles markdown noise, list markers, etc.).
  // The [*_]* character classes in _make_fuzzy_regex prevent catastrophic
  // backtracking.
  try {
    const pattern = new RegExp(_make_fuzzy_regex(target), "g");
    const results: [number, number][] = [];
    for (const match of text.matchAll(pattern)) {
      const [refined_start, refined_end] = _refine_match_boundaries(
        text,
        match.index!,
        match.index! + match[0].length,
      );
      results.push(_find_safe_boundaries(text, refined_start, refined_end));
    }
    if (results.length > 0) return results;
  } catch (e) {
    // Ignore regex compilation errors from edge cases
  }

  return [];
}

export function _build_critic_markup(
  target_text: string,
  new_text: string,
  comment: string | null | undefined,
  edit_index: number,
  include_index: boolean,
  highlight_only: boolean,
): string {
  const parts: string[] = [];

  let [prefix_markup, clean_target, suffix_markup] =
    _strip_balanced_markers(target_text);

  let clean_new = new_text;
  if (prefix_markup && new_text) {
    if (
      new_text.startsWith(prefix_markup) &&
      new_text.endsWith(suffix_markup)
    ) {
      const inner_len = prefix_markup.length;
      clean_new =
        new_text.length > inner_len * 2
          ? new_text.substring(inner_len, new_text.length - inner_len)
          : new_text;
    }
  }

  parts.push(prefix_markup);

  if (highlight_only) {
    parts.push(`{==${clean_target}==}`);
  } else {
    const has_target = Boolean(clean_target);
    const has_new = Boolean(clean_new);

    if (has_target && !has_new) parts.push(`{--${clean_target}--}`);
    else if (!has_target && has_new) parts.push(`{++${clean_new}++}`);
    else if (has_target && has_new)
      parts.push(`{--${clean_target}--}{++${clean_new}++}`);
  }

  parts.push(suffix_markup);

  const meta_parts: string[] = [];
  if (comment) meta_parts.push(comment);
  if (include_index) meta_parts.push(`[Edit:${edit_index}]`);

  if (meta_parts.length > 0) {
    parts.push(`{>>${meta_parts.join(" ")}<<}`);
  }

  return parts.join("");
}

/**
 * Applies edits to Markdown text and returns CriticMarkup-annotated output.
 *
 * Edit resolution follows the SAME semantics as `apply` (QA 2026-07-18 M1):
 *   - `regex: true` targets match as regular expressions
 *   - `match_mode` is honored: "strict" (default) refuses ambiguous targets,
 *     "first" marks the first occurrence, "all" marks every occurrence
 *   - a missing target is a per-edit failure, never a silent skip
 *
 * When `edit_reports` is provided, one entry per input edit is appended:
 * { index: 0-based input position, status: "applied"|"failed",
 *   error: string|null, occurrences: number }.
 */
export function apply_edits_to_markdown(
  markdown_text: string,
  edits: ModifyText[],
  include_index = false,
  highlight_only = false,
  edit_reports?: MarkupEditReport[],
): string {
  if (!edits || edits.length === 0) return markdown_text;

  const _report = (
    idx: number,
    status: "applied" | "failed",
    error: string | null = null,
    occurrences = 0,
  ) => {
    if (edit_reports) edit_reports.push({ index: idx, status, error, occurrences });
  };

  // Step 1: Find match positions for each edit
  const matched_edits: [number, number, string, ModifyText, number][] = [];

  for (let idx = 0; idx < edits.length; idx++) {
    const edit = edits[idx];
    const target = edit.target_text || "";
    const match_mode = (edit as any).match_mode || "strict";
    const is_regex = Boolean((edit as any).regex);

    if (!target) {
      _report(
        idx,
        "failed",
        `- Edit ${idx + 1} Failed: target_text is empty. Pure insertions are expressed as a ` +
          "replacement: put the text immediately around the insertion point in target_text and " +
          "repeat it (plus the new text) in new_text.",
      );
      continue;
    }

    let spans: [number, number][];
    try {
      spans = _find_all_matches_in_text(markdown_text, target, is_regex);
    } catch (e) {
      if (!(e instanceof RegexTimeoutError)) throw e;
      _report(idx, "failed", `- Edit ${idx + 1} Failed: ${e.message}`);
      continue;
    }

    if (spans.length === 0) {
      _report(
        idx,
        "failed",
        `- Edit ${idx + 1} Failed: Target text not found in document:\n  "${target.substring(0, 80)}"`,
      );
      continue;
    }

    if (spans.length > 1 && match_mode === "strict") {
      _report(
        idx,
        "failed",
        format_ambiguity_error(idx + 1, target, markdown_text, spans),
      );
      continue;
    }

    const selected =
      match_mode === "strict" || match_mode === "first" ? spans.slice(0, 1) : spans;
    for (const [start, end] of selected) {
      matched_edits.push([start, end, markdown_text.substring(start, end), edit, idx]);
    }
    _report(idx, "applied", null, selected.length);
  }

  // Step 2: Check for overlapping edits
  const matched_edits_filtered: [number, number, string, ModifyText, number][] =
    [];
  const occupied_ranges: [number, number][] = [];

  matched_edits.sort((a, b) => a[4] - b[4]);

  for (const [start, end, actual_text, edit, orig_idx] of matched_edits) {
    let overlaps = false;
    for (const [occ_start, occ_end] of occupied_ranges) {
      if (start < occ_end && end > occ_start) {
        overlaps = true;
        if (edit_reports) {
          const msg = `- Edit ${orig_idx + 1} Failed: overlaps with a previously matched edit.`;
          for (const r of edit_reports) {
            if (r.index === orig_idx) {
              r.status = "failed";
              r.error = msg;
              r.occurrences = 0;
            }
          }
        }
        break;
      }
    }

    if (!overlaps) {
      matched_edits_filtered.push([start, end, actual_text, edit, orig_idx]);
      occupied_ranges.push([start, end]);
    }
  }

  matched_edits_filtered.sort((a, b) => b[0] - a[0]);

  let result = markdown_text;

  for (const [
    start,
    end,
    actual_text,
    edit,
    orig_idx,
  ] of matched_edits_filtered) {
    const new_txt = edit.new_text || "";
    const [prefix_len, suffix_len] = trim_common_context(actual_text, new_txt);

    const unmodified_prefix =
      prefix_len > 0 ? actual_text.substring(0, prefix_len) : "";
    const unmodified_suffix =
      suffix_len > 0
        ? actual_text.substring(actual_text.length - suffix_len)
        : "";

    const t_end = actual_text.length - suffix_len;
    const n_end = new_txt.length - suffix_len;

    const isolated_target = actual_text.substring(prefix_len, t_end);
    const isolated_new = new_txt.substring(prefix_len, n_end);

    const markup = _build_critic_markup(
      isolated_target,
      isolated_new,
      edit.comment,
      // 1-based, matching apply's "Edit N" reports and batch validation
      // errors (QA 2026-07-17 F10; mirrors Python).
      orig_idx + 1,
      include_index,
      highlight_only,
    );

    const full_replacement = unmodified_prefix + markup + unmodified_suffix;
    result =
      result.substring(0, start) + full_replacement + result.substring(end);
  }

  return result;
}
export function format_ambiguity_error(
  edit_index: number,
  target_text: string,
  haystack: string,
  match_positions: [number, number][],
): string {
  const total = match_positions.length;
  if (total < 2) {
    throw new Error(
      `format_ambiguity_error requires at least 2 matches, got ${total}`,
    );
  }

  const shown = match_positions.slice(0, AMBIGUITY_EXAMPLES_CAP);
  const remaining = total - shown.length;

  const lines: string[] = [
    `- Edit ${edit_index} Failed: Ambiguous match. Target text appears ${total} times. First ${shown.length} occurrences:`,
  ];

  for (let i = 0; i < shown.length; i++) {
    const [start, end] = shown[i];
    const pre_start = Math.max(0, start - AMBIGUITY_CONTEXT_CHARS);
    const post_end = Math.min(haystack.length, end + AMBIGUITY_CONTEXT_CHARS);

    const pre_context = haystack
      .substring(pre_start, start)
      .replace(/\n/g, " ");
    const post_context = haystack.substring(end, post_end).replace(/\n/g, " ");
    let match_text = haystack.substring(start, end).replace(/\n/g, " ");

    if (match_text.length > 50) {
      match_text =
        match_text.substring(0, 25) +
        "..." +
        match_text.substring(match_text.length - 20);
    }

    const prefix_marker = pre_start > 0 ? "..." : "";
    const suffix_marker = post_end < haystack.length ? "..." : "";

    lines.push(
      `    ${i + 1}. "${prefix_marker}${pre_context}[${match_text}]${post_context}${suffix_marker}"`,
    );
  }

  if (remaining > 0) {
    lines.push(`    ... and ${remaining} more occurrence(s) not shown.`);
  }

  // Tell the agent EXACTLY how to re-call. Without this, agents loop forever
  // refining target_text/regex because they never learn that match_mode is the
  // built-in escape hatch for genuine ambiguity. The safe strategy (more
  // context) comes first: blindly switching to "first"/"all" has silently
  // modified unrelated occurrences — dates, section numbers — in real use
  // (QA C1), so those options carry an explicit verification warning.
  // Wording mirrors the Python engine's format_ambiguity_error.
  lines.push("  To resolve, re-send this edit using ONE of these strategies:");
  lines.push(
    "    1. RECOMMENDED: Provide more surrounding context in your target_text to uniquely " +
      'identify a single location (keep the default "match_mode": "strict").',
  );
  lines.push(
    `    2. Set "match_mode": "all" to modify ALL ${total} occurrences — only after verifying ` +
      "from the occurrence list above that EVERY occurrence should change.",
  );
  lines.push(
    '    3. Set "match_mode": "first" to modify only the FIRST occurrence — only after verifying ' +
      "the first occurrence above is the one you intend to change.",
  );

  return lines.join("\n");
}
