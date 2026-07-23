import { unzipSync } from "fflate";
import diff_match_patch from "diff-match-patch";
import { DeleteTableRow, InsertTableRow, ModifyText } from "./models.js";
import type {
  ExtractStructure,
  RowGeometry,
  TableGeometry,
} from "./ingest.js";

export type DiffEdit = ModifyText | InsertTableRow | DeleteTableRow;

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
// ---------------------------------------------------------------------------
// Structured (part- and table-aware) diff — QA 2026-07-18 C1/C2.
// ---------------------------------------------------------------------------

type Opcode = [
  tag: "equal" | "replace" | "delete" | "insert",
  i1: number,
  i2: number,
  j1: number,
  j2: number,
];

/**
 * difflib.SequenceMatcher.get_opcodes() equivalent over string arrays,
 * LCS-based. The row-key arrays this runs on are tiny (table row counts), so
 * the O(n*m) table is negligible. Adjacent non-equal steps coalesce into one
 * "replace"/"delete"/"insert" block exactly like difflib's opcodes.
 */
function _sequence_opcodes(a: string[], b: string[]): Opcode[] {
  const n = a.length;
  const m = b.length;
  // dp[i][j] = LCS length of a[i:] vs b[j:]
  const dp: Int32Array[] = [];
  for (let i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const ops: Opcode[] = [];
  let i = 0;
  let j = 0;
  let pend_i1 = 0;
  let pend_j1 = 0;
  let pend_del = 0;
  let pend_ins = 0;

  const flushPending = () => {
    if (pend_del === 0 && pend_ins === 0) return;
    const tag =
      pend_del > 0 && pend_ins > 0
        ? "replace"
        : pend_del > 0
          ? "delete"
          : "insert";
    ops.push([tag, pend_i1, pend_i1 + pend_del, pend_j1, pend_j1 + pend_ins]);
    pend_del = 0;
    pend_ins = 0;
  };

  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) {
      flushPending();
      const i1 = i;
      const j1 = j;
      while (i < n && j < m && a[i] === b[j]) {
        i++;
        j++;
      }
      ops.push(["equal", i1, i, j1, j]);
    } else {
      if (pend_del === 0 && pend_ins === 0) {
        pend_i1 = i;
        pend_j1 = j;
      }
      if (j < m && (i === n || dp[i][j + 1] >= dp[i + 1][j])) {
        pend_ins++;
        j++;
      } else {
        pend_del++;
        i++;
      }
    }
  }
  flushPending();
  return ops;
}

/**
 * True when a "\n\n"-separated block reads as projected table rows: every
 * line carries the " | " cell separator. Table rows are separated by single
 * newlines, so a whole table is one block in the paragraph alignment.
 */
function _is_table_blob(block: string): boolean {
  const lines = block.split("\n");
  return lines.length > 0 && lines.every((line) => line.includes(" | "));
}

/**
 * Pairwise row-level edits between two aligned table blobs, or null when the
 * blobs cannot be row-aligned (different row counts — a structural change the
 * text path hands to the engine's row guards). Row edits are unpinned: the
 * full row text is the anchor, and the engine's per-cell splitter resolves
 * them — word-level hunks across " | " separators land in the wrong cell.
 */
function _table_blob_row_edits(
  orig_blob: string,
  mod_blob: string,
): ModifyText[] | null {
  const rows_o = orig_blob.split("\n");
  const rows_m = mod_blob.split("\n");
  if (rows_o.length !== rows_m.length) return null;
  const row_edits: ModifyText[] = [];
  for (let k = 0; k < rows_o.length; k++) {
    if (rows_o[k] === rows_m[k]) continue;
    row_edits.push({
      type: "modify",
      target_text: rows_o[k],
      new_text: rows_m[k],
      comment: "Diff: Table row modified",
      _is_table_edit: true,
    });
  }
  return row_edits;
}

