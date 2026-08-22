import { resolve, basename } from "node:path";
import {
  DocumentObject,
  paginate,
  extract_outline,
  OutlineNode,
  RegexTimeoutError,
  userFindAllMatches,
  PAGE_RANGE_MAX_PAGES,
  response_budget_limit,
  whole_doc_guard_message,
  heading_path_at,
} from "@adeu/core";
import { split_projection } from "./shared.js";

export interface ToolResult {
  content: { type: "text"; text: string }[];
  structuredContent?: any;
  isError?: boolean;
  [key: string]: unknown;
}

/**
 * Precomputed projection products (doc-cache): the split body/appendix and
 * the body pagination that every builder would otherwise recompute per call.
 * All builders accept it OPTIONALLY — omitted, they compute exactly what
 * they always did, so responses stay byte-identical cached vs uncached.
 */
export interface ProjectionBundle {
  body: string;
  appendix: string;
  pagination: ReturnType<typeof paginate>;
}

// The changes ledger lives in its own module (440 lines of bubble parsing) but
// belongs to this module's public surface: every caller — index.ts and the
// conformance suite — reaches the builders through here.
export {
  build_changes_response,
  build_fields_response,
  fields_discovery_hint,
  banner_for_path,
} from "./ledger.js";

// Projection style markers: `**bold**` always; `_italic_` only where the
// underscore is not intra-word (identifiers like snake_case are literal text —
// the projection's italics markers always hug non-whitespace at a word edge).
const STYLE_MARKER_RE = /\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])/g;

// Characters the marker stripping must never consume: {#...} anchor tokens
// ({#_RefN}, {#cell:...}) and literal underscore runs (form fill-ins like
// [_________]). The word-edge `_` rules above would otherwise eat the
// anchor's leading underscore, handing agents a nonexistent anchor
// (QA 2026-07-23 F4).
const PROTECTED_TOKEN_RE = /\{#[^}]+\}|_{3,}/g;

// `{#anchor}` tokens — bookmark anchors and CC-1's `{#cc:N}` content-control
// anchors. A snippet window that lands inside one must not emit the fragment
// (CC-1 A1.6). Source form: the matcher is rebuilt per use because it is
// stateful under /g.
const ANCHOR_TOKEN_SRC = "\\{#[^}\\n]*\\}";

/**
 * Renders `prefix **match** suffix` with the document's own bold/italic
 * projection markers stripped first, so the highlight cannot collide with
 * markers already present — a regex match crossing styled runs used to
 * render as `**The **Supplier** _shall provide**_` (QA 2026-07-19 v8 F-10).
 * Markers are detected over the WHOLE region (a match boundary can cut a
 * marker away from its word-edge context), then each part is rebuilt from
 * the surviving characters. Mirrors Python's _emphasized_snippet.
 */
export function emphasizedSnippet(
  prefix: string,
  match: string,
  suffix: string,
): string {
  return emphasizedSnippetSpans(prefix + match + suffix, [
    [prefix.length, prefix.length + match.length],
  ]);
}

/**
 * Renders `region` with every matched span wrapped in `**…**`, stripping the
 * document's own style markers first (same rules as emphasizedSnippet).
 * Accepts MULTIPLE spans so one paragraph with several hits renders as one
 * entry with every hit highlighted (QA round 3, finding 3.10).
 */
export function emphasizedSnippetSpans(
  region: string,
  spans: Array<[number, number]>,
): string {
  const keep = new Array<boolean>(region.length).fill(true);
  const protected_idx = new Array<boolean>(region.length).fill(false);
  for (const m of region.matchAll(PROTECTED_TOKEN_RE)) {
    for (let i = m.index!; i < m.index! + m[0].length; i++)
      protected_idx[i] = true;
  }
  for (const m of region.matchAll(STYLE_MARKER_RE)) {
    let overlaps_protected = false;
    for (let i = m.index!; i < m.index! + m[0].length; i++) {
      if (protected_idx[i]) {
        overlaps_protected = true;
        break;
      }
    }
    if (overlaps_protected) continue;
    for (let i = m.index!; i < m.index! + m[0].length; i++) keep[i] = false;
  }
  const stripped = (a: number, b: number): string => {
    let out = "";
    for (let i = a; i < b; i++) if (keep[i]) out += region[i];
    return out;
  };
  const sorted = [...spans].sort((x, y) => x[0] - y[0]);
  const parts: string[] = [];
  let cursor = 0;
  for (const [s, e] of sorted) {
    parts.push(stripped(cursor, s));
    parts.push(`**${stripped(s, e)}**`);
    cursor = e;
  }
  parts.push(stripped(cursor, region.length));
  return parts.join("");
}

// Search-response size budget. A search response is sized at ~60 tokens per
// requested match; tokens are estimated at 4 characters each (the same crude
// ratio the test suite measures with). SNIPPET_RADIUS_LADDER is the descending
// set of per-hit context radii the renderer tries, widest first: 120 chars is
// the documented clamp, the rest are fallbacks for documents whose paragraphs
// are long enough that 20 full-width snippets would not fit the budget. The
// ladder does NOT bottom out at 0: a `...**Supplier**...` snippet costs chrome
// to tell the agent only what it already typed, so 16 chars each side is the
// floor and the budget pass drops trailing entries instead.
export const SEARCH_TOKENS_PER_MATCH = 60;
export const CHARS_PER_TOKEN = 4;
export const SNIPPET_RADIUS_LADDER = [120, 60, 30, 16];

// `max_matches * 60` is an allowance for match CONTENT, but a response also
// carries chrome that no radius can shrink (the file-path line, the results
// header, the distribution summary, the continuation and trim notes — ~110
// fixed, plus ~22 per entry for the rule, heading, breadcrumb and occurrence
// line, and ~13 for a floor-radius snippet). Sizing from chrome + a minimum
// snippet per entry keeps every requested entry payable on a 1- or 2-match
// page, where a purely content-sized budget was unreachable at ANY radius.
export const SEARCH_FIXED_CHROME_TOKENS = 120;
export const SEARCH_ENTRY_CHROME_TOKENS = 22;
export const SEARCH_MIN_SNIPPET_TOKENS = 13;

/**
 * Approximate-token ceiling for a search response that was ASKED for
 * `max_matches` hits and actually rendered `rendered_count` of them (defaults
 * to all of them). The chrome term is sized on hits RENDERED, not requested:
 * once the budget pass starts dropping hits it must stop granting per-hit
 * allowance for hits no longer in the response. Mirrors Python's
 * search_budget_tokens (_response_builders.py:181-202).
 */
export function search_budget_tokens(
  max_matches: number,
  rendered_count?: number,
): number {
  if (max_matches < 1) return SEARCH_FIXED_CHROME_TOKENS;
  const rendered =
    rendered_count === undefined
      ? max_matches
      : Math.min(max_matches, Math.max(rendered_count, 0));
  return Math.max(
    max_matches * SEARCH_TOKENS_PER_MATCH,
    SEARCH_FIXED_CHROME_TOKENS +
      rendered * (SEARCH_ENTRY_CHROME_TOKENS + SEARCH_MIN_SNIPPET_TOKENS),
  );
}

const SNIPPET_MARKUP_PAIRS: Array<[string, string]> = [
  ["{>>", "<<}"],
  ["{--", "--}"],
  ["{++", "++}"],
  ["{==", "==}"],
];
const SNIPPET_CLOSER_OF: Record<string, string> =
  Object.fromEntries(SNIPPET_MARKUP_PAIRS);
const SNIPPET_OPENER_OF: Record<string, string> = Object.fromEntries(
  SNIPPET_MARKUP_PAIRS.map(([opener, closer]) => [closer, opener]),
);
const SNIPPET_MARKUP_TOKEN_SRC = [
  ...Object.keys(SNIPPET_CLOSER_OF),
  ...Object.keys(SNIPPET_OPENER_OF),
]
  .map((t) => escapeRegExp(t))
  .join("|");

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Extends a snippet window until every CriticMarkup span it overlaps is whole,
 * returning the widened `[start, end]`. {>>…<<} meta bubbles are MULTI-line
 * and a clamped window cuts spans on BOTH edges, so agents could not harvest
 * ids/pairings from search results (QA round 3, finding 3.12).
 *
 * Delimiters are walked IN ORDER with a depth counter per pair, never counted:
 * a window holding one stray `--}` on the left and one stray `{--` on the
 * right balances arithmetically while reading `l1--}…{--del` (QA round 4,
 * finding 1). A closer seen at depth 0 belongs to a span that opened before
 * `start`, so `start` moves back to that opener; a pair still open when the
 * scan reaches `end` pushes `end` forward to its closer. Each step strictly
 * widens the window, so the loop terminates.
 *
 * Mirrors Python's _balance_snippet_window. NOTE the backward search:
 * Python's `body.rfind(opener, 0, start)` requires the opener to fit ENTIRELY
 * before `start`, while JS `lastIndexOf`'s second argument is the START of the
 * match — hence the `start - opener.length` bound, and the negative guard
 * (`lastIndexOf(x, -1)` searches at index 0 instead of failing).
 */
export function balanceSnippetWindow(
  body: string,
  start: number,
  end: number,
): [number, number] {
  for (;;) {
    const depth: Record<string, number> = {};
    for (const opener of Object.keys(SNIPPET_CLOSER_OF)) depth[opener] = 0;
    let widened = false;

    // `{#anchor}` tokens are kept whole on the same terms (CC-1 A1.6). They
    // are not a paired construct, so they need no depth counter — a window
    // edge landing strictly inside one just moves out to the token's own edge.
    // A split anchor is worse than a missing one: `{#cc:` is a
    // plausible-looking target an agent will copy, and the radius ladder makes
    // it reachable whenever a result set exceeds the response budget.
    const anchor_re = new RegExp(ANCHOR_TOKEN_SRC, "g");
    let a: RegExpExecArray | null;
    while ((a = anchor_re.exec(body)) !== null) {
      const a_start = a.index;
      const a_end = a.index + a[0].length;
      if (a_start < start && start < a_end) {
        start = a_start;
        widened = true;
      }
      if (a_start < end && end < a_end) {
        end = a_end;
        widened = true;
      }
      if (a_start >= end) break;
    }
    if (widened) continue;

    const token_re = new RegExp(SNIPPET_MARKUP_TOKEN_SRC, "g");
    token_re.lastIndex = start;
    let tok: RegExpExecArray | null;
    while ((tok = token_re.exec(body)) !== null) {
      // Python's finditer(body, start, end) cannot match past `end`; tokens
      // are all 3 chars, so anything straddling the edge ends the scan.
      if (tok.index + tok[0].length > end) break;
      const token = tok[0];
      if (token in SNIPPET_CLOSER_OF) {
        depth[token] += 1;
        continue;
      }
      const opener = SNIPPET_OPENER_OF[token];
      if (depth[opener]) {
        depth[opener] -= 1;
        continue;
      }
      const search_from = start - opener.length;
      const prev_opener =
        search_from < 0 ? -1 : body.lastIndexOf(opener, search_from);
      if (prev_opener !== -1) {
        start = prev_opener;
        widened = true;
        break;
      }
    }

    if (widened) continue;

    for (const opener of Object.keys(depth)) {
      if (!depth[opener]) continue;
      const closer = SNIPPET_CLOSER_OF[opener];
      const next_closer = body.indexOf(closer, end);
      if (next_closer !== -1) {
        end = next_closer + closer.length;
        widened = true;
        break;
      }
    }

    if (!widened) return [start, end];
  }
}

const TRAILING_BUBBLE_RE = /\{>>\s*(\[[^\]\n]{0,80}\])([\s\S]*?)<<\}/y;

