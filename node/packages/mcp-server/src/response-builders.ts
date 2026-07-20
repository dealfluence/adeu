import { resolve, basename } from "node:path";
import {
  DocumentObject,
  paginate,
  split_structural_appendix,
  extract_outline,
  OutlineNode,
  RegexTimeoutError,
  userFindAllMatches,
} from "@adeu/core";

export interface ToolResult {
  content: { type: "text"; text: string }[];
  structuredContent?: any;
  isError?: boolean;
  [key: string]: unknown;
}

// Projection style markers: `**bold**` always; `_italic_` only where the
// underscore is not intra-word (identifiers like snake_case are literal text —
// the projection's italics markers always hug non-whitespace at a word edge).
const STYLE_MARKER_RE = /\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])/g;

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
  const region = prefix + match + suffix;
  const b1 = prefix.length;
  const b2 = prefix.length + match.length;
  const keep = new Array<boolean>(region.length).fill(true);
  for (const m of region.matchAll(STYLE_MARKER_RE)) {
    for (let i = m.index!; i < m.index! + m[0].length; i++) keep[i] = false;
  }
  let strippedPrefix = "";
  let strippedMatch = "";
  let strippedSuffix = "";
  for (let i = 0; i < region.length; i++) {
    if (!keep[i]) continue;
    if (i < b1) strippedPrefix += region[i];
    else if (i < b2) strippedMatch += region[i];
    else strippedSuffix += region[i];
  }
  return `${strippedPrefix}**${strippedMatch}**${strippedSuffix}`;
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
): string {
  if (!nodes || nodes.length === 0) {
    return "# (No headings detected)\n\nThis document has no detectable headings.";
  }

  const visible = nodes.filter((n) => n.level <= max_level);

  if (visible.length === 0) {
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
      lines.push(`${prefix} ${node.text} (p${node.page})`);
    }
  }
  return lines.join("\n");
}

export function build_full_document_response(
  text: string,
  file_path: string,
): ToolResult {
  // The ENTIRE document body with no page banner, continuation footer, or
  // appendix pointer — the round-trip artifact for text-based apply/diff
  // (QA 2026-07-17 F1; mirrors Python's build_full_document_response).
  const [body] = split_structural_appendix(text);
  const ui_markdown = body;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: basename(file_path),
    },
  };
}

export function build_paginated_response(
  text: string,
  page: number,
  file_path: string,
): ToolResult {
  const [body, appendix] = split_structural_appendix(text);
  const has_appendix = Boolean(appendix.trim());

  const result = paginate(body, "");

  if (page < 1 || page > result.total_pages) {
    throw new Error(
      `Page ${page} out of range (doc has ${result.total_pages} pages).`,
    );
  }

  const selected = result.pages[page - 1];
  const banner = _build_page_banner(selected.page, selected.total_pages);
  const footer = _build_page_footer(
    selected.page,
    selected.total_pages,
    selected.has_next,
  );
  const appendix_pointer = _build_appendix_pointer(has_appendix);

  const ui_markdown =
    banner + selected.page_content + footer + appendix_pointer;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

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

export function build_outline_response(
  doc: DocumentObject,
  projected_text: string,
  file_path: string,
  outline_max_level: number = 2,
  outline_verbose: boolean = false,
  paragraph_offsets: Map<any, [number, number]> | null = null,
): ToolResult {
  // Levels outside 1-6 are meaningless (0/negative would render a
  // nonsensical "L1-L0" range label, QA L2). Clamp to the nearest sensible
  // depth, mirroring the Python builder.
  outline_max_level = Math.max(1, Math.min(outline_max_level, 6));

  const [body] = split_structural_appendix(projected_text);
  const pagination_result = paginate(body, "");

  const nodes = extract_outline(
    doc,
    body,
    pagination_result.body_pages,
    pagination_result.body_page_offsets,
    paragraph_offsets,
  );

  const rendered = render_outline_tree(
    nodes,
    outline_max_level,
    outline_verbose,
  );

  const visible_count = nodes.filter(
    (n) => n.level <= outline_max_level,
  ).length;
  const deeper_count = nodes.length - visible_count;
  const deeper_hint =
    deeper_count > 0
      ? ` (${deeper_count} more at deeper levels, raise outline_max_level to see)`
      : "";

  const header = `> **Outline view** — showing ${visible_count} of ${nodes.length} headings (L1-L${outline_max_level}${deeper_hint}) across ${pagination_result.total_pages} page(s). Call \`read_docx\` with \`mode='full'\` and \`page=N\` to read a section.\n\n---\n\n`;
  const ui_markdown = header + rendered;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

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
): ToolResult {
  const [, appendix] = split_structural_appendix(text);

  if (!appendix.trim()) {
    const ui_markdown =
      "# Appendix\n\nThis document has no structural appendix (no defined terms, named anchors, or diagnostics detected).";
    const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
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

  const ui_markdown = banner + selected.page_content + footer;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      file_path: resolve(file_path),
      title: `Appendix: ${basename(file_path)}`,
    },
  };
}