/**
 * Splits generated hunks the engine would reject: an UNBALANCED target
 * spanning a paragraph break with body text on both sides ("tail of A\n\n
 * head of B" — the shape dmp produces when adjacent paragraphs share a
 * prefix). Each leading paragraph piece becomes its own separator-carrying
 * deletion (an allowed merge shape) and the final piece carries the whole
 * replacement text; sequential application produces the identical merged
 * result. Without this, one such hunk poisons the entire batch — apply is
 * all-or-nothing (QA 2026-07-19 ADEU-QA-002 A).
 */
function _split_cross_paragraph_hunks(edits: ModifyText[]): ModifyText[] {
  const out: ModifyText[] = [];
  for (const e of edits) {
    const target = e.target_text || "";
    const newText = e.new_text || "";
    const idx = e._match_start_index;
    if (
      idx === undefined ||
      idx === null ||
      !target.includes("\n\n") ||
      target.split("\n\n").length - 1 === newText.split("\n\n").length - 1
    ) {
      out.push(e);
      continue;
    }
    const parts = target.split("\n\n");
    if (parts.length < 2 || !parts[0].trim() || !parts[parts.length - 1].trim()) {
      // One-sided shapes (separator-carrying deletions/insertions) are the
      // engine's supported merge protocol — leave them intact.
      out.push(e);
      continue;
    }
    let offset = idx;
    for (const piece of parts.slice(0, -1)) {
      out.push({
        type: "modify",
        target_text: piece + "\n\n",
        new_text: "",
        comment: e.comment || "Diff: Text deleted",
        _match_start_index: offset,
      });
      offset += piece.length + 2;
    }
    out.push({
      type: "modify",
      target_text: parts[parts.length - 1],
      new_text: newText,
      comment: e.comment,
      _match_start_index: offset,
    });
  }
  return out;
}

/**
 * Aligns original and modified text by paragraph (using the difflib-style
 * opcodes), then performs precise word-level diffing on replaced blocks.
 * Whole-paragraph structural changes come out as engine-applicable shapes:
 * a deleted paragraph is ONE deletion per paragraph carrying its "\n\n"
 * separator, an inserted paragraph carries its separator — a raw word-level
 * diff instead legally shifts hunk boundaries across paragraphs sharing a
 * prefix ("...arrears.\n\nThe "), which the engine rightly rejects; every
 * paragraph deletion/reordering in the QA corpus failed replay that way
 * (QA 2026-07-19 ADEU-QA-002 A). Mirrors the Python engine's
 * generate_edits_via_paragraph_alignment.
 */
