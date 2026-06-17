import { DocumentObject } from './docx/bridge.js';
import { Paragraph, Table, Run, DocxEvent } from './docx/primitives.js';
import { 
  _get_style_cache, get_paragraph_prefix, is_heading_paragraph, is_native_heading, 
  get_run_style_markers, get_run_text, apply_formatting_to_segments, 
  iter_block_items, iter_document_parts, iter_paragraph_content
} from './utils/docx.js';
import { findChild } from './docx/dom.js';
import { build_structural_appendix } from './domain.js';
import { extract_comments_data } from './comments.js';

export async function extractTextFromBuffer(buffer: Buffer, cleanView = false): Promise<string> {
  const doc = await DocumentObject.load(buffer);
  return _extractTextFromDoc(doc, cleanView) as string;
}

export function _extractTextFromDoc(
  doc: DocumentObject,
  cleanView = false,
  includeAppendix = true,
  return_paragraph_offsets = false,
): string | { text: string; paragraph_offsets: Map<any, [number, number]> } {
  const comments_map = extract_comments_data(doc.pkg);

  const full_text: string[] = [];
  const paragraph_offsets = new Map<any, [number, number]>();
  let cursor = 0;

  for (const part of iter_document_parts(doc)) {
    const part_cursor = full_text.length > 0 ? cursor + 2 : cursor;
    const part_text = _extract_blocks(part, comments_map, cleanView, part_cursor, return_paragraph_offsets ? paragraph_offsets : undefined);
    if (part_text) {
      if (full_text.length > 0) cursor += 2;
      full_text.push(part_text);
      cursor += part_text.length;
    }
  }

  let base_text = full_text.join('\n\n');

  if (includeAppendix) {
    const appendix = build_structural_appendix(doc, base_text);
    if (appendix) base_text += appendix;
  }

  if (return_paragraph_offsets) {
    return { text: base_text, paragraph_offsets };
  }
  return base_text;
}

function _extract_blocks(
  container: any,
  comments_map: any,
  cleanView: boolean,
  cursor: number,
  paragraph_offsets?: Map<any, [number, number]>
): string {
  const part = container.part || container;
  const [style_cache, default_pstyle] = _get_style_cache(part);

  const blocks: string[] = [];
  let local_cursor = cursor;
  let is_first_block = true;
  let is_first_para = true;

  if (container.constructor && container.constructor.name === 'NotesPart') {
    const header = container.note_type === 'fn' ? '## Footnotes' : '## Endnotes';
    const sep = `---\n${header}`;
    blocks.push(sep);
    local_cursor += sep.length;
    is_first_block = false;
  }

  for (const item of iter_block_items(container)) {
    if (!is_first_block) local_cursor += 2;
    const block_start = local_cursor;

    if (item.constructor.name === 'FootnoteItem') {
      const fn_text = _extract_blocks(item, comments_map, cleanView, block_start, paragraph_offsets);
      if (fn_text) {
        blocks.push(fn_text);
        local_cursor = block_start + fn_text.length;
        is_first_block = false;
      } else if (!is_first_block) {
        local_cursor -= 2;
      }
    } else if (item instanceof Paragraph) {
      let prefix = get_paragraph_prefix(item, style_cache, default_pstyle);
      if (is_first_para && container.constructor.name === 'FootnoteItem') {
        prefix = `[^${container.note_type}-${container.id}]: ` + prefix;
      }
      const p_text = build_paragraph_text(item, comments_map, cleanView, style_cache, default_pstyle);
      const full_block = prefix + p_text;
      blocks.push(full_block);
      if (paragraph_offsets) {
        paragraph_offsets.set(item._element, [block_start, full_block.length]);
      }
      local_cursor = block_start + full_block.length;
      is_first_para = false;
      is_first_block = false;
    } else if (item instanceof Table) {
      const table_text = extract_table(item, comments_map, cleanView, block_start, paragraph_offsets);
      if (table_text) {
        blocks.push(table_text);
        local_cursor = block_start + table_text.length;
        is_first_block = false;
      } else if (!is_first_block) {
        local_cursor -= 2;
      }
      is_first_para = false;
    }
  }

  return blocks.join('\n\n');
}