export function build_search_response(
  text: string,
  search_query: string,
  search_regex: boolean,
  search_case_sensitive: boolean,
  page: number | string | undefined,
  file_path: string,
): ToolResult {
  const [body] = split_structural_appendix(text);
  const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const flags = search_case_sensitive ? "g" : "gi";

  // When the caller asked for a regex but supplied something the engine can't
  // compile (e.g. an unterminated character class `\[`, or an inline-flag group
  // `(?i)...` that JS RegExp rejects), do NOT hard-error and burn the turn.
  // Downgrade to a literal search of the raw string and tell the model, so it
  // can either accept the literal hits or fix its pattern — instead of retrying
  // the same broken regex.
  let regexDowngradedNote = "";
  let regex: RegExp;
  let isUserRegex = false;
  if (search_regex) {
    try {
      regex = new RegExp(search_query, flags);
      isUserRegex = true;
    } catch (e: any) {
      regexDowngradedNote =
        `> **Note:** \`${search_query}\` is not a valid regular expression ` +
        `(${e.message}), so it was searched as literal text instead. ` +
        `If you meant a regex, fix the pattern; if you meant literal text, set \`search_regex\` to false.`;
      regex = new RegExp(escapeRegExp(search_query), flags);
    }
  } else {
    regex = new RegExp(escapeRegExp(search_query), flags);
  }

  // Patterns that blow the matching time budget (catastrophic backtracking,
  // QA 2026-07-17 F5) get the same literal downgrade as invalid patterns —
  // for a read-only search, degraded results beat a hung event loop.
  let allMatches: Array<{ 0: string; index?: number }>;
  if (isUserRegex) {
    try {
      allMatches = userFindAllMatches(search_query, body, flags).map((m) => ({
        0: body.slice(m.start, m.end),
        index: m.start,
      }));
    } catch (e: any) {
      if (!(e instanceof RegexTimeoutError)) throw e;
      regexDowngradedNote =
        `> **Note:** \`${search_query}\` was searched as literal text instead of as ` +
        `a regular expression: ${e.message}`;
      allMatches = Array.from(body.matchAll(new RegExp(escapeRegExp(search_query), flags)));
    }
  } else {
    allMatches = Array.from(body.matchAll(regex));
  }

  // Compute document pagination once — needed for both annotation and filtering.
  const pag_res = paginate(body, "");
  const page_offsets = pag_res.body_page_offsets;
  const total_doc_pages = pag_res.total_pages;

  // Resolve `page` parameter to either "all" or a concrete document-page number.
  // Undefined → "all" (search across the whole document).
  // "all" (case-insensitive) → "all".
  // A positive integer N → filter matches to document page N.
  // Anything else → hard error.
  let filter_doc_page: number | null = null; // null means "all"
  if (page !== undefined && page !== null) {
    const pageStr = String(page).toLowerCase();
    if (pageStr !== "all") {
      const parsed = parseInt(pageStr, 10);
      if (isNaN(parsed) || parsed < 1) {
        throw new Error(
          `Invalid page value: \`${page}\`. Pass a positive integer to restrict the search to that document page, omit \`page\` to search all pages, or pass \`page='all'\` explicitly.`,
        );
      }
      if (parsed > total_doc_pages) {
        throw new Error(
          `Document page ${parsed} is out of range — the document has ${total_doc_pages} page(s). In search mode, \`page\` filters matches to a specific document page; omit it or pass \`page='all'\` to search the whole document.`,
        );
      }
      filter_doc_page = parsed;
    }
  }

  // Helper: which document page does an offset live on?
  const pageOfOffset = (offset: number): number => {
    let p = 1;
    for (let j = 0; j < page_offsets.length; j++) {
      if (offset >= page_offsets[j]) p = j + 1;
      else break;
    }
    return p;
  };

  // Apply the filter (if any), but keep a record of all pages that had hits
  // so we can show a useful summary even when filtered.
  const pagesWithHits = new Set<number>();
  for (const m of allMatches) {
    pagesWithHits.add(pageOfOffset(m.index!));
  }

  const matches =
    filter_doc_page === null
      ? allMatches
      : allMatches.filter((m) => pageOfOffset(m.index!) === filter_doc_page);

  // --- Empty result ---
  if (matches.length === 0) {
    let body_msg: string;
    if (filter_doc_page !== null) {
      if (allMatches.length === 0) {
        body_msg = `> **Search Results** — No matches found for query \`${search_query}\` in \`${basename(file_path)}\`.\n\nVerify your search spelling, or try setting \`search_case_sensitive\` to false or enabling \`search_regex\` if you used pattern wildcards.`;
      } else {
        const hitPages = Array.from(pagesWithHits).sort((a, b) => a - b);
        body_msg = `> **Search Results** — No matches for \`${search_query}\` on document page ${filter_doc_page}.\n\nThe query DOES appear elsewhere in the document (${allMatches.length} match${allMatches.length !== 1 ? "es" : ""} on page${hitPages.length !== 1 ? "s" : ""} ${hitPages.join(", ")}). Omit \`page\` or pass \`page='all'\` to see them.`;
      }
    } else {
      body_msg = `> **Search Results** — No matches found for query \`${search_query}\` in \`${basename(file_path)}\`.\n\nVerify your search spelling, or try setting \`search_case_sensitive\` to false or enabling \`search_regex\` if you used pattern wildcards.`;
    }
    if (regexDowngradedNote) body_msg = `${regexDowngradedNote}\n\n${body_msg}`;
    const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${body_msg}`;
    return {
      content: [{ type: "text", text: llm_content }],
      structuredContent: {
        markdown: body_msg,
        title: `Search: ${basename(file_path)}`,
        file_path: resolve(file_path),
      },
    };
  }

  // --- Build the response ---
  const ui_parts: string[] = [];

  if (filter_doc_page !== null) {
    ui_parts.push(
      `> **Search Results** — Found ${matches.length} match${matches.length !== 1 ? "es" : ""} for \`${search_query}\` on document page ${filter_doc_page} of ${total_doc_pages} in \`${basename(file_path)}\`.`,
    );
    const otherPages = Array.from(pagesWithHits)
      .filter((p) => p !== filter_doc_page)
      .sort((a, b) => a - b);
    if (otherPages.length > 0) {
      ui_parts.push(
        `> Additional matches exist on page${otherPages.length !== 1 ? "s" : ""} ${otherPages.join(", ")} — omit \`page\` or pass \`page='all'\` to see them.`,
      );
    }
  } else {
    ui_parts.push(
      `> **Search Results** — Found ${matches.length} match${matches.length !== 1 ? "es" : ""} for \`${search_query}\` in \`${basename(file_path)}\`.`,
    );
    if (total_doc_pages > 1) {
      // Build a per-page hit distribution: "p1: 3, p3: 1, p7: 12"
      const counts = new Map<number, number>();
      for (const m of allMatches) {
        const p = pageOfOffset(m.index!);
        counts.set(p, (counts.get(p) || 0) + 1);
      }
      const distribution = Array.from(counts.entries())
        .sort((a, b) => a[0] - b[0])
        .map(([p, n]) => `p${p}: ${n}`)
        .join(", ");
      ui_parts.push(
        `> Distribution across ${total_doc_pages} document pages — ${distribution}. Pass \`page=N\` to filter to a specific document page.`,
      );
    }
  }

  // Per-match occurrence counts use the FULL match set, not the filtered one —
  // this gives the LLM accurate global counts even when filtering.
  const occurrences_map: Record<string, number> = {};
  for (const m of allMatches) {
    const matched_str = m[0];
    occurrences_map[matched_str] = (occurrences_map[matched_str] || 0) + 1;
  }

  function get_heading(idx: number, txt: string): string {
    const txtBefore = txt.substring(0, idx);
    const lines = txtBefore.split("\n");
    const path: string[] = [];
    let current_level = 999;

    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      const m = line.match(/^(#{1,6})\s+(.*)/);
      if (m) {
        const level = m[1].length;
        if (level < current_level) {
          let cleanHeading = m[2]
            .replace(/\*\*|__|[*_]/g, "")
            .replace(/\{#[^}]+\}/g, "")
            .trim();
          if (cleanHeading.length > 80) {
            cleanHeading = cleanHeading.substring(0, 80) + "...";
          }
          path.unshift(cleanHeading);
          current_level = level;
          if (level === 1) break;
        }
      }
    }
    return path.join(" > ");
  }

  let i = 1;
  for (const m of matches) {
    const matched_str = m[0];
    const m_start = m.index!;
    const m_end = m_start + matched_str.length;
    const p_num = pageOfOffset(m_start);

    const snippet_start = Math.max(0, m_start - 100);
    const snippet_end = Math.min(body.length, m_end + 100);
    const snippet = emphasizedSnippet(
      body.substring(snippet_start, m_start),
      matched_str,
      body.substring(m_end, snippet_end),
    );

    const snippet_lines = snippet
      .split("\n")
      .filter((line) => line.trim().length > 0)
      .map((line) => `> ${line}`)
      .join("\n");

    ui_parts.push("---");
    ui_parts.push(`### Match ${i} (p${p_num})`);

    const h_path = get_heading(m_start, body);
    if (h_path) {
      ui_parts.push(`**Path:** \`${h_path}\``);
    }

    const count = occurrences_map[matched_str];
    ui_parts.push(snippet_lines);
    ui_parts.push(
      `*Occurrences:* This exact phrasing appears ${count} time${count !== 1 ? "s" : ""} in the document.`,
    );

    i++;
  }

  if (regexDowngradedNote) ui_parts.unshift(regexDowngradedNote);
  const ui_markdown = ui_parts.join("\n\n");
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

  return {
    content: [{ type: "text", text: llm_content }],
    structuredContent: {
      markdown: ui_markdown,
      title: `Search: ${basename(file_path)}`,
      file_path: resolve(file_path),
    },
  };
}