/**
 * The meta bubble the projection writes immediately after a deletion's or
 * insertion's closer, reduced to its `[Chg:N …]` header — the id an agent
 * needs to accept or reject the change it is looking at. The bubble's prose is
 * elided with `...` rather than reproduced, because this is re-attached to a
 * snippet that was clamped for size in the first place. Empty when no bubble
 * follows. Mirrors Python's _trailing_bubble_header.
 */
function trailingBubbleHeader(body: string, at: number): string {
  TRAILING_BUBBLE_RE.lastIndex = at;
  const m = TRAILING_BUBBLE_RE.exec(body);
  if (!m) return "";
  return "{>>" + m[1] + (m[2].trim() ? " ..." : "") + "<<}";
}

/**
 * The `(prefix, suffix)` CriticMarkup tags a snippet window needs because it
 * sits STRICTLY INSIDE spans that open before it and close after it.
 *
 * balanceSnippetWindow only sees delimiters WITHIN the window, so a window cut
 * out of the middle of a long deletion contains no delimiters at all, is
 * declared balanced, and ships deleted text as live prose. The window is not
 * widened to the span's own edges (a 4000-char deletion would defeat clamping
 * entirely) — only the tags are re-attached. A `{>>` bubble carries its
 * `[Chg:N …]` header into the prefix; a deletion or insertion carries the
 * id-bearing bubble that follows its closer into the suffix. Mirrors Python's
 * _enclosing_snippet_markup.
 */