export function extract_table(
  table: Table,
  comments_map: any,
  cleanView: boolean,
  cursor: number,
  paragraph_offsets?: Map<any, [number, number]>
): string {
  const rows_text: string[] = [];
  let rows_processed = 0;
  let local_cursor = cursor;

  for (const row of table.rows) {
    const cell_texts: string[] = [];
    const seen_cells = new Set();

    const trPr = findChild(row._element, 'w:trPr');
    const ins = trPr ? findChild(trPr, 'w:ins') : null;
    const del_node = trPr ? findChild(trPr, 'w:del') : null;

    if (cleanView && del_node) continue;

    const row_start = local_cursor + (rows_processed > 0 ? 1 : 0);
    const wrapper_prefix_len = (!cleanView && ins) ? 4 : (!cleanView && del_node) ? 4 : 0;
    
    let cell_cursor = row_start + wrapper_prefix_len;
    let first_cell = true;

    for (const cell of row.cells) {
      if (seen_cells.has(cell)) continue;
      seen_cells.add(cell);

      if (!first_cell) cell_cursor += 3;
      
      const cell_content = _extract_blocks(cell, comments_map, cleanView, cell_cursor, paragraph_offsets);
      cell_texts.push(cell_content);
      cell_cursor += cell_content.length;
      first_cell = false;
    }

    let row_str = cell_texts.join(' | ');

    if (!cleanView) {
      if (ins) row_str = `{++ ${row_str} |Chg:${ins.getAttribute('w:id')}++}`;
      else if (del_node) row_str = `{-- ${row_str} |Chg:${del_node.getAttribute('w:id')}--}`;
    }

    rows_text.push(row_str);
    local_cursor = row_start + row_str.length;
    rows_processed++;
  }

  return rows_text.join('\n');
}

export function build_paragraph_text(
  paragraph: Paragraph, comments_map: any, cleanView: boolean,
  style_cache?: any, default_pstyle?: string | null
): string {
  const parts: string[] = [];
  const active_ins: Record<string, DocxEvent> = {};
  const active_del: Record<string, DocxEvent> = {};
  const active_comments: Set<string> = new Set();
  const active_fmt: Record<string, DocxEvent> = {};
  const deferred_meta_states: any[] = [];

  let pending_text = '';
  let current_wrappers: [string, string] = ['', ''];
  let current_style: [string, string] = ['', ''];

  const items = Array.from(iter_paragraph_content(paragraph));
  const is_heading = is_heading_paragraph(paragraph, style_cache, default_pstyle);
  const native_heading = is_native_heading(paragraph, style_cache, default_pstyle);
  let leading_strip_active = is_heading;

  for (let i = 0; i < items.length; i++) {
    const item = items[i];

    if (item instanceof Run) {
      const [prefix, suffix] = get_run_style_markers(item, native_heading);
      const text = get_run_text(item);

      if (cleanView && Object.keys(active_del).length > 0) continue;

      if (leading_strip_active) {
        if (!text || !text.trim()) continue;
        leading_strip_active = false;
      }

      const seg = apply_formatting_to_segments(text, prefix, suffix);
      if (seg) {
        const new_wrappers = cleanView ? ['', ''] as [string, string] : _get_wrappers(active_ins, active_del, active_comments, active_fmt);
        const new_style: [string, string] = [prefix, suffix];

        if (pending_text && new_wrappers[0] === current_wrappers[0] && new_wrappers[1] === current_wrappers[1]) {
          if (new_style[0] === current_style[0] && new_style[1] === current_style[1] && current_style[0] !== '' && 
              pending_text.endsWith(current_style[1]) && seg.startsWith(new_style[0])) {
            pending_text = pending_text.slice(0, -current_style[1].length) + seg.slice(new_style[0].length);
          } else {
            pending_text += seg;
          }
          current_style = new_style;
        } else {
          if (pending_text) parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = seg;
          current_wrappers = new_wrappers;
          current_style = new_style;
        }

        if (!cleanView) {
          const has_meta = Object.keys(active_ins).length > 0 || Object.keys(active_del).length > 0 || active_comments.size > 0 || Object.keys(active_fmt).length > 0;
          if (has_meta) {
            deferred_meta_states.push([{...active_ins}, {...active_del}, new Set(active_comments), {...active_fmt}]);
          }

          let should_defer = false;
          const is_redline = Object.keys(active_ins).length > 0 || Object.keys(active_del).length > 0 || Object.keys(active_fmt).length > 0;

          if (is_redline) {
            let j = i + 1;
            let next_is_redline = false;
            let temp_ins = Object.keys(active_ins).length;
            let temp_del = Object.keys(active_del).length;
            let temp_fmt = Object.keys(active_fmt).length;

            while (j < items.length) {
              const next_item = items[j];
              if (next_item instanceof Run) {
                if (!get_run_text(next_item)) { j++; continue; }
                if (temp_ins > 0 || temp_del > 0 || temp_fmt > 0) next_is_redline = true;
                break;
              } else {
                const ev = next_item as DocxEvent;
                if (ev.type === 'ins_start') temp_ins++;
                else if (ev.type === 'ins_end') temp_ins = Math.max(0, temp_ins - 1);
                else if (ev.type === 'del_start') temp_del++;
                else if (ev.type === 'del_end') temp_del = Math.max(0, temp_del - 1);
                else if (ev.type === 'fmt_start') temp_fmt++;
                else if (ev.type === 'fmt_end') temp_fmt = Math.max(0, temp_fmt - 1);
              }
              j++;
            }
            if (next_is_redline) should_defer = true;
          }

          if (!should_defer && deferred_meta_states.length > 0) {
            const meta_block = _build_merged_meta_block(deferred_meta_states, comments_map);
            if (meta_block) {
              if (pending_text) {
                parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
                pending_text = '';
                current_wrappers = ['', ''];
                current_style = ['', ''];
              }
              parts.push(`{>>${meta_block}<<}`);
            }
            deferred_meta_states.length = 0; // clear
          }
        }
      }
    } else {
      const ev = item as DocxEvent;
      leading_strip_active = false;
      
      if (!['ins_start', 'ins_end', 'del_start', 'del_end', 'fmt_start', 'fmt_end'].includes(ev.type)) {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
      }

      if (ev.type === 'start') active_comments.add(ev.id);
      else if (ev.type === 'end') active_comments.delete(ev.id);
      else if (ev.type === 'ins_start') active_ins[ev.id] = ev;
      else if (ev.type === 'ins_end') delete active_ins[ev.id];
      else if (ev.type === 'del_start') active_del[ev.id] = ev;
      else if (ev.type === 'del_end') delete active_del[ev.id];
      else if (ev.type === 'fmt_start') active_fmt[ev.id] = ev;
      else if (ev.type === 'fmt_end') delete active_fmt[ev.id];
      else if (ev.type === 'footnote' || ev.type === 'endnote') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push(`[^${ev.type === 'footnote' ? 'fn' : 'en'}-${ev.id}]`);
      } else if (ev.type === 'hyperlink_start') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push('[');
      } else if (ev.type === 'hyperlink_end') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push(`](${ev.date})`);
      } else if (ev.type === 'xref_start') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push('[~');
      } else if (ev.type === 'xref_end') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push(`~](#${ev.id})`);
      } else if (ev.type === 'bookmark') {
        if (pending_text) {
          parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
          pending_text = '';
          current_wrappers = ['', ''];
          current_style = ['', ''];
        }
        parts.push(`{#${ev.id}}`);
      }
    }
  }

  if (pending_text) parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);

  if (deferred_meta_states.length > 0) {
    const meta_block = _build_merged_meta_block(deferred_meta_states, comments_map);
    if (meta_block) parts.push(`{>>${meta_block}<<}`);
  }

  return parts.join('');
}

