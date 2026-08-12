// FILE: node/packages/mcp-server/src/ledger.ts
//
// The changes ledger (`read_docx mode='changes'`): every tracked change and
// comment in the document enumerated as one bounded, <=18 tokens/change list.
//
// A literal port of python/src/adeu/mcp_components/_response_builders.py:1372-1815
// (`_parse_com_header`, `_LedgerEntry`, `build_changes_response`). The Python
// engine's output is the contract — shared/conformance/goldens/ledger_*.txt are
// compared byte-for-byte by conformance.test.ts, so every deviation below is a
// bug, not a style choice. The CLI flavour (`is_cli`) is not ported: the Node
// package has no CLI.

import { resolve, basename } from "node:path";
import { offset_to_page, paginate, parse_page_arg, split_structural_appendix } from "@adeu/core";
import type { ProjectionBundle, ToolResult } from "./response-builders.js";

/** Mirrors `_LedgerEntry` (`_response_builders.py:703-713`). */
interface LedgerEntry {
  /** "chg" or "com". */
  kind: string;
  /** e.g. "12" or "5". */
  cid: string;
  /** "ins", "del", "fmt", or "" for comments. */
  change_type: string;
  author: string;
  page: number;
  snippet: string;
  pair_ids: string[];
  reply_to_id: string | null;
  position: number;
}

const BUBBLE_RE = /\{>>([\s\S]*?)<<\}/g;
const WRAPPER_RE = /(\{\+\+|\{--|\{==)([\s\S]*?)(?:\+\+\}|--\}|==\})/g;
// `\w` -> `[\p{L}\p{N}_]` with the `u` flag: Python's `\w` is Unicode-aware
// (alphanumeric per `str.isalnum`, plus `_`), JS's is ASCII-only, so an id typed
// in a Word comment as `[Chg:١٢ delete]` or `[Com:مرجع]` is an id in Python and
// nothing in Node — the tag then leaks into the previous entry's author.
const TAG_RE = /\[(Chg|Com):([\p{L}\p{N}_]+)(?:\s+(insert|delete|format))?\]/gu;
const PAIR_RE = /\(pairs\s+(?:with\s+)?((?:Chg:[\p{L}\p{N}_]+(?:,\s*)?)+)\)/u;
const CHG_ID_RE = /Chg:([\p{L}\p{N}_]+)/gu;
const REPLY_RE = /\(reply\s+to\s+(Com:[\p{L}\p{N}_]+|[\p{L}\p{N}_]+)\)/u;
// re.sub replaces every occurrence, hence the `g`; `.` excludes newlines in both
// engines (Python without re.DOTALL, JS without `s`).
const AUTHOR_NOISE_RE = /\s*\((?:pairs(?:\s+with)?|reply\s+to)\s+.*?\)/g;

// `\Z` -> `$`: with no `m` flag JS `$` is end-of-string, so the lookaheads and
// the trailing anchor are equivalent. `re.DOTALL` -> `[\s\S]`.
// `\d` -> `\p{Nd}` for the same reason as `\w` above: Python's `\d` matches any
// Unicode decimal digit, so a comment dated `@ ١٤٤٧-01-01:` has a header there.
const COM_HEADER_DATED_RE = /^\s*([\s\S]*?)\s*@\s*(\p{Nd}{4}\S*):(?=\s|$)\s*([\s\S]*)$/u;
const COM_HEADER_PLAIN_RE = /^\s*(?:([\s\S]*?):(?=\s|$)\s*|:\s*)([\s\S]*)$/;

const SNIPPET_TAGS: Record<string, [string, string]> = {
  del: ["{--", "--}"],
  ins: ["{++", "++}"],
  fmt: ["{==", "==}"],
};

/** How many entries one ledger page renders. */
const LEDGER_PAGE_SIZE = 300;

/** How far back of the body the wrapper scan reads for snippets. */
const SNIPPET_LOOKBACK = 100000;