export function generate_edits_via_paragraph_alignment(
  original_text: string,
  modified_text: string,
): ModifyText[] {
  // Normalize trailing whitespace/newlines in modified_text to match original_text
  const orig_stripped = original_text.trimEnd();
  const orig_ws = original_text.slice(orig_stripped.length);
  const mod_stripped = modified_text.trimEnd();
  modified_text = mod_stripped + orig_ws;

  const orig_paragraphs = original_text.split("\n\n");
  const mod_paragraphs = modified_text.split("\n\n");

  const orig_offsets: number[] = [];
  let current_offset = 0;
  for (const p of orig_paragraphs) {
    orig_offsets.push(current_offset);
    current_offset += p.length + 2;
  }

  const opcodes = _sequence_opcodes(orig_paragraphs, mod_paragraphs);
  const edits: ModifyText[] = [];

  for (const [tag, i1, i2, j1, j2] of opcodes) {
    if (tag === "equal") continue;

    const offset =
      i1 < orig_offsets.length ? orig_offsets[i1] : original_text.length;

    if (tag === "delete") {
      // Multi-paragraph mid-document blocks are emitted as ONE deletion PER
      // paragraph ("A\n\n", "B\n\n"), never "A\n\nB\n\n": the engine's merge
      // protocol supports one deleted paragraph break per edit
      // (QA 2026-07-19 ADEU-QA-002 A). The document's trailing block takes
      // the LEADING separator instead (QA 2026-07-19 v8 F-12 fallout).
      if (i2 < orig_paragraphs.length) {
        let piece_offset = offset;
        for (let k = i1; k < i2; k++) {
          const piece = orig_paragraphs[k] + "\n\n";
          edits.push({
            type: "modify",
            target_text: piece,
            new_text: "",
            comment: "Diff: Text deleted",
            _match_start_index: piece_offset,
          });
          piece_offset += piece.length;
        }
      } else {
        let deleted_text = orig_paragraphs.slice(i1, i2).join("\n\n");
        let del_offset = offset;
        if (i1 > 0) {
          deleted_text = "\n\n" + deleted_text;
          del_offset -= 2;
        }
        edits.push({
          type: "modify",
          target_text: deleted_text,
          new_text: "",
          comment: "Diff: Text deleted",
          _match_start_index: del_offset,
        });
      }
    } else if (tag === "insert") {
      // An inserted paragraph must CARRY its paragraph separator, or the
      // engine (rightly) treats the text as an inline insertion and glues it
      // to the neighboring paragraph (QA 2026-07-18 v6 H2).
      let inserted_text = mod_paragraphs.slice(j1, j2).join("\n\n");
      if (i1 < orig_paragraphs.length) {
        inserted_text = inserted_text + "\n\n";
      } else {
        inserted_text = "\n\n" + inserted_text;
      }
      edits.push({
        type: "modify",
        target_text: "",
        new_text: inserted_text,
        comment: "Diff: Text inserted",
        _match_start_index: offset,
      });
    } else if (tag === "replace") {
      // Table blobs in equal-count replace blocks pair up positionally and
      // diff as ROW-LEVEL edits; word-level hunks over a table blob start or
      // end inside " | " separators and land in the wrong cell. Prose pairs
      // (and any block whose counts differ) keep the word-level chunk diff.
      if (
        i2 - i1 === j2 - j1 &&
        Array.from({ length: i2 - i1 }).some(
          (_, k) =>
            _is_table_blob(orig_paragraphs[i1 + k]) ||
            _is_table_blob(mod_paragraphs[j1 + k]),
        )
      ) {
        for (let k = 0; k < i2 - i1; k++) {
          const orig_p = orig_paragraphs[i1 + k];
          const mod_p = mod_paragraphs[j1 + k];
          if (orig_p === mod_p) continue;
          const pair_offset = orig_offsets[i1 + k];
          const row_edits =
            _is_table_blob(orig_p) && _is_table_blob(mod_p)
              ? _table_blob_row_edits(orig_p, mod_p)
              : null;
          if (row_edits !== null) {
            edits.push(...row_edits);
            continue;
          }
          const pair_edits = generate_edits_from_text(orig_p, mod_p);
          for (const ce of pair_edits) {
            ce._match_start_index = (ce._match_start_index || 0) + pair_offset;
            edits.push(ce);
          }
        }
        continue;
      }

      const orig_chunk = orig_paragraphs.slice(i1, i2).join("\n\n");
      const mod_chunk = mod_paragraphs.slice(j1, j2).join("\n\n");
      const chunk_edits = generate_edits_from_text(orig_chunk, mod_chunk);
      for (const ce of chunk_edits) {
        ce._match_start_index = (ce._match_start_index || 0) + offset;
        edits.push(ce);
      }
    }
  }

  // Word-level diffs over unequal replace chunks can still emit hunks that
  // straddle a paragraph break with body text on both sides; split them into
  // engine-applicable pieces (ADEU-QA-002 A).
  return _split_cross_paragraph_hunks(edits);
}

const _IMAGE_MARKER_RE = /!\[[^\]]*\]\(docx-image:[^)]*\)/g;

/**
 * Removes generated edits whose image-marker multisets differ between
 * target and new text. The engine (validate_edit_strings) rightly refuses
 * such edits — markers are read-only projections — so emitting them would
 * make the diff pipeline reject its own output. An added/removed image is
 * reported as a warning instead of an unappliable edit.
 */
