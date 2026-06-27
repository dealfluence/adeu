import { trim_common_context } from "./diff.js";
import { ModifyText } from "./models.js";
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
  const token_pattern = /(_+)|(\s+)|(['"])|([.,;:\/])/g;

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
  if (!target) return [-1, -1];

  let idx = text.indexOf(target);
  if (idx !== -1) return _find_safe_boundaries(text, idx, idx + target.length);

  const norm_text = _replace_smart_quotes(text);
  const norm_target = _replace_smart_quotes(target);
  idx = norm_text.indexOf(norm_target);
  if (idx !== -1)
    return _find_safe_boundaries(text, idx, idx + norm_target.length);

  try {
    const pattern = new RegExp(_make_fuzzy_regex(target));
    const match = pattern.exec(text);
    if (match) {
      const raw_start = match.index;
      const raw_end = match.index + match[0].length;
      const [refined_start, refined_end] = _refine_match_boundaries(
        text,
        raw_start,
        raw_end,
      );
      return _find_safe_boundaries(text, refined_start, refined_end);
    }
  } catch (e) {
    // Ignore regex compilation errors from edge cases
  }

  return [-1, -1];
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

export function apply_edits_to_markdown(
  markdown_text: string,
  edits: ModifyText[],
  include_index = false,
  highlight_only = false,
): string {
  if (!edits || edits.length === 0) return markdown_text;

  const matched_edits: [number, number, string, ModifyText, number][] = [];

  for (let idx = 0; idx < edits.length; idx++) {
    const edit = edits[idx];
    const target = edit.target_text || "";

    if (!target) {
      continue;
    }

    const [start, end] = _find_match_in_text(markdown_text, target);
    if (start === -1) continue;

    const actual_matched_text = markdown_text.substring(start, end);
    matched_edits.push([start, end, actual_matched_text, edit, idx]);
  }

  const matched_edits_filtered: [number, number, string, ModifyText, number][] =
    [];
  const occupied_ranges: [number, number][] = [];

  matched_edits.sort((a, b) => a[4] - b[4]);

  for (const [start, end, actual_text, edit, orig_idx] of matched_edits) {
    let overlaps = false;
    for (const [occ_start, occ_end] of occupied_ranges) {
      if (start < occ_end && end > occ_start) {
        overlaps = true;
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
      orig_idx,
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
  // built-in escape hatch for genuine ambiguity.
  lines.push("  To resolve, re-send this edit using ONE of these strategies:");
  lines.push(
    `    1. Set "match_mode": "all" to modify ALL ${total} occurrences (same target_text).`,
  );
  lines.push(
    '    2. Set "match_mode": "first" to modify only the FIRST occurrence (same target_text).',
  );
  lines.push(
    '    3. Please provide more surrounding context in your target_text to uniquely ' +
      'identify a single location (keep the default "match_mode": "strict").',
  );

  return lines.join("\n");
}
