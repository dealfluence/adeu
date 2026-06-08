import diff_match_patch from "diff-match-patch";
import { ModifyText } from "./models.js";

function _count_standalone_underscores(s: string): number {
  let count = 0;
  let i = 0;
  const n = s.length;
  const isAlnum = (char: string) => /[a-zA-Z0-9]/.test(char);
  while (i < n) {
    if (s[i] === "_") {
      // Is it part of "__"?
      let is_double = false;
      if ((i > 0 && s[i - 1] === "_") || (i < n - 1 && s[i + 1] === "_")) {
        is_double = true;
      }

      // Is it intra-word?
      let is_intra = false;
      if (i > 0 && isAlnum(s[i - 1]) && i < n - 1 && isAlnum(s[i + 1])) {
        is_intra = true;
      }

      if (!is_double && !is_intra) {
        count++;
      }
    }
    i++;
  }
  return count;
}

export function trim_common_context(
  target: string,
  new_val: string,
): [number, number] {
  if (!target || !new_val) return [0, 0];

  const isSpace = (char: string) => /\s/.test(char);

  // 1. Prefix with Word Boundary Check
  let prefix_len = 0;
  let limit = Math.min(target.length, new_val.length);
  while (prefix_len < limit && target[prefix_len] === new_val[prefix_len]) {
    prefix_len++;
  }

  // Backtrack to nearest whitespace if we split a word
  if (prefix_len < target.length && prefix_len < new_val.length) {
    while (prefix_len > 0) {
      const target_split =
        !isSpace(target[prefix_len - 1]) && !isSpace(target[prefix_len]);
      const new_split =
        !isSpace(new_val[prefix_len - 1]) && !isSpace(new_val[prefix_len]);
      if (target_split || new_split) {
        prefix_len--;
      } else {
        break;
      }
    }
  }

  // Backtrack prefix to avoid splitting markdown markers
  while (prefix_len > 0) {
    if (prefix_len < target.length) {
      const charSeq = target.substring(prefix_len - 1, prefix_len + 1);
      if (charSeq === "**" || charSeq === "__") {
        prefix_len--;
        continue;
      }
    }

    const left = target.substring(0, prefix_len);
    const b_count = (left.match(/\*\*/g) || []).length;
    const u2_count = (left.match(/__/g) || []).length;
    const u1_count = _count_standalone_underscores(left);

    if (b_count % 2 !== 0) {
      prefix_len = left.lastIndexOf("**");
      continue;
    }
    if (u2_count % 2 !== 0) {
      prefix_len = left.lastIndexOf("__");
      continue;
    }
    if (u1_count % 2 !== 0) {
      let idx = left.length - 1;
      const isAlnum = (char: string) => /[a-zA-Z0-9]/.test(char);
      while (idx >= 0) {
        if (
          left[idx] === "_" &&
          (idx === 0 || left[idx - 1] !== "_") &&
          (idx === left.length - 1 || left[idx + 1] !== "_")
        ) {
          const is_intra = idx > 0 && isAlnum(left[idx - 1]) && idx < left.length - 1 && isAlnum(left[idx + 1]);
          if (!is_intra) {
            prefix_len = idx;
            break;
          }
        }
        idx--;
      }
      continue;
    }

    // Safety: Backtrack if we consumed a Markdown Header marker (#)
    let temp_len = prefix_len;
    let hit_header = false;
    while (temp_len > 0) {
      const char = target[temp_len - 1];
      if (char === "#") {
        prefix_len = temp_len - 1;
        while (prefix_len > 0 && target[prefix_len - 1] !== "\n") {
          prefix_len--;
        }
        hit_header = true;
        break;
      }
      if (char === "\n") break;
      temp_len--;
    }
    if (hit_header) continue;

    break;
  }

  // 2. Suffix with Word Boundary Check
  let suffix_len = 0;
  const target_rem_len = target.length - prefix_len;
  const new_rem_len = new_val.length - prefix_len;
  const limit_suffix = Math.min(target_rem_len, new_rem_len);

  while (
    suffix_len < limit_suffix &&
    target[target.length - 1 - suffix_len] ===
      new_val[new_val.length - 1 - suffix_len]
  ) {
    suffix_len++;
  }

  if (suffix_len > 0) {
    while (suffix_len > 0) {
      let target_split = false;
      if (suffix_len < target.length) {
        target_split =
          !isSpace(target[target.length - 1 - suffix_len]) &&
          !isSpace(target[target.length - suffix_len]);
      }
      let new_split = false;
      if (suffix_len < new_val.length) {
        new_split =
          !isSpace(new_val[new_val.length - 1 - suffix_len]) &&
          !isSpace(new_val[new_val.length - suffix_len]);
      }
      if (target_split || new_split) {
        suffix_len--;
      } else {
        break;
      }
    }
  }

  while (suffix_len > 0) {
    const idx = target.length - suffix_len;
    if (idx > 0) {
      const charSeq = target.substring(idx - 1, idx + 1);
      if (charSeq === "**" || charSeq === "__") {
        suffix_len--;
        continue;
      }
    }

    const right = target.substring(target.length - suffix_len);
    const b_count = (right.match(/\*\*/g) || []).length;
    const u2_count = (right.match(/__/g) || []).length;
    const u1_count = _count_standalone_underscores(right);

    if (b_count % 2 !== 0) {
      suffix_len -= right.indexOf("**") + 2;
      continue;
    }
    if (u2_count % 2 !== 0) {
      suffix_len -= right.indexOf("__") + 2;
      continue;
    }
    if (u1_count % 2 !== 0) {
      let idx_in_right = 0;
      const isAlnum = (char: string) => /[a-zA-Z0-9]/.test(char);
      while (idx_in_right < right.length) {
        if (
          right[idx_in_right] === "_" &&
          (idx_in_right === 0 || right[idx_in_right - 1] !== "_") &&
          (idx_in_right === right.length - 1 || right[idx_in_right + 1] !== "_")
        ) {
          const is_intra = idx_in_right > 0 && isAlnum(right[idx_in_right - 1]) && idx_in_right < right.length - 1 && isAlnum(right[idx_in_right + 1]);
          if (!is_intra) {
            suffix_len -= idx_in_right + 1;
            break;
          }
        }
        idx_in_right++;
      }
      continue;
    }
    break;
  }

  if (
    suffix_len > 0 &&
    /^\s+$/.test(target.substring(target.length - suffix_len))
  ) {
    suffix_len = 0;
  }

  // Absorb balanced wrappers
  for (const marker of ["**", "__", "_"]) {
    const mlen = marker.length;
    const tgt_rem = target.substring(prefix_len, target.length - suffix_len);
    const new_rem = new_val.substring(prefix_len, new_val.length - suffix_len);

    if (
      tgt_rem.startsWith(marker) &&
      new_rem.startsWith(marker) &&
      tgt_rem.endsWith(marker) &&
      new_rem.endsWith(marker) &&
      tgt_rem.length >= 2 * mlen &&
      new_rem.length >= 2 * mlen
    ) {
      prefix_len += mlen;
      suffix_len += mlen;
    }
  }

  return [prefix_len, suffix_len];
}