function _drop_image_marker_hunks(
  edits: ModifyText[],
  warnings: string[],
): ModifyText[] {
  const _normalized = (s: string): string =>
    s.replace(_IMAGE_MARKER_RE, "").replace(/\s+/g, " ").trim();

  const kept: ModifyText[] = [];
  for (const e of edits) {
    const t_imgs = ((e.target_text || "").match(_IMAGE_MARKER_RE) || []).sort();
    const n_imgs = ((e.new_text || "").match(_IMAGE_MARKER_RE) || []).sort();
    if (JSON.stringify(t_imgs) === JSON.stringify(n_imgs)) {
      kept.push(e);
      continue;
    }
    if (_normalized(e.target_text || "") === _normalized(e.new_text || "")) {
      warnings.push(
        "An inline image was added or removed between the documents. Images cannot be " +
          "transferred by text edits — the image-only difference was skipped; apply it " +
          "manually in Word.",
      );
    } else {
      warnings.push(
        "A text change overlapping an added/removed inline image was skipped — images " +
          "cannot be transferred by text edits. Apply that section manually in Word.",
      );
    }
  }
  return kept;
}

/**
 * Removes generated hunks whose pinned range cuts INTO a read-only image
 * marker without covering it whole — the shape an alt-text-only difference
 * produces (`RED` -> `BLUE` inside `![RED logo](docx-image:1)`). The engine
 * categorically rejects such edits, so emitting them makes diff output
 * unappliable by apply (QA 2026-07-19 F-04/F-14). Hunks that contain
 * complete markers are judged by _drop_image_marker_hunks instead.
 */
function _drop_marker_interior_hunks(
  edits: ModifyText[],
  text_orig: string,
  warnings: string[],
): ModifyText[] {
  const marker_spans: [number, number][] = [];
  for (const m of text_orig.matchAll(_IMAGE_MARKER_RE)) {
    marker_spans.push([m.index!, m.index! + m[0].length]);
  }
  if (marker_spans.length === 0) return edits;

  const kept: ModifyText[] = [];
  let warned = false;
  for (const e of edits) {
    const idx = e._match_start_index;
    if (idx === undefined || idx === null) {
      kept.push(e);
      continue;
    }
    const end = idx + (e.target_text || "").length;
    const cuts_into_marker = marker_spans.some(
      ([m_start, m_end]) =>
        m_start < end && idx < m_end && !(idx <= m_start && m_end <= end),
    );
    if (!cuts_into_marker) {
      kept.push(e);
      continue;
    }
    if (!warned) {
      warnings.push(
        "An image's alternative text differs between the documents. Image markers " +
          "(![alt](docx-image:N)) are read-only projections, so this difference cannot be " +
          "expressed as a text edit — it was skipped; update the image or its alt text " +
          "manually in Word.",
      );
      warned = true;
    }
  }
  return kept;
}

/**
 * True when every projected row reads exactly as its cells joined by " | ".
 * Rows wrapped in tracked-row CriticMarkup ({++ … ++} / {-- … --}) do not,
 * and such tables are diffed as plain text rather than as row structures.
 */
function _rows_are_plain(text: string, table: TableGeometry): boolean {
  for (const row of table.rows) {
    if (text.substring(row.start, row.end) !== row.cells.join(" | ")) {
      return false;
    }
  }
  return true;
}

const _ROW_KEY_SEP = "\x1f";

/**
 * Row-level alignment opcodes between two tables, or null when the row sets
 * differ only cell-internally (no rows added/removed) — the caller then
 * keeps fine-grained word-level text edits instead of row operations.
 */
function _table_row_opcodes(
  rows_o: RowGeometry[],
  rows_m: RowGeometry[],
): Opcode[] | null {
  const keys_o = rows_o.map((r) => r.cells.join(_ROW_KEY_SEP));
  const keys_m = rows_m.map((r) => r.cells.join(_ROW_KEY_SEP));
  const opcodes = _sequence_opcodes(keys_o, keys_m);
  for (const [tag, i1, i2, j1, j2] of opcodes) {
    if (
      tag === "insert" ||
      tag === "delete" ||
      (tag === "replace" && i2 - i1 !== j2 - j1)
    ) {
      return opcodes;
    }
  }
  return null;
}

