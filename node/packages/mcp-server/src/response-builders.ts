import { resolve, basename } from "node:path";
import {
  DocumentObject,
  paginate,
  split_structural_appendix,
  extract_outline,
  OutlineNode,
} from "@adeu/core";

export interface ToolResult {
  content: { type: "text"; text: string }[];
  structuredContent?: any;
  isError?: boolean;
  [key: string]: unknown;
}

function _build_appendix_pointer(has_appendix: boolean): string {
  if (!has_appendix) return "";
  return `\n\n---\n\n> **Appendix available.** This document has structural metadata (defined terms, cross-references, bookmarks, diagnostics) that may be relevant when editing. Call \`read_docx\` with \`mode='appendix'\` to load it before submitting edits.`;
}

function _build_page_banner(page: number, total: number): string {
  if (total <= 1) return "";
  return `> **Page ${page} of ${total}** — call \`read_docx\` with \`mode='outline'\` for a heading map of the full document.\n\n---\n\n`;
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
  page: number | string,
  file_path: string,
): ToolResult {
  const [body] = split_structural_appendix(text);
  const escapeRegExp = (s: string) =>
    s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const flags = search_case_sensitive ? "g" : "gi";
  const patternStr = search_regex ? search_query : escapeRegExp(search_query);

  let regex: RegExp;
  try {
    regex = new RegExp(patternStr, flags);
  } catch (e: any) {
    throw new Error(`Invalid regex pattern: ${e.message}`);
  }

  const matches = Array.from(body.matchAll(regex));

  if (matches.length === 0) {
    const ui_markdown = `> **Search Results** — No matches found for query \`${search_query}\` in \`${basename(file_path)}\`.\n\nVerify your search spelling, or try setting \`search_case_sensitive\` to false or enabling \`search_regex\` if you used pattern wildcards.`;
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

  const pag_res = paginate(body, "");
  const page_offsets = pag_res.body_page_offsets;
  const total_matches = matches.length;
  const total_pages = Math.ceil(total_matches / 10);

  let start_idx = 0;
  let end_idx = total_matches;
  let page_text = "all";

  const pageStr = String(page).toLowerCase();
  if (pageStr !== "all") {
    const page_num = parseInt(pageStr, 10);
    if (isNaN(page_num) || page_num < 1 || page_num > total_pages) {
      throw new Error(`Page ${page} out of range (search has ${total_pages} pages).`);
    }
    start_idx = (page_num - 1) * 10;
    end_idx = Math.min(start_idx + 10, total_matches);
    page_text = `${page_num} of ${total_pages}`;
  }

  const page_matches = matches.slice(start_idx, end_idx);

  const ui_parts: string[] = [
    `> **Search Results** — Found ${total_matches} matches for query \`${search_query}\` in \`${basename(file_path)}\`.`,
  ];

  if (total_pages > 1 && pageStr !== "all") {
    const nextPage = parseInt(pageStr, 10) + 1;
    ui_parts.push(`> Showing page ${page_text} (matches ${start_idx + 1}-${end_idx}). To see more matches, call \`read_docx\` with \`search_query='${search_query}'\`, \`search_regex=${search_regex ? "true" : "false"}\`, and \`page=${nextPage}\`.`);
  }

  const occurrences_map: Record<string, number> = {};
  for (const m of matches) {
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
          let cleanHeading = m[2].replace(/\*\*|__|[*_]/g, "").replace(/\{#[^}]+\}/g, "").trim();
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

  let i = start_idx + 1;
  for (const m of page_matches) {
    const matched_str = m[0];
    const m_start = m.index!;
    const m_end = m_start + matched_str.length;

    let p_num = 1;
    for (let j = 0; j < page_offsets.length; j++) {
      if (m_start >= page_offsets[j]) {
        p_num = j + 1;
      } else {
        break;
      }
    }

    const snippet_start = Math.max(0, m_start - 100);
    const snippet_end = Math.min(body.length, m_end + 100);
    const snippet = body.substring(snippet_start, m_start) + `**${matched_str}**` + body.substring(m_end, snippet_end);

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
    ui_parts.push(`*Occurrences:* This exact phrasing appears ${count} time${count !== 1 ? "s" : ""} in the document.`);

    i++;
  }

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