function _words_to_chars(
  text1: string,
  text2: string,
): [string, string, string[]] {
  const token_array: string[] = [];
  const token_hash: Record<string, number> = {};

  // RegExp equivalent to Python's r"(\s+|\w+|[^\w\s])" with unicode support
  const split_pattern = /(\s+|[\p{L}\p{N}_]+|[^\p{L}\p{N}_\s])/gu;

  const encode_text = (text: string) => {
    // Keep delimiters via capture group in split
    const tokens = text.split(split_pattern).filter(Boolean);
    let encoded_chars = "";
    for (const token of tokens) {
      if (token in token_hash) {
        encoded_chars += String.fromCharCode(token_hash[token]);
      } else {
        const code = token_array.length;
        token_hash[token] = code;
        token_array.push(token);
        encoded_chars += String.fromCharCode(code);
      }
    }
    return encoded_chars;
  };

  return [encode_text(text1), encode_text(text2), token_array];
}

export function generate_edits_from_text(
  original_text: string,
  modified_text: string,
): ModifyText[] {
  const dmp = new diff_match_patch.diff_match_patch();
  dmp.Diff_Timeout = 2.0; // Enforce strict 2-second timeout to prevent deep recursion hangs

  const [chars1, chars2, token_array] = _words_to_chars(
    original_text,
    modified_text,
  );
  const diffs = dmp.diff_main(chars1, chars2, false);
  dmp.diff_cleanupSemantic(diffs);

  // Manually map characters back to words to bypass prototype volatility (diff_charsToLines_)
  for (let i = 0; i < diffs.length; i++) {
    const chars = diffs[i][1];
    let text = "";
    for (let j = 0; j < chars.length; j++)
      text += token_array[chars.charCodeAt(j)];
    diffs[i][1] = text;
  }

  const edits: ModifyText[] = [];
  let current_original_index = 0;
  let pending_delete: [number, string] | null = null;

  for (const [op, text] of diffs) {
    if (op === 0) {
      // Equal
      if (pending_delete) {
        const [idx, del_txt] = pending_delete;
        edits.push({
          type: "modify",
          target_text: del_txt,
          new_text: "",
          comment: "Diff: Text deleted",
          _match_start_index: idx,
        });
        pending_delete = null;
      }
      current_original_index += text.length;
    } else if (op === -1) {
      // Delete
      pending_delete = [current_original_index, text];
      current_original_index += text.length;
    } else if (op === 1) {
      // Insert
      if (pending_delete) {
        const [idx, del_txt] = pending_delete;
        edits.push({
          type: "modify",
          target_text: del_txt,
          new_text: text,
          comment: "Diff: Replacement",
          _match_start_index: idx,
        });
        pending_delete = null;
      } else {
        edits.push({
          type: "modify",
          target_text: "",
          new_text: text,
          comment: "Diff: Text inserted",
          _match_start_index: current_original_index,
        });
      }
    }
  }

  if (pending_delete) {
    const [idx, del_txt] = pending_delete;
    edits.push({
      type: "modify",
      target_text: del_txt,
      new_text: "",
      comment: "Diff: Text deleted",
      _match_start_index: idx,
    });
  }

  return edits;
}
export function create_unified_diff(
  original_text: string,
  modified_text: string,
  context_lines: number = 3,
): string {
  const dmp = new diff_match_patch.diff_match_patch();
  dmp.Diff_Timeout = 2.0;

  const a = dmp.diff_linesToChars_(original_text, modified_text);
  const diffs = dmp.diff_main(a.chars1, a.chars2, false);
  dmp.diff_charsToLines_(diffs, a.lineArray);

  const output: string[] = [];
  output.push("--- Original");
  output.push("+++ Modified");

  let i = 0;
  while (i < diffs.length) {
    while (i < diffs.length && diffs[i][0] === 0) i++;
    if (i >= diffs.length) break;

    let start = i;
    let preContext: string[] = [];
    if (start > 0 && diffs[start - 1][0] === 0) {
      const lines = diffs[start - 1][1].replace(/\n$/, "").split("\n");
      preContext = lines.slice(-context_lines);
    }

    const chunk: string[] = [];
    chunk.push(...preContext.map((l) => ` ${l}`));

    while (i < diffs.length) {
      const [op, text] = diffs[i];
      const lines = text.replace(/\n$/, "").split("\n");

      if (op === 0) {
        if (lines.length > context_lines * 2) break;
        chunk.push(...lines.map((l) => ` ${l}`));
      } else {
        const prefix = op === -1 ? "-" : "+";
        chunk.push(...lines.map((l) => `${prefix}${l}`));
      }
      i++;
    }

    let postContext: string[] = [];
    if (i < diffs.length && diffs[i][0] === 0) {
      const lines = diffs[i][1].replace(/\n$/, "").split("\n");
      postContext = lines.slice(0, context_lines);
    }
    chunk.push(...postContext.map((l) => ` ${l}`));

    output.push("@@ ... @@");
    output.push(...chunk);
  }

  if (output.length === 2) return ""; // No changes
  return output.join("\n");
}