/**
 * Emits structured operations (delete_row / insert_row / per-row modify)
 * that transform table_o's row set into table_m's, following the alignment
 * `opcodes` from _table_row_opcodes (QA 2026-07-18 C2: a generic text edit
 * cannot add or remove rows — it writes fake pipe text into one cell or is
 * rejected by the cell-count validator).
 */
function _row_ops_for_table(
  table_o: TableGeometry,
  table_m: TableGeometry,
  opcodes: Opcode[],
  warnings: string[],
): DiffEdit[] {
  const rows_o = table_o.rows;
  const rows_m = table_m.rows;
  const keys_o = rows_o.map((r) => r.cells.join(_ROW_KEY_SEP));

  const row_text = (r: RowGeometry): string => r.cells.join(" | ");

  // Duplicate row texts make text-anchored row operations ambiguous. The
  // engine fails closed with a strict-mode ambiguity error at apply time;
  // tell the user up front so the rejection is not a surprise.
  if (new Set(keys_o).size !== keys_o.length) {
    warnings.push(
      "A table contains rows with identical text; the generated row operations anchor by " +
        "row text and may be rejected as ambiguous at apply time. If that happens, apply the " +
        "row changes with explicit insert_row/delete_row edits.",
    );
  }

  // Rows removed by the transformation (deletes + surplus rows of shrinking
  // replaces) can never anchor an insert.
  const removed = new Set<number>();
  const replaced_new_text: Record<number, string> = {};
  for (const [tag, i1, i2, j1, j2] of opcodes) {
    if (tag === "delete") {
      for (let k = i1; k < i2; k++) removed.add(k);
    } else if (tag === "replace") {
      const pairs = Math.min(i2 - i1, j2 - j1);
      for (let k = i1 + pairs; k < i2; k++) removed.add(k);
      for (let k = 0; k < pairs; k++) {
        replaced_new_text[i1 + k] = row_text(rows_m[j1 + k]);
      }
    }
  }

  const surviving: number[] = [];
  for (let k = 0; k < rows_o.length; k++) {
    if (!removed.has(k)) surviving.push(k);
  }

  const anchor_text = (orig_idx: number): string => {
    // Anchor on the row's FINAL text: modified rows are matched via the
    // engine's clean-view fallback after their own edit applies.
    return replaced_new_text[orig_idx] !== undefined
      ? replaced_new_text[orig_idx]
      : row_text(rows_o[orig_idx]);
  };

  const insert_ops = (
    new_rows: RowGeometry[],
    at_orig_index: number,
  ): DiffEdit[] => {
    const ops: DiffEdit[] = [];
    const before = surviving.filter((k) => k < at_orig_index);
    const after = surviving.filter((k) => k >= at_orig_index);
    if (before.length > 0) {
      // Insert below the preceding surviving row. Emitting in reverse keeps
      // the final order: below-A(B), then below-A(C) yields A,C,B — so emit
      // C first.
      const anchor_idx = before[before.length - 1];
      const anchor = anchor_text(anchor_idx);
      for (const r of [...new_rows].reverse()) {
        ops.push({
          type: "insert_row",
          target_text: anchor,
          position: "below",
          cells: [...r.cells],
          // Pin to the anchor row's offset: text anchors alone are
          // ambiguous when tables share identical rows. Pins do not
          // survive JSON round-trips (the strict text match applies then,
          // failing closed with the duplicate-row warning above).
          _match_start_index: rows_o[anchor_idx].start,
        });
      }
    } else if (after.length > 0) {
      const anchor_idx = after[0];
      const anchor = anchor_text(anchor_idx);
      for (const r of new_rows) {
        ops.push({
          type: "insert_row",
          target_text: anchor,
          position: "above",
          cells: [...r.cells],
          _match_start_index: rows_o[anchor_idx].start,
        });
      }
    } else {
      warnings.push(
        "A table gained rows but no original row survives to anchor them; " +
          "these row insertions were skipped — add them with explicit insert_row operations.",
      );
    }
    return ops;
  };

  const ops: DiffEdit[] = [];
  for (const [tag, i1, i2, j1, j2] of opcodes) {
    if (tag === "equal") continue;
    if (tag === "replace") {
      const pairs = Math.min(i2 - i1, j2 - j1);
      for (let k = 0; k < pairs; k++) {
        const o_txt = row_text(rows_o[i1 + k]);
        const m_txt = row_text(rows_m[j1 + k]);
        if (o_txt !== m_txt) {
          ops.push({
            type: "modify",
            target_text: o_txt,
            new_text: m_txt,
            comment: "Diff: Table row modified",
            _is_table_edit: true,
          });
        }
      }
      for (let k = i1 + pairs; k < i2; k++) {
        ops.push({
          type: "delete_row",
          target_text: row_text(rows_o[k]),
          _match_start_index: rows_o[k].start,
        });
      }
      const surplus_new = rows_m.slice(j1 + pairs, j2);
      if (surplus_new.length > 0) {
        ops.push(...insert_ops(surplus_new, i2));
      }
    } else if (tag === "delete") {
      for (let k = i1; k < i2; k++) {
        ops.push({
          type: "delete_row",
          target_text: row_text(rows_o[k]),
          _match_start_index: rows_o[k].start,
        });
      }
    } else if (tag === "insert") {
      ops.push(...insert_ops(rows_m.slice(j1, j2), i1));
    }
  }
  return ops;
}