function enclosingSnippetMarkup(
  body: string,
  start: number,
  end: number,
): [string, string] {
  const open_spans: Array<[number, string, string]> = [];
  for (const [opener, closer] of SNIPPET_MARKUP_PAIRS) {
    const pair_re = new RegExp(
      `${escapeRegExp(opener)}|${escapeRegExp(closer)}`,
      "g",
    );
    const stack: number[] = [];
    let tok: RegExpExecArray | null;
    while ((tok = pair_re.exec(body)) !== null) {
      if (tok.index + tok[0].length > start) break;
      if (tok[0] === opener) stack.push(tok.index);
      else if (stack.length) stack.pop();
    }
    if (!stack.length) continue;
    const closer_at = body.indexOf(closer, end);
    if (closer_at === -1) continue;
    const open_at = stack[stack.length - 1];
    let prefix = opener;
    let suffix = closer;
    if (opener === "{>>") {
      const header = /^\[[^\]\n]{0,80}\]/.exec(
        body.slice(open_at + opener.length, start),
      );
      if (header) prefix += header[0];
    } else {
      suffix += trailingBubbleHeader(body, closer_at + closer.length);
    }
    open_spans.push([open_at, prefix, suffix]);
  }

  open_spans.sort((a, b) => a[0] - b[0]);
  return [
    open_spans.map(([, prefix]) => prefix).join(""),
    open_spans
      .slice()
      .reverse()
      .map(([, , suffix]) => suffix)
      .join(""),
  ];
}

/** Merges overlapping/touching [start, end) spans, sorted by start. */
function mergeSpans(spans: Array<[number, number]>): Array<[number, number]> {
  const merged: Array<[number, number]> = [];
  for (const [span_start, span_end] of [...spans].sort(
    (a, b) => a[0] - b[0] || a[1] - b[1],
  )) {
    const last = merged[merged.length - 1];
    if (last && span_start <= last[1]) last[1] = Math.max(last[1], span_end);
    else merged.push([span_start, span_end]);
  }
  return merged;
}

/**
 * Widens `[start, end)` outward to the nearest code-point boundary, so a
 * slice never cuts a surrogate pair in half.
 *
 * Python computes snippet windows in CODE POINTS (`hit.start - radius`
 * indexes characters), so an astral character straddling a window edge is
 * kept whole; JS offsets are UTF-16 code units, and slicing at the raw index
 * ships a lone surrogate that becomes U+FFFD once encoded for the wire. This
 * has no Python counterpart — it is what the port needs to reach parity.
 */
function snapCodePointBoundary(
  text: string,
  start: number,
  end: number,
): [number, number] {
  if (start > 0 && start < text.length) {
    const code = text.charCodeAt(start);
    if (code >= 0xdc00 && code <= 0xdfff) start -= 1;
  }
  if (end > 0 && end < text.length) {
    const code = text.charCodeAt(end - 1);
    if (code >= 0xd800 && code <= 0xdbff) end += 1;
  }
  return [start, end];
}

/**
 * The LLM-only header block: File Path, then the fields banner.
 *
 * spec-projection §7 puts the banner immediately after the File Path line, so
 * the two render as one blockquote. Both are chrome and both vanish under
 * `no_chrome`, which exists so the projection can round-trip.
 */
function with_path_header(
  file_path: string,
  fields_banner: string | null | undefined,
  ui_markdown: string,
): string {
  let header = `> **File Path:** \`${resolve(file_path)}\``;
  if (fields_banner) header += `\n${fields_banner}`;
  return `${header}\n\n${ui_markdown}`;
}

function parseBundleAndOptions(
  arg3?:
    | ProjectionBundle
    | { no_chrome?: boolean; fields_banner?: string | null }
    | boolean,
  arg4?: boolean | { no_chrome?: boolean; fields_banner?: string | null },
): {
  bundle?: ProjectionBundle;
  no_chrome: boolean;
  fields_banner?: string | null;
} {
  let bundle: ProjectionBundle | undefined;
  let no_chrome = false;
  let fields_banner: string | null | undefined;

  if (typeof arg3 === "boolean") {
    no_chrome = arg3;
  } else if (arg3 && "body" in arg3) {
    bundle = arg3 as ProjectionBundle;
  } else if (arg3 && typeof arg3 === "object") {
    if ("no_chrome" in arg3 && arg3.no_chrome !== undefined) {
      no_chrome = Boolean(arg3.no_chrome);
    }
    if ("fields_banner" in arg3) fields_banner = arg3.fields_banner;
  }

  if (typeof arg4 === "boolean") {
    no_chrome = arg4;
  } else if (arg4 && typeof arg4 === "object") {
    if ("no_chrome" in arg4 && arg4.no_chrome !== undefined) {
      no_chrome = Boolean(arg4.no_chrome);
    }
    if ("fields_banner" in arg4) fields_banner = arg4.fields_banner;
  }

  return { bundle, no_chrome, fields_banner };
}

function _build_appendix_pointer(has_appendix: boolean): string {
  if (!has_appendix) return "";
  return `\n\n---\n\n> **Appendix available.** This document has structural metadata (defined terms, cross-references, bookmarks, diagnostics) that may be relevant when editing. Call \`read_docx\` with \`mode='appendix'\` to load it before submitting edits.`;
}

function _build_page_banner(page: number, total: number): string {
  if (total <= 1) return "";
  // "synthetic" is load-bearing: Adeu pages are length-based content chunks
  // sized for LLM consumption, and readers must never mistake them for
  // printed Word pages or explicit page breaks (QA 2026-07-19 ADEU-QA-005).
  return `> **Page ${page} of ${total}** (synthetic page — a length-based chunk, not a printed Word page) — call \`read_docx\` with \`mode='outline'\` for a heading map of the full document.\n\n---\n\n`;
}

function _build_page_footer(
  page: number,
  total: number,
  has_next: boolean,
): string {
  if (total <= 1 || !has_next) return "";
  return `\n\n---\n\n> **Continues on page ${page + 1} of ${total}.**`;
}

export function render_outline_tree(
  nodes: OutlineNode[],
  max_level: number = 2,
  verbose: boolean = false,
  no_chrome: boolean = false,
): string {
  if (!nodes || nodes.length === 0) {
    return "# (No headings detected)\n\nThis document has no detectable headings.";
  }

  const visible = nodes.filter((n) => n.level <= max_level);

  if (visible.length === 0) {
    if (no_chrome) {
      return `# (No headings at level <= ${max_level})\n\nDocument has ${nodes.length} headings, all at deeper levels.`;
    }
    return `# (No headings at level <= ${max_level})\n\nDocument has ${nodes.length} headings, all at deeper levels. Call read_docx with mode='outline' and outline_max_level=N (up to 6) to see them.`;
  }

  const lines: string[] = [];
  for (const node of visible) {
    const prefix = "#".repeat(node.level);
    if (verbose) {
      const meta_parts = [`p${node.page}`, node.style];
      if (node.has_table) meta_parts.push("has table");
      if (node.footnote_ids && node.footnote_ids.length > 0)
        meta_parts.push("fn:" + node.footnote_ids.join(","));
      lines.push(`${prefix} ${node.text} (${meta_parts.join(", ")})`);
    } else {
      // A heading that spans pages advertises the whole range it owns, so a
      // reader knows how many page reads the section costs (_response_builders.py:398-401).
      const page_str =
        node.end_page && node.end_page > node.page
          ? `p${node.page}-p${node.end_page}`
          : `p${node.page}`;
      lines.push(`${prefix} ${node.text} (${page_str})`);
    }
  }
  return lines.join("\n");
}