export function create_word_patch_diff(
  original_text: string,
  modified_text: string,
  original_path: string = "Original",
  modified_path: string = "Modified"
): string {
  const edits = generate_edits_from_text(original_text, modified_text);
  const output: string[] = [
    `--- ${original_path}`,
    `+++ ${modified_path}`,
    ""
  ];
  
  const CONTEXT_SIZE = 40;

  for (const edit of edits) {
    const raw_start = edit._match_start_index || 0;
    const raw_target = edit.target_text || "";
    const raw_new = edit.new_text || "";

    const [prefix_len, suffix_len] = trim_common_context(raw_target, raw_new);

    const target_end_in_target = raw_target.length - suffix_len;
    const new_end_in_new = raw_new.length - suffix_len;

    const display_target = raw_target.substring(prefix_len, target_end_in_target);
    const display_new = raw_new.substring(prefix_len, new_end_in_new);

    const change_start = raw_start + prefix_len;
    const change_end = change_start + display_target.length;

    let pre_start = Math.max(0, change_start - CONTEXT_SIZE);
    let pre_context = original_text.substring(pre_start, change_start);
    if (pre_start > 0) pre_context = "..." + pre_context;

    let post_end = Math.min(original_text.length, change_end + CONTEXT_SIZE);
    let post_context = original_text.substring(change_end, post_end);
    if (post_end < original_text.length) post_context = post_context + "...";

    pre_context = pre_context.replace(/\n/g, " ").replace(/\r/g, "");
    post_context = post_context.replace(/\n/g, " ").replace(/\r/g, "");

    output.push("@@ Word Patch @@");
    output.push(` ${pre_context}`);
    if (display_target) output.push(`- ${display_target}`);
    if (display_new) output.push(`+ ${display_new}`);
    output.push(` ${post_context}`);
    output.push("");
  }

  return output.join("\n");
}