/**
 * DOCX-to-DOCX diff over projections extracted with return_structure=true.
 *
 * Improvements over a flat generate_edits_from_text pass:
 *   - each OPC part (header/body/footer/notes) diffs against its
 *     counterpart, so no edit can span a part boundary and content that
 *     moved between parts is reported instead of hidden (QA 2026-07-18 C1);
 *   - table row insertions/deletions become structured insert_row /
 *     delete_row operations instead of pipe-text edits (QA C2).
 *
 * Every ModifyText keeps its _match_start_index pinned into text_orig, which
 * the engine consumes positionally (the Node engine has no
 * make_edits_self_contained JSON round trip like the Python CLI).
 *
 * Returns { edits, warnings }. Warnings describe fallbacks the caller should
 * surface (differing part layouts, unanchorable rows, …).
 */
export function generate_structured_edits(
  text_orig: string,
  struct_orig: ExtractStructure,
  text_mod: string,
  struct_mod: ExtractStructure,
): { edits: DiffEdit[]; warnings: string[] } {
  const warnings: string[] = [];
  const edits: DiffEdit[] = [];

  const kinds_o = struct_orig.part_ranges
    .filter(([s, e]) => e > s)
    .map(([, , k]) => k);
  const kinds_m = struct_mod.part_ranges
    .filter(([s, e]) => e > s)
    .map(([, , k]) => k);

  if (JSON.stringify(kinds_o) !== JSON.stringify(kinds_m)) {
    warnings.push(
      `The documents have different part layouts (${kinds_o.join(" + ") || "none"} vs ` +
        `${kinds_m.join(" + ") || "none"}); comparing flattened text instead. Header/footer ` +
        "additions or removals cannot be expressed as text edits.",
    );
    const flat = _drop_marker_interior_hunks(
      _drop_image_marker_hunks(
        generate_edits_via_paragraph_alignment(text_orig, text_mod),
        warnings,
      ),
      text_orig,
      warnings,
    );
    return { edits: [...flat], warnings };
  }

  const ranges_o = struct_orig.part_ranges
    .filter(([s, e]) => e > s)
    .map(([s, e]) => [s, e] as [number, number]);
  const ranges_m = struct_mod.part_ranges
    .filter(([s, e]) => e > s)
    .map(([s, e]) => [s, e] as [number, number]);

  for (let p = 0; p < ranges_o.length; p++) {
    const [po_start, po_end] = ranges_o[p];
    const [pm_start, pm_end] = ranges_m[p];

    const tables_o = struct_orig.tables.filter(
      (t) => po_start <= t.start && t.end <= po_end,
    );
    const tables_m = struct_mod.tables.filter(
      (t) => pm_start <= t.start && t.end <= pm_end,
    );

    const tables_alignable =
      tables_o.length === tables_m.length &&
      tables_o.every((t) => _rows_are_plain(text_orig, t)) &&
      tables_m.every((t) => _rows_are_plain(text_mod, t));
    if (tables_o.length !== tables_m.length) {
      warnings.push(
        `A ${kinds_o[p]} part has ${tables_o.length} table(s) in the ` +
          `original but ${tables_m.length} in the modified document; its tables were compared as plain ` +
          "text. Adding or removing whole tables is not supported via diff/apply.",
      );
    }

    if (!tables_alignable) {
      const part_edits = generate_edits_via_paragraph_alignment(
        text_orig.substring(po_start, po_end),
        text_mod.substring(pm_start, pm_end),
      );
      for (const e of part_edits) {
        e._match_start_index = (e._match_start_index || 0) + po_start;
      }
      edits.push(...part_edits);
      continue;
    }

    // Walk interleaved segments: text-before-table, table, text-after…
    const boundaries_o: [number, number][] = [
      [po_start, po_start],
      ...tables_o.map((t) => [t.start, t.end] as [number, number]),
      [po_end, po_end],
    ];
    const boundaries_m: [number, number][] = [
      [pm_start, pm_start],
      ...tables_m.map((t) => [t.start, t.end] as [number, number]),
      [pm_end, pm_end],
    ];

    for (let seg_idx = 0; seg_idx < boundaries_o.length - 1; seg_idx++) {
      const seg_o_start = boundaries_o[seg_idx][1];
      const seg_o_end = boundaries_o[seg_idx + 1][0];
      const seg_m_start = boundaries_m[seg_idx][1];
      const seg_m_end = boundaries_m[seg_idx + 1][0];
      // Paragraph alignment, never a raw word-level diff over the whole
      // segment: dmp legally shifts hunk boundaries across paragraphs
      // sharing a prefix, and the engine rightly rejects a deletion with
      // body text on both sides of a paragraph break (ADEU-QA-002 A).
      const seg_edits = generate_edits_via_paragraph_alignment(
        text_orig.substring(seg_o_start, seg_o_end),
        text_mod.substring(seg_m_start, seg_m_end),
      );
      for (const e of seg_edits) {
        e._match_start_index = (e._match_start_index || 0) + seg_o_start;
      }
      edits.push(...seg_edits);

      if (seg_idx < tables_o.length) {
        const t_o = tables_o[seg_idx];
        const t_m = tables_m[seg_idx];
        const row_opcodes = _table_row_opcodes(t_o.rows, t_m.rows);
        if (row_opcodes !== null) {
          edits.push(..._row_ops_for_table(t_o, t_m, row_opcodes, warnings));
        } else {
          // Cell-internal changes only (row sets align 1:1). Emit one
          // ROW-LEVEL edit per differing row — the engine splits it into
          // per-cell sub-edits along the " | " boundaries. A word-level diff
          // over the whole table span produces hunks that start or end
          // inside a cell separator, which apply into the wrong cell or
          // write literal pipe text. Unpinned like every other table edit:
          // pinned application bypasses the cell splitter, and the full row
          // text is the anchor contract.
          for (let k = 0; k < t_o.rows.length; k++) {
            const o_txt = t_o.rows[k].cells.join(" | ");
            const m_txt = t_m.rows[k].cells.join(" | ");
            if (o_txt === m_txt) continue;
            edits.push({
              type: "modify",
              target_text: o_txt,
              new_text: m_txt,
              comment: "Diff: Table row modified",
              _is_table_edit: true,
            });
          }
        }
      }
    }
  }

  // Row operations anchor by row text (pins do not survive JSON): if the
  // anchor text also appears elsewhere in the document — e.g. two tables
  // sharing a header row — the strict text match at apply time is
  // ambiguous. In-process consumers ride the pinned offsets; JSON consumers
  // fail closed, so tell the user why up front.
  const countOccurrences = (haystack: string, needle: string): number => {
    let count = 0;
    let from = 0;
    while (true) {
      const idx = haystack.indexOf(needle, from);
      if (idx === -1) break;
      count++;
      from = idx + needle.length;
    }
    return count;
  };
  let ambiguous_anchor_warned = false;
  for (const e of edits) {
    if (
      (e.type === "insert_row" ||
        e.type === "delete_row" ||
        (e as any)._is_table_edit) &&
      !ambiguous_anchor_warned
    ) {
      if (e.target_text && countOccurrences(text_orig, e.target_text) > 1) {
        warnings.push(
          `The row anchor "${e.target_text.substring(0, 60)}" appears more than once in the document. ` +
            "Applying this diff from its JSON output may be rejected as ambiguous — " +
            "make the anchor rows unique, or apply the row changes with explicit " +
            "insert_row/delete_row edits.",
        );
        ambiguous_anchor_warned = true;
      }
    }
  }

  // Our own output must never trip the engine's read-only image-marker
  // validation: an added/removed image becomes a warning, not an edit —
  // and so does an alt-text change, whose hunk lands INSIDE a marker.
  const modify_edits = edits.filter(
    (e): e is ModifyText => e.type === "modify",
  );
  const kept_modifies = new Set(
    _drop_marker_interior_hunks(
      _drop_image_marker_hunks(modify_edits, warnings),
      text_orig,
      warnings,
    ),
  );
  const final_edits = edits.filter(
    (e) => e.type !== "modify" || kept_modifies.has(e as ModifyText),
  );

  return { edits: final_edits, warnings };
}