const collapse = (s: string) => s.replace(/\s+/g, " ").trim();
/** `str.isdigit()`: any Unicode decimal digit, not just ASCII. */
const isDigits = (s: string) => /^\p{Nd}+$/u.test(s);
const stripPrefix = (s: string, prefix: string) => (s.startsWith(prefix) ? s.slice(prefix.length) : s);

const ND_RE = /\p{Nd}/u;

/**
 * Rewrites Unicode decimal digits as ASCII ones, so `parseInt` reads what
 * Python's `int()` reads (`int("١٢") == 12`, `parseInt("١٢")` is NaN — and a NaN
 * sort key silently scrambles the ledger's order). Unicode guarantees decimal
 * digits come in contiguous runs of ten starting at a zero, so a digit's value
 * is its distance from the start of its run, mod 10.
 */
function ascii_digits(s: string): string {
  return s.replace(/\p{Nd}/gu, (d) => {
    const cp = d.codePointAt(0) as number;
    let zero = cp;
    while (zero > 0 && ND_RE.test(String.fromCodePoint(zero - 1))) zero--;
    return String((cp - zero) % 10);
  });
}

/**
 * Splits `[Com:N]`'s tail into author, body, and the offset of the delimiter
 * that ended the header (-1 when there is no header at all).
 * Mirrors `_parse_com_header` (`:1372-1388`).
 */
function _parse_com_header(slice_text: string): [string, string, number] {
  const m1 = COM_HEADER_DATED_RE.exec(slice_text);
  if (m1) {
    const body = m1[3];
    return [m1[1].trim(), body, slice_text.length - body.length];
  }

  const m2 = COM_HEADER_PLAIN_RE.exec(slice_text);
  if (m2) {
    const raw_author = m2[1];
    const body = m2[2];
    return [raw_author ? raw_author.trim() : "", body, slice_text.length - body.length];
  }

  return ["", slice_text.trim(), -1];
}

interface ChgItem {
  cid: string;
  raw_type: string | undefined;
  rest: string;
  change_type: string;
}

interface ComItem {
  cid: string;
  parsed_author: string;
  body_text: string;
}

function addPair(pair_map: Map<string, string[]>, from: string, to: string): void {
  const partners = pair_map.get(from);
  if (!partners) {
    pair_map.set(from, [to]);
  } else if (!partners.includes(to)) {
    partners.push(to);
  }
}

