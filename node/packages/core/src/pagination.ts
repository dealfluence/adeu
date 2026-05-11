/**
 * Stateless paginator for projected DOCX Markdown.
 */

const PAGE_TARGET_CHARS = 19_000;
const APPENDIX_MARKER = '<!-- READONLY_BOUNDARY_START -->';

const _CRITIC_TOKENS: Record<string, string> = {
  '{++': '++}',
  '{--': '--}',
  '{==': '==}',
  '{>>': '<<}',
};

const _CHG_ID_PATTERN = /\bChg:(\d+)\b/g;

export interface PageInfo {
  page: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
  tracked_change_count: number;
  page_content: string;
}

export interface PaginationResult {
  pages: PageInfo[];
  total_pages: number;
  body_pages: string[];
  body_page_offsets: number[];
}

export function split_structural_appendix(markdown: string): [string, string] {
  if (!markdown) return ['', ''];

  const idx = markdown.indexOf(APPENDIX_MARKER);
  if (idx === -1) return [markdown, ''];

  const line_start = markdown.lastIndexOf('\n', idx) + 1;
  const body = markdown.substring(0, line_start).trimEnd();
  const appendix = markdown.substring(line_start);

  return [body, appendix];
}

export function paginate(markdown_body: string, structural_appendix: string = ''): PaginationResult {
  if (!markdown_body) {
    const appendix_clean = structural_appendix ? structural_appendix.trim() : '';
    const content = appendix_clean;
    return {
      pages: [{
        page: 1,
        total_pages: 1,
        has_next: false,
        has_prev: false,
        tracked_change_count: _count_tracked_changes(content),
        page_content: content,
      }],
      total_pages: 1,
      body_pages: [''],
      body_page_offsets: [0],
    };
  }

  const block_records = _tokenize_into_atomic_blocks(markdown_body);
  const [body_pages, body_page_offsets] = _assemble_pages(block_records);

  let final_pages: string[];
  if (structural_appendix && structural_appendix.trim()) {
    const appendix = structural_appendix.trim();
    final_pages = body_pages.map(bp => bp ? `${bp}\n\n${appendix}` : appendix);
  } else {
    final_pages = [...body_pages];
  }

  const total = final_pages.length;
  const page_infos: PageInfo[] = final_pages.map((content, i) => ({
    page: i + 1,
    total_pages: total,
    has_next: (i + 1 < total),
    has_prev: (i + 1 > 1),
    tracked_change_count: _count_tracked_changes(content),
    page_content: content,
  }));

  return {
    pages: page_infos,
    total_pages: total,
    body_pages,
    body_page_offsets,
  };
}

function _tokenize_into_atomic_blocks(markdown_body: string): [string, number][] {
  const raw_blocks = _split_on_safe_paragraph_breaks(markdown_body);
  return _merge_footnote_sections(raw_blocks);
}

function _split_on_safe_paragraph_breaks(text: string): [string, number][] {
  const counters: Record<string, number> = { '++}': 0, '--}': 0, '==}': 0, '<<}': 0 };
  const blocks: [string, number][] = [];
  let block_start = 0;
  let i = 0;
  const n = text.length;

  while (i < n) {
    let matched_open = false;
    for (const [open_tok, close_tok] of Object.entries(_CRITIC_TOKENS)) {
      if (text.startsWith(open_tok, i)) {
        counters[close_tok]++;
        i += open_tok.length;
        matched_open = true;
        break;
      }
    }
    if (matched_open) continue;

    let matched_close = false;
    for (const close_tok of Object.values(_CRITIC_TOKENS)) {
      if (text.startsWith(close_tok, i)) {
        if (counters[close_tok] > 0) counters[close_tok]--;
        i += close_tok.length;
        matched_close = true;
        break;
      }
    }
    if (matched_close) continue;

    if (text[i] === '\n' && i + 1 < n && text[i + 1] === '\n') {
      if (Object.values(counters).every(c => c === 0)) {
        const block_text = text.substring(block_start, i);
        if (block_text) blocks.push([block_text, block_start]);

        let j = i;
        while (j < n && text[j] === '\n') j++;
        i = j;
        block_start = i;
        continue;
      }
    }

    i++;
  }

  if (block_start < n) {
    const block_text = text.substring(block_start, n);
    if (block_text) blocks.push([block_text, block_start]);
  }

  return blocks;
}

function _merge_footnote_sections(blocks: [string, number][]): [string, number][] {
  if (!blocks.length) return blocks;

  const merged: [string, number][] = [];
  let i = 0;

  while (i < blocks.length) {
    const [block_text, block_offset] = blocks[i];
    const stripped = block_text.trimStart();
    const is_section_header = stripped.startsWith('## Footnotes') || stripped.startsWith('## Endnotes');

    if (!is_section_header) {
      merged.push([block_text, block_offset]);
      i++;
      continue;
    }

    let accumulated_text = block_text;
    let j = i + 1;
    while (j < blocks.length) {
      const [next_text] = blocks[j];
      const next_stripped = next_text.trimStart();
      if (next_stripped.startsWith('[^fn-') || next_stripped.startsWith('[^en-')) {
        accumulated_text = `${accumulated_text}\n\n${next_text}`;
        j++;
      } else {
        break;
      }
    }

    merged.push([accumulated_text, block_offset]);
    i = j;
  }

  return merged;
}

function _assemble_pages(block_records: [string, number][]): [string[], number[]] {
  if (!block_records.length) return [[''], [0]];

  const pages: string[] = [];
  const page_starts: number[] = [];

  let current_blocks: string[] = [];
  let current_size = 0;
  let current_start = -1;

  const flush_current = () => {
    if (current_blocks.length > 0) {
      pages.push(current_blocks.join('\n\n'));
      page_starts.push(current_start);
    }
    current_blocks = [];
    current_size = 0;
    current_start = -1;
  };

  for (const [block_text, block_offset] of block_records) {
    const block_size = block_text.length;
    const added_size = block_size + (current_blocks.length > 0 ? 2 : 0);

    if (current_blocks.length > 0 && current_size + added_size > PAGE_TARGET_CHARS) {
      flush_current();
    }

    if (current_blocks.length === 0 && block_size > PAGE_TARGET_CHARS) {
      pages.push(block_text);
      page_starts.push(block_offset);
      continue;
    }

    if (current_blocks.length === 0) current_start = block_offset;
    current_blocks.push(block_text);
    current_size += current_size > 0 ? added_size : block_size;
  }

  flush_current();

  if (!pages.length) return [[''], [0]];
  return [pages, page_starts];
}

function _count_tracked_changes(page_content: string): number {
  const matches = [...page_content.matchAll(_CHG_ID_PATTERN)];
  const distinct = new Set(matches.map(m => m[1]));
  return distinct.size;
}