function _get_wrappers(ins: any, del: any, comments: Set<string>, fmt: any): [string, string] {
  if (Object.keys(del).length > 0) return ['{--', '--}'];
  if (Object.keys(ins).length > 0) return ['{++', '++}'];
  if (comments.size > 0 || Object.keys(fmt).length > 0) return ['{==', '==}'];
  return ['', ''];
}

function _build_merged_meta_block(states_list: any[], comments_map: any): string {
  const change_lines: string[] = [];
  const comment_lines: string[] = [];
  const seen_sigs = new Set<string>();

  for (const [ins_map, del_map, comments_set, fmt_map] of states_list) {
    for (const [uid, meta] of Object.entries(ins_map as Record<string, DocxEvent>)) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(`[${sig} insert] ${meta.author || 'Unknown'}`);
        seen_sigs.add(sig);
      }
    }
    for (const [uid, meta] of Object.entries(del_map as Record<string, DocxEvent>)) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(`[${sig} delete] ${meta.author || 'Unknown'}`);
        seen_sigs.add(sig);
      }
    }
    for (const [uid, meta] of Object.entries(fmt_map as Record<string, DocxEvent>)) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(`[${sig} format] ${meta.author || 'Unknown'}`);
        seen_sigs.add(sig);
      }
    }

    // Threaded Comment Resolution Tree
    const children_map: Record<string, string[]> = {};
    for (const [c_id, data] of Object.entries(comments_map as Record<string, any>)) {
      const p_id = data.parent_id;
      if (p_id) {
        if (!children_map[p_id]) children_map[p_id] = [];
        children_map[p_id].push(c_id);
      }
    }

    function render_comment(cid: string) {
      if (!comments_map[cid]) return;
      const sig = `Com:${cid}`;
      if (seen_sigs.has(sig)) return;

      const data = comments_map[cid];
      let header = `[${sig}] ${data.author}`;
      if (data.date) header += ` @ ${data.date}`;
      if (data.resolved) header += `(RESOLVED)`;
      comment_lines.push(`${header}: ${data.text}`);
      seen_sigs.add(sig);

      if (children_map[cid]) {
        const children = children_map[cid].sort((a, b) => (comments_map[a]?.date || '').localeCompare(comments_map[b]?.date || ''));
        for (const child_id of children) {
          render_comment(child_id);
        }
      }
    }

    const sorted_ids = Array.from(comments_set as Set<string>).sort();
    for (const c_id of sorted_ids) {
      render_comment(c_id);
    }
  }

  return [...change_lines, ...comment_lines].join('\n');
}