export function build_full_document_response(
  text: string,
  file_path: string,
  bundleOrOpts?:
    | ProjectionBundle
    | { no_chrome?: boolean; fields_banner?: string | null }
    | boolean,
  no_chrome_param?:
    | boolean
    | { no_chrome?: boolean; fields_banner?: string | null },
): ToolResult {
  // The ENTIRE document body with no page banner, continuation footer, or
  // appendix pointer — the round-trip artifact for text-based apply/diff
  // (QA 2026-07-17 F1; mirrors Python's build_full_document_response).
  const { bundle, no_chrome, fields_banner } = parseBundleAndOptions(
    bundleOrOpts,
    no_chrome_param,
  );
  const body = bundle ? bundle.body : split_projection(text)[0];
  const ui_markdown = body;
  const llm_content = no_chrome
    ? ui_markdown
    : with_path_header(file_path, fields_banner, ui_markdown);
  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: basename(file_path),
    },
  };
}

/**
 * The whole-document response budget refusal for an oversized unbounded read
 * (port of Python's build_budget_guard_message, _response_builders.py:654-700).
 *
 * The page count comes from the SAME pagination every reader path uses, so the
 * page numbers the refusal advertises are the page numbers `page=N` accepts.
 * The total is measured on `projected_text` (`:695` measures it too), so the
 * caller decides what is counted by choosing what it passes. Callers pass the
 * string the refused read WOULD have returned — `bundle.body`, the structural
 * appendix excluded — which is what Python's mode='full' text already is
 * (doc_cache.py projects it with include_appendix=False).
 *
 * `nodes` are the cached outline nodes OF THE VIEW being refused (the clean
 * view has its own — DocCache.ensureCleanOutline). Documents with no L1
 * heading get no outline section at all, rather than a "(No headings
 * detected)" placeholder; `null` ships the refusal without a heading map.
 */
export function build_budget_guard_message(
  projected_text: string,
  file_path: string,
  nodes: OutlineNode[] | null,
  bundle?: ProjectionBundle,
): string {
  const [body] = bundle
    ? [bundle.body]
    : split_projection(projected_text);
  const pagination = bundle ? bundle.pagination : paginate(body, "");
  const list = nodes ?? [];
  const has_l1 = list.some((n) => n.level === 1);
  const outline = has_l1 ? render_outline_tree(list, 1, false) : "";
  return whole_doc_guard_message(
    projected_text.length,
    response_budget_limit(),
    file_path,
    outline,
    pagination.total_pages,
  );
}

export function build_paginated_response(
  text: string,
  page: number,
  file_path: string,
  bundleOrOpts?:
    | ProjectionBundle
    | { no_chrome?: boolean; fields_banner?: string | null }
    | boolean,
  no_chrome_param?:
    | boolean
    | { no_chrome?: boolean; fields_banner?: string | null },
): ToolResult {
  const { bundle, no_chrome, fields_banner } = parseBundleAndOptions(
    bundleOrOpts,
    no_chrome_param,
  );
  const [body, appendix] = bundle
    ? [bundle.body, bundle.appendix]
    : split_projection(text);
  const has_appendix = Boolean(appendix.trim());

  const result = bundle ? bundle.pagination : paginate(body, "");

  if (page < 1 || page > result.total_pages) {
    throw new Error(
      `Page ${page} out of range (doc has ${result.total_pages} pages).`,
    );
  }

  const selected = result.pages[page - 1];

  let ui_markdown: string;
  let llm_content: string;

  if (no_chrome) {
    const page_marker =
      selected.total_pages > 1
        ? `[p${selected.page}/${selected.total_pages}]\n\n`
        : "";
    ui_markdown = `${page_marker}${selected.page_content}`;
    llm_content = ui_markdown;
  } else {
    const banner = _build_page_banner(selected.page, selected.total_pages);
    const footer = _build_page_footer(
      selected.page,
      selected.total_pages,
      selected.has_next,
    );
    const appendix_pointer = _build_appendix_pointer(has_appendix);

    ui_markdown = banner + selected.page_content + footer + appendix_pointer;
    llm_content = with_path_header(file_path, fields_banner, ui_markdown);
  }

  return {
    content: [{ type: "text", text: llm_content }],
    // Include structuredContent for the UI to render the markdown
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: basename(file_path),
    },
  };
}