export function build_changes_response(
  text: string,
  file_path: string,
  opts: {
    comments_data?: Record<string, any> | null;
    author_filter?: string | null;
    page?: number | string | null;
    offset?: number;
    bundle?: ProjectionBundle;
    existing_change_ids?: Iterable<string> | null;
    no_chrome?: boolean;
  } = {},
): ToolResult {
  const {
    comments_data = null,
    author_filter = null,
    page = null,
    bundle,
    existing_change_ids = null,
    no_chrome = false,
  } = opts;

  let offset = opts.offset ?? 0;
  if (offset < 0) offset = 0;

  // `dict.get` sees real entries only, so every lookup here is own-property
  // only. A plain index reaches `Object.prototype`, and comment ids come from
  // the document: `[Com:toString]` typed in a Word comment would find a
  // function, report author "Unknown"/snippet "" and shadow the bubble Python
  // parses. (Python's `comments_data.get(int(cid))` branch needs no port — a JS
  // object's keys are strings already.)
  const comment_data_for = (key: string) =>
    comments_data && Object.hasOwn(comments_data, key) ? comments_data[key] : null;

  const body = bundle ? bundle.body : split_structural_appendix(text)[0];
  const pag_res = bundle ? bundle.pagination : paginate(body, "");
  const page_offsets = pag_res.body_page_offsets;
  const total_pages = pag_res.total_pages;

  // Materialised once: Python takes any iterable but tests membership
  // repeatedly, which a generator could not survive.
  const existing_ids = existing_change_ids == null ? null : new Set(existing_change_ids);
  const isLiveId = (cid: string) =>
    existing_ids !== null && (existing_ids.has(cid) || existing_ids.has(`Chg:${cid}`));

  // Map, never a plain object: numeric-looking string keys in an object are
  // reordered numerically, which would silently change the emitted order.
  const chg_entries = new Map<string, LedgerEntry>();
  const com_entries = new Map<string, LedgerEntry>();
  const pair_map = new Map<string, string[]>();

  for (const m of body.matchAll(BUBBLE_RE)) {
    const b_start = m.index;
    const p_num = offset_to_page(b_start, page_offsets);
    const bubble_raw = m[1].trim();

    const pre = body.slice(Math.max(0, b_start - SNIPPET_LOOKBACK), b_start);
    const wrappers = [...pre.matchAll(WRAPPER_RE)];
    const all_ins_snips = wrappers.filter((w) => w[1] === "{++").map((w) => w[2]);
    const all_del_snips = wrappers.filter((w) => w[1] === "{--").map((w) => w[2]);
    const all_fmt_snips = wrappers.filter((w) => w[1] === "{==").map((w) => w[2]);

    const tag_matches = [...bubble_raw.matchAll(TAG_RE)];
    if (tag_matches.length === 0) continue;

    let first_com_delim_pos = Number.POSITIVE_INFINITY;
    for (const tm of tag_matches) {
      if (tm[1] === "Com") {
        const tm_end = tm.index + tm[0].length;
        const [, , d_off] = _parse_com_header(bubble_raw.slice(tm_end));
        if (d_off !== -1) {
          first_com_delim_pos = tm_end + d_off;
          break;
        }
      }
    }

    const header_tokens: RegExpExecArray[] = [];
    for (const tm of tag_matches) {
      if (tm[1] === "Com") {
        header_tokens.push(tm as RegExpExecArray);
      } else if (tm[1] === "Chg") {
        if (tm.index <= first_com_delim_pos) {
          header_tokens.push(tm as RegExpExecArray);
        } else {
          // Inside a comment body a [Chg:N] tag is only a header when it
          // opens its own line; mid-line mentions are prose and stay put.
          const line_start = tm.index === 0 ? 0 : bubble_raw.lastIndexOf("\n", tm.index - 1) + 1;
          if (!bubble_raw.slice(line_start, tm.index).trim()) {
            header_tokens.push(tm as RegExpExecArray);
          }
        }
      }
    }

    if (header_tokens.length === 0) continue;

    const parsed_chg_items: ChgItem[] = [];
    const parsed_com_items: ComItem[] = [];

    for (let i = 0; i < header_tokens.length; i++) {
      const tm = header_tokens[i];
      const kind = tm[1];
      const cid = tm[2];
      const next_start = i + 1 < header_tokens.length ? header_tokens[i + 1].index : bubble_raw.length;
      const token_slice = bubble_raw.slice(tm.index + tm[0].length, next_start);

      if (kind === "Chg") {
        parsed_chg_items.push({ cid, raw_type: tm[3], rest: token_slice.trim(), change_type: "" });
      } else if (kind === "Com") {
        const [c_author, c_body] = _parse_com_header(token_slice);
        parsed_com_items.push({ cid, parsed_author: c_author, body_text: c_body.trim() });
      }
    }

    // One author line can cover several ids; the last non-empty rest fills down.
    let shared_chg_rest = "";
    for (let i = parsed_chg_items.length - 1; i >= 0; i--) {
      if (parsed_chg_items[i].rest) {
        shared_chg_rest = parsed_chg_items[i].rest;
        break;
      }
    }
    for (const item of parsed_chg_items) {
      if (!item.rest) item.rest = shared_chg_rest;

      if (item.raw_type === "delete") item.change_type = "del";
      else if (item.raw_type === "insert") item.change_type = "ins";
      else if (item.raw_type === "format") item.change_type = "fmt";
      else item.change_type = all_del_snips.length ? "del" : all_fmt_snips.length ? "fmt" : "ins";
    }

    const N_del = parsed_chg_items.filter((it) => it.change_type === "del").length;
    const N_ins = parsed_chg_items.filter((it) => it.change_type === "ins").length;
    const N_fmt = parsed_chg_items.filter((it) => it.change_type === "fmt").length;

    // The wrappers this bubble annotates are the LAST N of the preceding run.
    const bubble_del_snips = N_del > 0 && all_del_snips.length >= N_del ? all_del_snips.slice(-N_del) : all_del_snips;
    const bubble_ins_snips = N_ins > 0 && all_ins_snips.length >= N_ins ? all_ins_snips.slice(-N_ins) : all_ins_snips;
    const bubble_fmt_snips = N_fmt > 0 && all_fmt_snips.length >= N_fmt ? all_fmt_snips.slice(-N_fmt) : all_fmt_snips;

    let del_idx = 0;
    let ins_idx = 0;
    let fmt_idx = 0;

    // Python walks `parsed_chg_items + parsed_com_items`; the changes always come
    // first, so the two loops below are the same traversal.
    for (const item of parsed_chg_items) {
      const cid = item.cid;
      const change_type = item.change_type;
      const rest = item.rest;

      const pick = (snips: string[], idx: number) =>
        idx < snips.length ? snips[idx] : snips.length ? snips[snips.length - 1] : "";

      let raw_snip = "";
      if (change_type === "del") raw_snip = pick(bubble_del_snips, del_idx++);
      else if (change_type === "ins") raw_snip = pick(bubble_ins_snips, ins_idx++);
      else if (change_type === "fmt") raw_snip = pick(bubble_fmt_snips, fmt_idx++);

      if (!raw_snip) {
        // No wrapper of this type in the lookback: fall back to the nearest one
        // of any length that closes before the bubble.
        const [open_tag, close_tag] = SNIPPET_TAGS[change_type] ?? SNIPPET_TAGS.del;
        const tag_open = body.lastIndexOf(open_tag, b_start - open_tag.length);
        if (tag_open !== -1) {
          let tag_close = body.indexOf(close_tag, tag_open);
          if (tag_close !== -1 && tag_close + close_tag.length > b_start) tag_close = -1;
          raw_snip =
            tag_close !== -1
              ? body.slice(tag_open + open_tag.length, tag_close)
              : body.slice(tag_open + open_tag.length, b_start);
        }
      }

      let clean_snip = collapse(raw_snip);
      if (clean_snip.length > 48) clean_snip = `${clean_snip.slice(0, 45)}...`;

      const pair_match = PAIR_RE.exec(rest);
      if (pair_match) {
        const partner_cids = [...pair_match[1].matchAll(CHG_ID_RE)].map((pm) => pm[1]);
        const bubble_cids = parsed_chg_items.map((it) => it.cid);
        let src_cid = cid;
        if (partner_cids.includes(cid)) {
          const non_partner_cids = bubble_cids.filter((c) => !partner_cids.includes(c));
          src_cid = non_partner_cids.length ? non_partner_cids[0] : cid;
        }
        for (const pid of partner_cids) {
          if (pid !== src_cid) {
            addPair(pair_map, src_cid, pid);
            addPair(pair_map, pid, src_cid);
          }
        }
      }

      const author = rest.replace(AUTHOR_NOISE_RE, "").trim() || "Unknown";

      if (existing_ids !== null && !isLiveId(cid)) continue;

      if (!chg_entries.has(cid)) {
        chg_entries.set(cid, {
          kind: "chg",
          cid,
          change_type,
          author,
          page: p_num,
          snippet: clean_snip,
          pair_ids: [],
          reply_to_id: null,
          position: b_start,
        });
      }
    }

    for (const item of parsed_com_items) {
      const cid = item.cid;
      const cdata = comment_data_for(cid) || comment_data_for(`Com:${cid}`) || null;

      let author: string;
      let raw_comm: string;
      let parent_id: unknown;
      if (cdata !== null) {
        author = cdata.author || "Unknown";
        raw_comm = cdata.text ?? "";
        parent_id = cdata.parent_id;
      } else {
        const parsed_author = item.parsed_author;
        raw_comm = item.body_text;
        const reply_match = REPLY_RE.exec(parsed_author);
        parent_id = reply_match ? reply_match[1] : null;
        author = parsed_author;
      }

      author = author.replace(AUTHOR_NOISE_RE, "").trim() || "Unknown";

      let clean_comm = collapse(raw_comm);
      if (clean_comm.length > 120) clean_comm = `${clean_comm.slice(0, 117)}...`;

      const reply_to = parent_id
        ? String(parent_id).startsWith("Com:")
          ? String(parent_id)
          : `Com:${parent_id}`
        : null;

      if (!com_entries.has(cid)) {
        com_entries.set(cid, {
          kind: "com",
          cid,
          change_type: "",
          author,
          page: p_num,
          snippet: clean_comm,
          pair_ids: [],
          reply_to_id: reply_to,
          position: b_start,
        });
      }
    }
  }

  // Comments the projection carries no bubble for (resolved, orphaned, or in a
  // part the body excludes) still belong in the ledger — pinned to the end.
  if (comments_data) {
    for (const [cid, cdata] of Object.entries(comments_data)) {
      const str_cid = stripPrefix(String(cid), "Com:");
      if (com_entries.has(str_cid)) continue;

      const author = (cdata.author || "Unknown").replace(AUTHOR_NOISE_RE, "").trim() || "Unknown";
      let clean_comm = collapse(cdata.text ?? "");
      if (clean_comm.length > 120) clean_comm = `${clean_comm.slice(0, 117)}...`;
      const parent_id = cdata.parent_id;
      const reply_to = parent_id
        ? String(parent_id).startsWith("Com:")
          ? String(parent_id)
          : `Com:${parent_id}`
        : null;

      com_entries.set(str_cid, {
        kind: "com",
        cid: str_cid,
        change_type: "",
        author,
        page: 1,
        snippet: clean_comm,
        pair_ids: [],
        reply_to_id: reply_to,
        position: 999999,
      });
    }
  }

  // A live id with no bubble is still actionable, so it is listed rather than
  // hidden — the agent can accept/reject it.
  if (existing_ids !== null) {
    for (const raw_id of existing_ids) {
      const clean_cid = stripPrefix(String(raw_id), "Chg:");
      if (chg_entries.has(clean_cid)) continue;
      chg_entries.set(clean_cid, {
        kind: "chg",
        cid: clean_cid,
        change_type: "del",
        author: "Unknown",
        page: 1,
        snippet: "",
        pair_ids: [],
        reply_to_id: null,
        position: 999999,
      });
    }
  }

  for (const [cid, e] of chg_entries) {
    const partners = pair_map.get(cid);
    if (partners) e.pair_ids = partners;
    if (existing_ids !== null) e.pair_ids = e.pair_ids.filter((pid) => isLiveId(pid));
  }

  const num_id = (cid: string) => (isDigits(cid) ? parseInt(ascii_digits(cid), 10) : 0);
  const all_entries = [...chg_entries.values(), ...com_entries.values()].sort(
    (a, b) =>
      a.position - b.position ||
      (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0) ||
      num_id(a.cid) - num_id(b.cid),
  );

  let filtered = all_entries;
  if (author_filter) {
    const af = author_filter.trim().toLowerCase();
    filtered = filtered.filter((e) => e.author.toLowerCase().includes(af));
  }

  if (page != null && String(page).toLowerCase() !== "all") {
    // parse_page_arg already throws with Python's message.
    const [kind, p_val] = parse_page_arg(page);
    if (kind === "range") {
      const [start_p, end_p] = p_val as [number, number];
      if (start_p < 1 || end_p < 1 || start_p > total_pages) {
        throw new Error(`Page ${start_p} out of range (doc has ${total_pages} pages).`);
      }
      filtered = filtered.filter((e) => start_p <= e.page && e.page <= end_p);
    } else if (kind === "single") {
      const p_single = p_val as number;
      if (p_single < 1 || p_single > total_pages) {
        throw new Error(`Page ${p_single} out of range (doc has ${total_pages} pages).`);
      }
      filtered = filtered.filter((e) => e.page === p_single);
    }
  }

  const total_changes = filtered.filter((e) => e.kind === "chg").length;
  const total_comments = filtered.filter((e) => e.kind === "com").length;

  const dist = new Map<number, number>();
  for (const e of filtered) dist.set(e.page, (dist.get(e.page) ?? 0) + 1);
  const dist_str = dist.size
    ? [...dist.keys()]
        .sort((a, b) => a - b)
        .map((p) => `p${p}: ${dist.get(p)}`)
        .join(", ")
    : "none";

  const authors = [...new Set(filtered.filter((e) => e.author).map((e) => e.author))].sort();
  const authors_str = authors.length ? authors.join(", ") : "None";

  const header =
    `> **Changes ledger** — ${total_changes} change(s), ${total_comments} comment(s) across ${total_pages} page(s).\n` +
    `> Distribution — ${dist_str}\n` +
    `> Authors — ${authors_str}\n\n`;

  const total_entries = filtered.length;
  const slice_entries = filtered.slice(offset, offset + LEDGER_PAGE_SIZE);

  const lines: string[] = [];
  for (const e of slice_entries) {
    if (e.kind === "chg") {
      let pair_suffix = "";
      if (e.pair_ids.length) {
        const sorted_pids = [...e.pair_ids].sort((a, b) => num_id(a) - num_id(b) || (a < b ? -1 : a > b ? 1 : 0));
        pair_suffix = `  (pairs ${sorted_pids.map((pid) => `Chg:${pid}`).join(", ")})`;
      }
      lines.push(`Chg:${e.cid}  ${e.change_type}  ${e.author}  p${e.page}  "${e.snippet}"${pair_suffix}`);
    } else {
      const reply_suffix = e.reply_to_id ? `  (reply to ${e.reply_to_id})` : "";
      lines.push(`Com:${e.cid}  ${e.author}  p${e.page}  "${e.snippet}"${reply_suffix}`);
    }
  }

  let continuation = "";
  if (offset + LEDGER_PAGE_SIZE < total_entries) {
    const next_offset = offset + LEDGER_PAGE_SIZE;
    const mcp_args: string[] = [];
    // The RAW path, as Python emits it: the continuation is a call the agent
    // replays verbatim, so it must echo the path it was given.
    if (file_path) mcp_args.push(`file_path="${file_path}"`);
    mcp_args.push('mode="changes"');
    if (author_filter) mcp_args.push(`changes_author="${author_filter}"`);
    if (page != null) {
      mcp_args.push(typeof page === "string" ? `page="${page}"` : `page=${page}`);
    }
    mcp_args.push(`changes_offset=${next_offset}`);
    continuation =
      `\n\n> **Showing entries ${offset + 1}-${offset + slice_entries.length} of ${total_entries}.** ` +
      `Continue with \`read_docx(${mcp_args.join(", ")})\`.`;
  }

  let ui_markdown: string;
  let llm_content: string;
  if (no_chrome) {
    // The ledger lines ARE the payload here, so chrome-stripping normally
    // leaves them alone. With nothing to list (clean document, a filter
    // matching no entry, or an offset past the last one) the counts are the
    // only answer there is: emit them as a bare line, never an empty
    // response (QA 2026-08-12: `--mode changes --no-chrome` returned "").
    ui_markdown = lines.join("\n") || `${total_changes} change(s), ${total_comments} comment(s)`;
    llm_content = ui_markdown;
  } else {
    ui_markdown = header + lines.join("\n") + continuation;
    llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
  }

  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: basename(file_path),
    },
  };
}