/**
 * Compares embedded media bytes (word/media/*) between two DOCX packages.
 * Adeu's diff is a text comparison: two documents whose images differ but
 * whose projections agree produce an empty diff, which reads as "visually
 * identical" unless the caller says otherwise (QA 2026-07-19 F-04). Returns
 * warning strings describing changed/added/removed media members; empty when
 * the media sets are byte-identical (or either package is unreadable — the
 * caller has already surfaced package-level errors).
 */
export function collect_media_difference_warnings(
  original_docx: Uint8Array,
  modified_docx: Uint8Array,
): string[] {
  const media_hashes = (data: Uint8Array): Map<string, string> => {
    const hashes = new Map<string, string>();
    try {
      const unzipped = unzipSync(data);
      for (const [name, bytes] of Object.entries(unzipped)) {
        if (!name.startsWith("word/media/")) continue;
        // FNV-1a over the bytes: cheap, dependency-free content fingerprint.
        let h1 = 0x811c9dc5;
        let h2 = 0xcbf29ce4;
        for (let i = 0; i < bytes.length; i++) {
          h1 = Math.imul(h1 ^ bytes[i], 0x01000193) >>> 0;
          h2 = Math.imul(h2 ^ bytes[bytes.length - 1 - i], 0x01000193) >>> 0;
        }
        hashes.set(name, `${bytes.length}:${h1.toString(16)}:${h2.toString(16)}`);
      }
    } catch {
      return new Map();
    }
    return hashes;
  };

  const hashes_orig = media_hashes(original_docx);
  const hashes_mod = media_hashes(modified_docx);

  const changed: string[] = [];
  const added: string[] = [];
  const removed: string[] = [];
  for (const [name, hash] of hashes_orig) {
    if (!hashes_mod.has(name)) removed.push(name);
    else if (hashes_mod.get(name) !== hash) changed.push(name);
  }
  for (const name of hashes_mod.keys()) {
    if (!hashes_orig.has(name)) added.push(name);
  }
  changed.sort();
  added.sort();
  removed.sort();
  if (changed.length + added.length + removed.length === 0) return [];

  const parts: string[] = [];
  if (changed.length) parts.push(`${changed.length} changed`);
  if (added.length) parts.push(`${added.length} added`);
  if (removed.length) parts.push(`${removed.length} removed`);
  const names = [...changed, ...added, ...removed].slice(0, 5).join(", ");
  return [
    `The documents' embedded media differ (${parts.join(", ")}: ${names}). This diff compares ` +
      "TEXT only — an empty edit list does not mean the documents are visually identical. " +
      "Image changes must be applied manually in Word.",
  ];
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