export function build_page_range_response(
  text: string,
  start: number,
  end: number,
  file_path: string,
  bundleOrOpts?: ProjectionBundle | { no_chrome?: boolean } | boolean,
  no_chrome_param?: boolean | { no_chrome?: boolean },
): ToolResult {
  const { bundle, no_chrome } = parseBundleAndOptions(
    bundleOrOpts,
    no_chrome_param,
  );
  if (start < 1)
    throw new Error(`Invalid page number ${start}: page numbers must be positive integers.`);
  if (start > end)
    throw new Error(`end page (${end}) cannot be less than start page (${start})`);
  const [body, appendix] = bundle ? [bundle.body, bundle.appendix] : split_projection(text);
  const has_appendix = Boolean(appendix.trim());
  const result = bundle ? bundle.pagination : paginate(body, "");
  const total_pages = result.total_pages;
  if (start > total_pages)
    throw new Error(`Page ${start} out of range (doc has ${total_pages} pages).`);
  const last = Math.min(end, start + PAGE_RANGE_MAX_PAGES - 1, total_pages);
  const page_blocks: string[] = [];
  for (let p = start; p <= last; p++) {
    const selected = result.pages[p - 1];
    const banner = no_chrome
      ? (selected.total_pages > 1 ? `[p${selected.page}/${selected.total_pages}]\n\n` : "")
      : _build_page_banner(selected.page, selected.total_pages);
    page_blocks.push(`${banner}${selected.page_content}`);
  }
  const ui_parts: string[] = [page_blocks.join("\n\n")];
  if (!no_chrome) {
    if (last < end && last < total_pages) {
      ui_parts.push(
        `> **Range capped at ${PAGE_RANGE_MAX_PAGES} pages.** Continue with \`page="${last + 1}-${end}"\`.`,
      );
    } else if (end > total_pages) {
      ui_parts.push(
        `> **[range stopped at page ${total_pages}: the document has ${total_pages} page(s)]**`,
      );
    }
    const pointer = _build_appendix_pointer(has_appendix);
    if (pointer) ui_parts.push(pointer.trim());
  }
  const ui_markdown = ui_parts.join("\n\n");
  const llm_content = no_chrome ? ui_markdown : `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: { markdown: ui_markdown, file_path: resolve(file_path), title: basename(file_path) },
  };
}

export function build_outline_response(
  doc: DocumentObject,
  projected_text: string,
  file_path: string,
  outline_max_level: number = 2,
  outline_verbose: boolean = false,
  paragraph_offsets: Map<any, [number, number]> | null = null,
  no_chromeOrOpts: boolean | { no_chrome?: boolean } = false,
): ToolResult {
  const [body] = split_projection(projected_text);
  const pagination_result = paginate(body, "");

  const nodes = extract_outline(
    doc,
    body,
    pagination_result.body_pages,
    pagination_result.body_page_offsets,
    paragraph_offsets,
  );

  return render_outline_response(
    nodes,
    pagination_result.total_pages,
    file_path,
    outline_max_level,
    outline_verbose,
    no_chromeOrOpts,
  );
}

/**
 * Assembly half of build_outline_response, byte-identical to it: lets the
 * doc-cache serve outline mode from precomputed nodes + pagination without a
 * document load. Kept as ONE shared function so the header/format can never
 * drift between the cached and uncached paths.
 */
export function render_outline_response(
  nodes: OutlineNode[],
  total_pages: number,
  file_path: string,
  outline_max_level: number = 2,
  outline_verbose: boolean = false,
  no_chromeOrOpts: boolean | { no_chrome?: boolean } = false,
): ToolResult {
  const no_chrome =
    typeof no_chromeOrOpts === "boolean"
      ? no_chromeOrOpts
      : Boolean(no_chromeOrOpts?.no_chrome);

  // Levels outside 1-6 are meaningless (0/negative would render a
  // nonsensical "L1-L0" range label, QA L2). Clamp to the nearest sensible
  // depth, mirroring the Python builder.
  outline_max_level = Math.max(1, Math.min(outline_max_level, 6));

  const rendered = render_outline_tree(
    nodes,
    outline_max_level,
    outline_verbose,
    no_chrome,
  );

  const visible_count = nodes.filter(
    (n) => n.level <= outline_max_level,
  ).length;
  const deeper_count = nodes.length - visible_count;
  const deeper_hint =
    deeper_count > 0
      ? ` (${deeper_count} more at deeper levels, raise outline_max_level to see)`
      : "";

  let ui_markdown: string;
  let llm_content: string;

  if (no_chrome) {
    ui_markdown = rendered;
    llm_content = ui_markdown;
  } else {
    const header = `> **Outline view** — showing ${visible_count} of ${nodes.length} headings (L1-L${outline_max_level}${deeper_hint}) across ${total_pages} page(s). Call \`read_docx\` with \`mode='full'\` and \`page=N\` to read a section.\n\n---\n\n`;
    ui_markdown = header + rendered;
    llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
  }

  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: `Outline: ${basename(file_path)}`,
    },
  };
}

