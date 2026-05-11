import { resolve, basename } from 'node:path';
import { 
  DocumentObject, 
  paginate, 
  split_structural_appendix, 
  extract_outline, 
  OutlineNode 
} from '@adeu/core';

export interface ToolResult {
  content: { type: 'text'; text: string }[];
  isError?: boolean;
  [key: string]: unknown;
}

function _build_appendix_pointer(has_appendix: boolean): string {
  if (!has_appendix) return '';
  return `\n\n---\n\n> **Appendix available.** This document has structural metadata (defined terms, cross-references, bookmarks, diagnostics) that may be relevant when editing. Call \`read_docx\` with \`mode='appendix'\` to load it before submitting edits.`;
}

function _build_page_banner(page: number, total: number): string {
  if (total <= 1) return '';
  return `> **Page ${page} of ${total}** — call \`read_docx\` with \`mode='outline'\` for a heading map of the full document.\n\n---\n\n`;
}

function _build_page_footer(page: number, total: number, has_next: boolean): string {
  if (total <= 1 || !has_next) return '';
  return `\n\n---\n\n> **Continues on page ${page + 1} of ${total}.**`;
}

export function render_outline_tree(nodes: OutlineNode[], max_level: number = 2, verbose: boolean = false): string {
  if (!nodes || nodes.length === 0) {
    return '# (No headings detected)\n\nThis document has no detectable headings.';
  }

  const visible = nodes.filter(n => n.level <= max_level);

  if (visible.length === 0) {
    return `# (No headings at level <= ${max_level})\n\nDocument has ${nodes.length} headings, all at deeper levels. Call read_docx with mode='outline' and outline_max_level=N (up to 6) to see them.`;
  }

  const lines: string[] = [];
  for (const node of visible) {
    const prefix = '#'.repeat(node.level);
    if (verbose) {
      const meta_parts = [`p${node.page}`, node.style];
      if (node.has_table) meta_parts.push('has table');
      if (node.footnote_ids && node.footnote_ids.length > 0) meta_parts.push('fn:' + node.footnote_ids.join(','));
      lines.push(`${prefix} ${node.text} (${meta_parts.join(', ')})`);
    } else {
      lines.push(`${prefix} ${node.text} (p${node.page})`);
    }
  }
  return lines.join('\n');
}

export function build_paginated_response(text: string, page: number, file_path: string): ToolResult {
  const [body, appendix] = split_structural_appendix(text);
  const has_appendix = Boolean(appendix.trim());

  const result = paginate(body, '');

  if (page < 1 || page > result.total_pages) {
    throw new Error(`Page ${page} out of range (doc has ${result.total_pages} pages).`);
  }

  const selected = result.pages[page - 1];
  const banner = _build_page_banner(selected.page, selected.total_pages);
  const footer = _build_page_footer(selected.page, selected.total_pages, selected.has_next);
  const appendix_pointer = _build_appendix_pointer(has_appendix);
  
  const ui_markdown = banner + selected.page_content + footer + appendix_pointer;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

  return {
    content: [{ type: 'text', text: llm_content }]
  };
}

export function build_outline_response(
  doc: DocumentObject, 
  projected_text: string, 
  file_path: string, 
  outline_max_level: number = 2, 
  outline_verbose: boolean = false
): ToolResult {
  const [body] = split_structural_appendix(projected_text);
  const pagination_result = paginate(body, '');

  const nodes = extract_outline(
    doc,
    body,
    pagination_result.body_pages,
    pagination_result.body_page_offsets
  );

  const rendered = render_outline_tree(nodes, outline_max_level, outline_verbose);

  const visible_count = nodes.filter(n => n.level <= outline_max_level).length;
  const deeper_count = nodes.length - visible_count;
  const deeper_hint = deeper_count > 0 ? ` (${deeper_count} more at deeper levels, raise outline_max_level to see)` : '';

  const header = `> **Outline view** — showing ${visible_count} of ${nodes.length} headings (L1-L${outline_max_level}${deeper_hint}) across ${pagination_result.total_pages} page(s). Call \`read_docx\` with \`mode='full'\` and \`page=N\` to read a section.\n\n---\n\n`;
  const ui_markdown = header + rendered;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

  return {
    content: [{ type: 'text', text: llm_content }]
  };
}

export function build_appendix_response(text: string, page: number, file_path: string): ToolResult {
  const [, appendix] = split_structural_appendix(text);

  if (!appendix.trim()) {
    const ui_markdown = "# Appendix\n\nThis document has no structural appendix (no defined terms, named anchors, or diagnostics detected).";
    const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;
    return {
      content: [{ type: 'text', text: llm_content }]
    };
  }

  const result = paginate(appendix, '');

  if (page < 1 || page > result.total_pages) {
    throw new Error(`Appendix page ${page} out of range (appendix has ${result.total_pages} pages).`);
  }

  const selected = result.pages[page - 1];

  let banner = '';
  let footer = '';

  if (selected.total_pages > 1) {
    banner = `> **Appendix page ${selected.page} of ${selected.total_pages}** — structural metadata for this document.\n\n---\n\n`;
    footer = selected.has_next ? `\n\n---\n\n> **Continues on appendix page ${selected.page + 1} of ${selected.total_pages}.**` : '';
  } else {
    banner = "> **Appendix** — structural metadata for this document.\n\n---\n\n";
  }

  const ui_markdown = banner + selected.page_content + footer;
  const llm_content = `> **File Path:** \`${resolve(file_path)}\`\n\n${ui_markdown}`;

  return {
    content: [{ type: 'text', text: llm_content }]
  };
}