export function build_appendix_response(
  text: string,
  page: number,
  file_path: string,
  bundleOrOpts?: ProjectionBundle | { no_chrome?: boolean } | boolean,
  no_chrome_param?: boolean | { no_chrome?: boolean },
): ToolResult {
  const { bundle, no_chrome } = parseBundleAndOptions(
    bundleOrOpts,
    no_chrome_param,
  );
  const appendix = bundle
    ? bundle.appendix
    : split_projection(text)[1];

  if (!appendix.trim()) {
    const ui_markdown =
      "# Appendix\n\nThis document has no structural appendix (no defined terms, named anchors, or diagnostics detected).";
    const llm_content = no_chrome
      ? ui_markdown
      : `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
    return {
      content: [{ type: "text", text: llm_content }],
      structuredContent: {
        markdown: ui_markdown,
        file_path: resolve(file_path),
        title: `Appendix: ${basename(file_path)}`,
      },
    };
  }

  const result = paginate(appendix, "");

  if (page < 1 || page > result.total_pages) {
    throw new Error(
      `Appendix page ${page} out of range (appendix has ${result.total_pages} pages).`,
    );
  }

  const selected = result.pages[page - 1];

  let ui_markdown: string;
  let llm_content: string;

  if (no_chrome) {
    const page_marker =
      selected.total_pages > 1
        ? `[p${selected.page}/${selected.total_pages}]\n\n`
        : "";
    ui_markdown = `${page_marker}${selected.page_content}`;
    llm_content = ui_markdown;
  } else {
    let banner = "";
    let footer = "";

    if (selected.total_pages > 1) {
      banner = `> **Appendix page ${selected.page} of ${selected.total_pages}** — structural metadata for this document.\n\n---\n\n`;
      footer = selected.has_next
        ? `\n\n---\n\n> **Continues on appendix page ${selected.page + 1} of ${selected.total_pages}.**`
        : "";
    } else {
      banner =
        "> **Appendix** — structural metadata for this document.\n\n---\n\n";
    }

    ui_markdown = banner + selected.page_content + footer;
    llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
  }

  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: `Appendix: ${basename(file_path)}`,
    },
  };
}

/** One hit, carrying its document page and its GLOBAL 1-based match index. */
interface SearchHit {
  text: string;
  start: number;
  end: number;
  page: number;
  index: number;
}

/**
 * Filters projected Markdown to exact substring or regex matches.
 *
 * `page` semantics:
 *   - undefined/null or "all" (case-insensitive): ALL matches across the whole
 *     document. When matches span >1 document page, include a one-line
 *     distribution summary.
 *   - positive int N: only matches whose offset falls within document page N.
 *     If N has zero hits but the query exists elsewhere, emit a helpful
 *     empty-result pointer (not an error). If N exceeds the document's total
 *     pages, throw.
 *   - anything else (0, negative, a range string, a non-"all" string): throw.
 *
 * Occurrence counts (the "appears X times" line under each match) are always
 * computed from the FULL match set, never filtered.
 *
 * `max_matches < 1` renders NO match entries — just the counts header and a
 * note naming the knob to raise. It is never rewritten to the default 20.
 *
 * Port of Python's build_search_response (_response_builders.py:716-1278); the
 * `is_cli` strict-regex branch is not ported — the MCP path always downgrades
 * an uncompilable pattern to a literal search with a note.
 */
export function build_search_response(
  text: string,
  search_query: string,
  search_regex: boolean,
  search_case_sensitive: boolean,
  page: number | string | undefined,
  file_path: string,
  bundle?: ProjectionBundle,
  opts?: {
    max_matches?: number;
    match_offset?: number;
    full_paragraph?: boolean;
    no_chrome?: boolean;
  },
): ToolResult {
  // `max_matches < 1` is honoured as the zero it says, not silently rewritten
  // to the default 20: a caller (or a tool wrapper computing a remaining
  // budget) that asks for 0 matches and is handed 20 full snippets gets a
  // payload it never asked for (QA round 5, finding 3).
  const max_matches = opts?.max_matches ?? 20;
  const full_paragraph = opts?.full_paragraph ?? false;
  const no_chrome = opts?.no_chrome ?? false;
  let match_offset = opts?.match_offset ?? 0;
  if (match_offset < 0) match_offset = 0;

  const body = bundle ? bundle.body : split_projection(text)[0];
  const flags = search_case_sensitive ? "g" : "gi";

  const literalMatches = (): Array<{ start: number; end: number }> =>
    Array.from(
      body.matchAll(new RegExp(escapeRegExp(search_query), flags)),
      (m) => ({ start: m.index!, end: m.index! + m[0].length }),
    );

  // When the caller asked for a regex but supplied something the engine can't
  // compile (e.g. an unterminated character class `\[`, or an inline-flag group
  // `(?i)...` that JS RegExp rejects), do NOT hard-error and burn the turn.
  // Downgrade to a literal search of the raw string and tell the model, so it
  // can either accept the literal hits or fix its pattern. Patterns that blow
  // the matching time budget (catastrophic backtracking, QA 2026-07-17 F5) get
  // the same treatment — for a read-only search, degraded results beat a hung
  // event loop.
  let regexDowngradedNote = "";
  let rawMatches: Array<{ start: number; end: number }>;
  let isUserRegex = false;
  if (search_regex) {
    try {
      new RegExp(search_query, flags);
      isUserRegex = true;
    } catch (e: any) {
      regexDowngradedNote =
        `> **Note:** \`${search_query}\` is not a valid regular expression ` +
        `(${e.message}), so it was searched as literal text instead. ` +
        `If you meant a regex, fix the pattern; if you meant literal text, set \`search_regex\` to false.`;
    }
  }
  if (isUserRegex) {
    try {
      rawMatches = userFindAllMatches(search_query, body, flags);
    } catch (e: any) {
      if (!(e instanceof RegexTimeoutError)) throw e;
      regexDowngradedNote =
        `> **Note:** \`${search_query}\` was searched as literal text instead of as ` +
        `a regular expression: ${e.message}`;
      rawMatches = literalMatches();
    }
  } else {
    rawMatches = literalMatches();
  }

  // Pagination is needed for both filter mode and the distribution summary,
  // even when there are no matches (to validate `page` is in range).
  const pag_res = bundle ? bundle.pagination : paginate(body, "");
  const page_offsets = pag_res.body_page_offsets;
  const total_doc_pages = pag_res.total_pages;

  const pageOfOffset = (offset: number): number => {
    let p = 1;
    for (let j = 0; j < page_offsets.length; j++) {
      if (offset >= page_offsets[j]) p = j + 1;
      else break;
    }
    return p;
  };

  // ---- Resolve `page` into either null (= all) or a 1-indexed int. ----
  // Python reprs the offending value (`'2-4'` for a string, `0` for an int);
  // the two spellings tell an agent whether its argument arrived as a string.
  const shownPage = typeof page === "string" ? `'${page}'` : String(page);
  const badPage = (omitted_hint: string): Error =>
    new Error(
      `Invalid page value: ${shownPage}. In search mode, \`page\` must be ` +
        `${omitted_hint}, \`'all'\`, or a positive integer document page number.`,
    );
  let page_filter: number | null = null;
  if (page !== undefined && page !== null) {
    if (typeof page === "string") {
      if (page.toLowerCase() !== "all") {
        // Allow numeric strings ("3"); reject anything else — including a
        // range like "2-4", which `parseInt` would silently read as 2.
        if (!/^[+-]?\d+$/.test(page.trim())) throw badPage("omitted (search all pages)");
        page_filter = parseInt(page.trim(), 10);
        if (page_filter < 1) throw badPage("omitted");
      }
    } else if (typeof page === "number" && Number.isInteger(page)) {
      if (page < 1) throw badPage("omitted");
      page_filter = page;
    } else {
      throw badPage("omitted");
    }
  }

  if (page_filter !== null && page_filter > total_doc_pages) {
    throw new Error(
      `Document page ${page_filter} is out of range — the document has ` +
        `${total_doc_pages} page(s). In search mode, \`page\` filters matches ` +
        `by document page; omit \`page\` (or pass \`page='all'\`) to search ` +
        `across the whole document.`,
    );
  }

  const title = `Search: ${basename(file_path)}`;
  const wrap = (ui_markdown: string): ToolResult => ({
    content: [
      {
        type: "text",
        text: no_chrome
          ? ui_markdown
          : `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`,
      },
    ],
    structuredContent: {
      markdown: ui_markdown,
      title,
      file_path: resolve(file_path),
    },
  });

  // ---- No matches anywhere. ----
  if (rawMatches.length === 0) {
    let ui_markdown = no_chrome
      ? `No matches found for query \`${search_query}\`.`
      : `> **Search Results** — No matches found for query \`${search_query}\` in \`${basename(file_path)}\`.\n\n` +
        "Verify your search spelling, or try setting `search_case_sensitive` to false " +
        "or enabling `search_regex` if you used pattern wildcards.";
    if (regexDowngradedNote)
      ui_markdown = `${regexDowngradedNote}\n\n${ui_markdown}`;
    return wrap(ui_markdown);
  }

  // ---- Assign each match to its document page. ----
  const all_hits: SearchHit[] = rawMatches.map((m, i) => ({
    text: body.slice(m.start, m.end),
    start: m.start,
    end: m.end,
    page: pageOfOffset(m.start),
    index: i + 1,
  }));
  const total_matches = all_hits.length;

  // Global occurrence map and page distribution — never filtered.
  const occurrences_map: Record<string, number> = {};
  const page_distribution = new Map<number, number>();
  for (const hit of all_hits) {
    occurrences_map[hit.text] = (occurrences_map[hit.text] || 0) + 1;
    page_distribution.set(hit.page, (page_distribution.get(hit.page) || 0) + 1);
  }
  const pages_with_hits = Array.from(page_distribution.keys()).sort(
    (a, b) => a - b,
  );

  // ---- Apply filter. ----
  const filtered =
    page_filter === null
      ? all_hits
      : all_hits.filter((hit) => hit.page === page_filter);

  // `page=N` valid but has no hits, query exists elsewhere.
  if (page_filter !== null && filtered.length === 0) {
    const other_pages_str = pages_with_hits.join(", ");
    const ui_markdown = no_chrome
      ? `No matches on document page ${page_filter} for query \`${search_query}\`. ` +
        `Query appears on page(s) ${other_pages_str}.`
      : `> **Search Results** — No matches on document page ${page_filter} ` +
        `for query \`${search_query}\` in \`${basename(file_path)}\`.\n\n` +
        `The query DOES appear elsewhere (${total_matches} match` +
        `${total_matches !== 1 ? "es" : ""} on page` +
        `${pages_with_hits.length !== 1 ? "s" : ""} ${other_pages_str}). ` +
        "Omit `page` or pass `page='all'` to see them.";
    return wrap(ui_markdown);
  }

  // ---- Render. ----
  const total_filtered = filtered.length;

  /**
   * A counts header plus one explanatory note and NO match entries, for every
   * reason the requested window renders nothing: `match_offset` past the last
   * match, `max_matches` below 1, or a size budget that cannot pay for even
   * one snippet. The totals are still reported so the caller knows the query
   * itself matched.
   */
  const window_note_response = (note: string): ToolResult => {
    const ui_parts: string[] = [];
    if (!no_chrome) {
      ui_parts.push(
        page_filter === null
          ? `> **Search Results** — Found ${total_matches} match` +
              `${total_matches !== 1 ? "es" : ""} for query \`${search_query}\` ` +
              `in \`${basename(file_path)}\`.`
          : `> **Search Results** — Found ${total_filtered} match` +
              `${total_filtered !== 1 ? "es" : ""} on document page ${page_filter} ` +
              `for query \`${search_query}\` in \`${basename(file_path)}\` ` +
              `(${total_matches} total in document).`,
      );
    }
    ui_parts.push(note);
    if (regexDowngradedNote) ui_parts.unshift(regexDowngradedNote);
    return wrap(ui_parts.filter((part) => part).join("\n\n"));
  };

  if (max_matches < 1) {
    return window_note_response(
      no_chrome
        ? `No matches shown (max_matches=${max_matches}, total matches=${total_filtered}).`
        : `> **Note:** No matches shown (max_matches=${max_matches}, total matches=${total_filtered}). ` +
            "Pass `max_matches=N` with N >= 1 to see match snippets.",
    );
  }

  if (match_offset >= total_filtered) {
    return window_note_response(
      no_chrome
        ? `No matches in this window (match_offset=${match_offset}, total matches=${total_filtered}).`
        : `> **Note:** No matches in this window (match_offset=${match_offset}, total matches=${total_filtered}).`,
    );
  }

  const selected_matches = filtered.slice(
    match_offset,
    match_offset + max_matches,
  );

  /**
   * Header, distribution, and continuation notes for a response that renders
   * `num_rendered` of the filtered matches. Built from the final count so the
   * "N shown" figure and the `match_offset` to continue from stay truthful
   * when the budget pass drops trailing entries.
   */
  function build_header(num_rendered: number): string[] {
    const head: string[] = [];
    const next_offset = match_offset + num_rendered;
    const has_more = next_offset < total_filtered;

    if (page_filter === null) {
      const found =
        `> **Search Results** — Found ${total_matches} match` +
        `${total_matches !== 1 ? "es" : ""} for query \`${search_query}\` ` +
        `in \`${basename(file_path)}\``;
      head.push(
        total_filtered > num_rendered || match_offset > 0
          ? `${found} (${total_matches} total, ${num_rendered} shown).`
          : `${found}.`,
      );
      // Distribution summary only when matches span >1 document page.
      if (pages_with_hits.length > 1) {
        const dist_str = pages_with_hits
          .map((p) => `p${p}: ${page_distribution.get(p)}`)
          .join(", ");
        head.push(
          `> Distribution across ${pages_with_hits.length} document pages — ${dist_str}`,
        );
      }
    } else {
      const found =
        `> **Search Results** — Found ${total_filtered} match` +
        `${total_filtered !== 1 ? "es" : ""} on document page ${page_filter} ` +
        `for query \`${search_query}\` in \`${basename(file_path)}\` ` +
        `(${total_matches} total in document`;
      head.push(
        total_filtered > num_rendered || match_offset > 0
          ? `${found}, ${num_rendered} shown).`
          : `${found}).`,
      );
      const other_pages = pages_with_hits.filter((p) => p !== page_filter);
      if (other_pages.length) {
        head.push(
          `> Additional matches exist on page` +
            `${other_pages.length !== 1 ? "s" : ""} ${other_pages.join(", ")} — ` +
            "omit `page` or pass `page='all'` to see them.",
        );
      }
    }

    if (has_more) {
      head.push(
        `> **Note:** Only ${num_rendered} matches shown (max_matches=${max_matches}). ` +
          `Continue with \`match_offset=${next_offset}\`.`,
      );
    }
    return head;
  }

  // Breadcrumbs render CLEAN-view heading text: a heading carrying a pending
  // tracked change must not leak raw CriticMarkup into the Path line (QA
  // 2026-07-23 F22b). Deletions vanish, insertions/highlights unwrap to their
  // text, meta bubbles drop. Because we operate on ONE line of the projection,
  // a multi-line `{>>…<<}` bubble can be clipped by the line break — drop the
  // unterminated tail too, then sweep any leftover delimiter fragments.
  // Hoisted to @adeu/core's outline module for CC-2 so the fields ledger
  // renders identical breadcrumbs from the same projection rather than a
  // second dialect.
  const get_heading = heading_path_at;

  /**
   * Groups hits by their containing projection line: one paragraph renders as
   * ONE entry with every hit emphasized, instead of once per regex alternation
   * branch with divergent highlights (QA round 3, finding 3.10). Called on
   * every budget attempt, because the unit the budget pass drops is the HIT,
   * not the entry.
   */
  function group_by_line(hits: SearchHit[]): Array<[number, SearchHit[]]> {
    const groups: Array<[number, SearchHit[]]> = [];
    const by_line = new Map<number, SearchHit[]>();
    for (const hit of hits) {
      const last_nl = hit.start <= 0 ? -1 : body.lastIndexOf("\n", hit.start - 1);
      const line_start = last_nl === -1 ? 0 : last_nl + 1;
      let group = by_line.get(line_start);
      if (!group) {
        group = [];
        by_line.set(line_start, group);
        groups.push([line_start, group]);
      }
      group.push(hit);
    }
    return groups;
  }

  /**
   * Renders one paragraph's hits as a single match entry. `radius` is the
   * context kept on each side of every hit; null renders the whole paragraph
   * (`full_paragraph`). Blocks are joined with blank lines because each one is
   * its own Markdown block.
   */
  function render_entry(
    line_start: number,
    group: SearchHit[],
    radius: number | null,
  ): string {
    const first = group[0];
    const last_hit_end = Math.max(...group.map((hit) => hit.end));
    const next_nl = body.indexOf("\n", last_hit_end);
    const line_end = next_nl === -1 ? body.length : next_nl;

    let intervals: Array<[number, number]>;
    if (radius === null) {
      intervals = [[line_start, line_end]];
    } else {
      const windows: Array<[number, number]> = group.map((hit) => [
        Math.max(line_start, hit.start - radius),
        Math.min(line_end, hit.end + radius),
      ]);
      // Balance AFTER merging (a widened window can swallow its neighbour) and
      // merge again, so no two segments overlap. Then snap each edge out to a
      // code-point boundary: `radius` is a code-unit distance here, so an edge
      // can land between the halves of a surrogate pair. Snapping cannot make
      // two intervals overlap — merged intervals are separated by at least one
      // code unit, and the unit after a pair is never a low surrogate.
      intervals = mergeSpans(
        mergeSpans(windows).map(([s, e]) => balanceSnippetWindow(body, s, e)),
      ).map(([s, e]) => snapCodePointBoundary(body, s, e));
    }

    const segments: string[] = [];
    for (const [s_pos, e_pos] of intervals) {
      const spans: Array<[number, number]> = group
        .filter((hit) => s_pos <= hit.start && hit.end <= e_pos)
        .map((hit) => [hit.start - s_pos, hit.end - s_pos]);
      // Re-attach the tags of any span this window sits strictly inside (QA
      // round 5, finding 2): a window cut out of the middle of a deletion holds
      // no delimiters at all, so without this the deleted clause reads as live
      // prose. The tags are added OUTSIDE emphasizedSnippetSpans, whose job is
      // stripping the document's own bold/italic markers.
      const [open_tags, close_tags] = enclosingSnippetMarkup(body, s_pos, e_pos);
      segments.push(
        open_tags +
          emphasizedSnippetSpans(body.slice(s_pos, e_pos), spans) +
          close_tags,
      );
    }

    // " ... " marks elided interior text between distant hits; the outer "..."
    // marks context trimmed off the head/tail, measured against the line each
    // EDGE landed on (balancing an unterminated bubble can pull the window onto
    // an earlier line).
    let snippet = segments.join(" ... ");
    const window_start = intervals[0][0];
    const window_end = intervals[intervals.length - 1][1];
    const nl_before =
      window_start <= 0 ? -1 : body.lastIndexOf("\n", window_start - 1);
    const first_line_start = nl_before + 1;
    const nl_after = body.indexOf("\n", window_end);
    const last_line_end = nl_after === -1 ? body.length : nl_after;
    if (window_start > first_line_start) snippet = "..." + snippet;
    if (window_end < last_line_end) snippet = snippet + "...";

    const snippet_lines = snippet
      .split("\n")
      .filter((line) => line.trim().length > 0)
      .map((line) => `> ${line}`)
      .join("\n");

    const match_lines = ["---", `### Match ${first.index} (p${first.page})`];
    const h_path = get_heading(first.start, body);
    if (h_path) match_lines.push(`**Path:** \`${h_path}\``);

    const distinct_strs: string[] = [];
    for (const hit of group)
      if (!distinct_strs.includes(hit.text)) distinct_strs.push(hit.text);
    let occurrence_line: string;
    if (distinct_strs.length === 1) {
      const n = occurrences_map[distinct_strs[0]];
      occurrence_line = `*Occurrences:* This exact phrasing appears ${n} time${n !== 1 ? "s" : ""} in the document.`;
    } else {
      occurrence_line =
        "*Occurrences:* " +
        distinct_strs
          .map((s) => {
            const n = occurrences_map[s];
            return `\`${s}\` appears ${n} time${n !== 1 ? "s" : ""}`;
          })
          .join("; ") +
        " in the document.";
    }
    match_lines.push(snippet_lines, occurrence_line);
    return match_lines.join("\n\n");
  }

  const content_prefix = no_chrome
    ? ""
    : `> **File Path:** \`${resolve(file_path)}\`\n\n`;

  function compose(
    hits: SearchHit[],
    radius: number | null,
    budget_note: string,
  ): string {
    const parts: string[] = no_chrome ? [] : build_header(hits.length);
    for (const [line_start, group] of group_by_line(hits))
      parts.push(render_entry(line_start, group, radius));
    if (budget_note && !no_chrome) parts.push(budget_note);
    // The downgrade note survives `no_chrome`: it reports that the query was
    // searched with DIFFERENT semantics than asked for, so suppressing it would
    // make the hit list read as regex matches.
    if (regexDowngradedNote) parts.unshift(regexDowngradedNote);
    return parts.filter((part) => part).join("\n\n");
  }

  // ---- Response size budget (QA round 5, finding 2). ----
  // A ±120 window is up to 240 chars of context PER HIT, so 20 hits in long
  // paragraphs blow the ceiling this response is sized against even though each
  // snippet is individually clamped. Render at the widest radius that fits the
  // whole payload; if even the narrowest does not, drop trailing HITS (the
  // caller reaches them with match_offset) rather than emit an oversized
  // response. `full_paragraph` is an explicit opt-out.
  //
  // The unit dropped is the HIT, never the entry: when 20 hits share ONE
  // projection line there is a single entry, so an entry-dropping pass has
  // nothing to drop — and the radius ladder cannot rescue that case either,
  // because a balanced window is at least as wide as the CriticMarkup spans it
  // must keep whole, however small the radius (QA round 5, finding 1).
  const fits = (markdown: string, rendered_count: number): boolean =>
    content_prefix.length + markdown.length <=
    search_budget_tokens(max_matches, rendered_count) * CHARS_PER_TOKEN;

  let ui_markdown: string;
  if (full_paragraph) {
    ui_markdown = compose(selected_matches, null, "");
  } else {
    let radius = SNIPPET_RADIUS_LADDER[0];
    ui_markdown = compose(selected_matches, radius, "");
    for (const rung of SNIPPET_RADIUS_LADDER.slice(1)) {
      radius = rung;
      if (fits(ui_markdown, selected_matches.length)) break;
      ui_markdown = compose(
        selected_matches,
        radius,
        `> **Note:** Snippets trimmed to ±${radius} chars to fit the response size budget.`,
      );
    }
    const kept = [...selected_matches];
    while (kept.length && !fits(ui_markdown, kept.length)) {
      kept.pop();
      if (!kept.length) {
        return window_note_response(
          no_chrome
            ? `No matches shown in this window: not even one ±${radius}-char snippet fits ` +
                `the response size budget (max_matches=${max_matches}, total matches=${total_filtered}).`
            : `> **Note:** No matches shown in this window: not even one ±${radius}-char snippet fits ` +
                `the response size budget (max_matches=${max_matches}, total matches=${total_filtered}). ` +
                "Raise `max_matches`, or pass `full_paragraph=true` to read the matching paragraph in full.",
        );
      }
      ui_markdown = compose(
        kept,
        radius,
        `> **Note:** Snippets trimmed to ±${radius} chars and trailing matches dropped ` +
          "to fit the response size budget — continue from the `match_offset` above.",
      );
    }
  }

  return wrap(ui_markdown);
}
