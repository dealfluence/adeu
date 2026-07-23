/**
 * Structural outline extractor.
 */

import { DocumentObject } from "./docx/bridge.js";
import { Paragraph, Table, DocxEvent } from "./docx/primitives.js";
import { build_paragraph_text, extract_table } from "./ingest.js";
import { extract_comments_data } from "./comments.js";
import { findChild } from "./docx/dom.js";
import {
  _get_style_cache,
  get_paragraph_prefix,
  iter_block_items,
  iter_document_parts,
  iter_paragraph_content,
} from "./utils/docx.js";

const _HEADING_PREFIX_RE = /^(#{1,6}) /;
const _HEURISTIC_MIN_WORDS = 3;

export interface OutlineNode {
  level: number;
  text: string;
  page: number;
  style: string;
  has_table: boolean;
  footnote_ids: string[];
}

interface _BlockRecord {
  item: any;
  is_paragraph: boolean;
  is_table: boolean;
  start_offset: number;
  projected_length: number;
}

export function extract_outline(
  doc: DocumentObject,
  projected_body: string,
  body_pages: string[],
  body_page_offsets: number[],
  paragraph_offsets: Record<string, [number, number]> | Map<any, [number, number]> | null = null,
): OutlineNode[] {
  if (body_pages.length !== body_page_offsets.length) {
    throw new Error("body_pages and body_page_offsets length mismatch");
  }

  if (paragraph_offsets) {
    return _extract_outline_fast(doc, projected_body, body_page_offsets, paragraph_offsets);
  }

  const comments_map = extract_comments_data(doc.pkg);
  const block_records = _walk_doc_body(doc, comments_map);

  const heading_indices: number[] = [];
  for (let idx = 0; idx < block_records.length; idx++) {
    const rec = block_records[idx];
    if (!(rec.is_paragraph && _is_heading(rec.item))) continue;
    if (!_heading_passes_quality_filter(rec.item, comments_map)) continue;
    heading_indices.push(idx);
  }

  if (heading_indices.length === 0) return [];

  const nodes: OutlineNode[] = [];
  for (let h_pos = 0; h_pos < heading_indices.length; h_pos++) {
    const rec_idx = heading_indices[h_pos];
    const rec = block_records[rec_idx];
    const paragraph = rec.item as Paragraph;

    const level = _heading_level(paragraph);
    const text = _heading_text(paragraph, comments_map);
    const style = _determine_heading_style(paragraph);

    const owned_end = _find_owned_end(
      block_records,
      heading_indices,
      h_pos,
      level,
    );
    const owned_blocks = block_records.slice(rec_idx + 1, owned_end);

    const has_table = _direct_has_table(block_records, rec_idx + 1, owned_end);
    const footnote_ids = _collect_footnote_ids(owned_blocks);

    const page_num = _offset_to_page(rec.start_offset, body_page_offsets);

    nodes.push({ level, text, page: page_num, style, has_table, footnote_ids });
  }

  return nodes;
}

function _direct_has_table(
  block_records: _BlockRecord[],
  range_start: number,
  range_end: number,
): boolean {
  for (let idx = range_start; idx < range_end; idx++) {
    const rec = block_records[idx];
    if (rec.is_paragraph && _is_heading(rec.item)) return false;
    if (rec.is_table) return true;
  }
  return false;
}

function _walk_doc_body(
  doc: DocumentObject,
  comments_map: any,
): _BlockRecord[] {
  const parts = Array.from(iter_document_parts(doc));
  let body_start_offset = 0;
  let body_part: any = null;

  for (const part of parts) {
    if (part === doc) {
      body_part = part;
      break;
    }
    const part_text = _project_part(part, comments_map);
    if (part_text) {
      if (body_start_offset > 0) body_start_offset += 2;
      body_start_offset += part_text.length;
    }
  }

  if (!body_part) {
    body_part = doc;
    body_start_offset = 0;
  } else {
    if (body_start_offset > 0) body_start_offset += 2;
  }

  const records: _BlockRecord[] = [];
  let cursor = body_start_offset;
  let is_first_block = true;

  for (const item of iter_block_items(body_part)) {
    if (item instanceof Paragraph) {
      const prefix = get_paragraph_prefix(item);
      const p_text = build_paragraph_text(item, comments_map, false);
      const block_len = (prefix + p_text).length;

      if (!is_first_block) cursor += 2;

      records.push({
        item,
        is_paragraph: true,
        is_table: false,
        start_offset: cursor,
        projected_length: block_len,
      });
      cursor += block_len;
      is_first_block = false;
    } else if (item instanceof Table) {
      const table_text = extract_table(item, comments_map, false, 0);
      const block_len = table_text ? table_text.length : 0;

      if (!is_first_block) cursor += 2;

      const table_start = cursor;
      records.push({
        item,
        is_paragraph: false,
        is_table: true,
        start_offset: table_start,
        projected_length: block_len,
      });
      _record_table_inner_blocks_lite(item, table_start, records, comments_map);
      cursor += block_len;
      is_first_block = false;
    }
  }

  return records;
}

function _compute_inner_block_offset(
  table: Table,
  target_paragraph: Paragraph,
  table_start_offset: number,
  comments_map: any,
): number {
  const target_el = target_paragraph._element;
  let cursor = table_start_offset;
  let rows_processed = 0;

  for (const row of table.rows) {
    if (rows_processed > 0) {
      if (rows_processed === 1) {
        const first_row = table.rows[0];
        const seen_cells_first = new Set();
        let num_cols = 0;
        for (const cell of first_row.cells) {
          if (seen_cells_first.has(cell)) continue;
          seen_cells_first.add(cell);
          num_cols += 1;
        }
        const divider_len = num_cols > 0 ? Array(num_cols).fill("---").join(" | ").length : 0;
        cursor += 1 + divider_len + 1;
      } else {
        cursor += 1;
      }
    }

    const seen_cells = new Set();
    let cells_in_row = 0;

    for (const cell of row.cells) {
      if (seen_cells.has(cell)) continue;
      seen_cells.add(cell);

      if (cells_in_row > 0) cursor += 3;

      const [new_cursor, found] = _walk_cell_for_offset(
        cell,
        target_el,
        cursor,
        comments_map,
      );
      if (found) return new_cursor;
      cursor = new_cursor;

      cells_in_row++;
    }
    rows_processed++;
  }

  return table_start_offset;
}

function _walk_cell_for_offset(
  cell: any,
  target_el: any,
  cell_start_cursor: number,
  comments_map: any,
): [number, boolean] {
  let cursor = cell_start_cursor;
  let is_first_block = true;

  for (const inner_item of iter_block_items(cell)) {
    if (!is_first_block) cursor += 2;

    if (inner_item instanceof Paragraph) {
      if (inner_item._element === target_el) return [cursor, true];
      const prefix = get_paragraph_prefix(inner_item);
      const p_text = build_paragraph_text(inner_item, comments_map, false);
      cursor += (prefix + p_text).length;
    } else if (inner_item instanceof Table) {
      const nested_offset = _compute_inner_block_offset(
        inner_item,
        new Paragraph(target_el, null),
        cursor,
        comments_map,
      );
      if (nested_offset !== cursor) {
        if (_element_is_descendant(target_el, inner_item._element))
          return [nested_offset, true];
      }
      const table_text = extract_table(inner_item, comments_map, false, 0);
      cursor += table_text ? table_text.length : 0;
    }
    is_first_block = false;
  }
  return [cursor, false];
}

function _element_is_descendant(
  target_el: Element,
  ancestor_el: Element,
): boolean {
  let cur: Node | null = target_el.parentNode;
  while (cur) {
    if (cur === ancestor_el) return true;
    cur = cur.parentNode;
  }
  return false;
}

function _record_table_inner_blocks_lite(
  table: Table,
  inherited_offset: number,
  records: _BlockRecord[],
  comments_map: any,
) {
  const seen_cells = new Set();
  for (const row of table.rows) {
    for (const cell of row.cells) {
      if (seen_cells.has(cell)) continue;
      seen_cells.add(cell);

      for (const inner_item of iter_block_items(cell)) {
        if (inner_item instanceof Paragraph) {
          const true_offset = _is_heading(inner_item)
            ? _compute_inner_block_offset(
                table,
                inner_item,
                inherited_offset,
                comments_map,
              )
            : inherited_offset;
          records.push({
            item: inner_item,
            is_paragraph: true,
            is_table: false,
            start_offset: true_offset,
            projected_length: 0,
          });
        } else if (inner_item instanceof Table) {
          records.push({
            item: inner_item,
            is_paragraph: false,
            is_table: true,
            start_offset: inherited_offset,
            projected_length: 0,
          });
          _record_table_inner_blocks_lite(
            inner_item,
            inherited_offset,
            records,
            comments_map,
          );
        }
      }
    }
  }
}

function _project_part(part: any, comments_map: any): string {
  const blocks: string[] = [];
  const c_type = part.constructor.name;

  if (c_type === "NotesPart") {
    const header = part.note_type === "fn" ? "## Footnotes" : "## Endnotes";
    blocks.push(`---\n${header}`);
  }

  let is_first_para = true;
  for (const item of iter_block_items(part)) {
    if (item.constructor.name === "FootnoteItem") {
      const fn_text = _project_part(item, comments_map);
      if (fn_text) blocks.push(fn_text);
    } else if (item instanceof Paragraph) {
      let prefix = get_paragraph_prefix(item);
      if (is_first_para && c_type === "FootnoteItem")
        prefix = `[^${part.note_type}-${part.id}]: ${prefix}`;
      const p_text = build_paragraph_text(item, comments_map, false);
      blocks.push(prefix + p_text);
      is_first_para = false;
    } else if (item instanceof Table) {
      const table_text = extract_table(item, comments_map, false, 0);
      if (table_text) blocks.push(table_text);
      is_first_para = false;
    }
  }

  return blocks.join("\n\n");
}

function _is_heading(paragraph: Paragraph): boolean {
  return _HEADING_PREFIX_RE.test(get_paragraph_prefix(paragraph));
}

function _heading_passes_quality_filter(
  paragraph: Paragraph,
  comments_map: any,
): boolean {
  const style = _determine_heading_style(paragraph);
  if (style !== "(heuristic)") return true;
  const text = _heading_text(paragraph, comments_map);
  if (!text) return false;
  const word_count = (text.match(/\w+/g) || []).length;
  return word_count >= _HEURISTIC_MIN_WORDS;
}

function _heading_level(paragraph: Paragraph): number {
  const match = _HEADING_PREFIX_RE.exec(get_paragraph_prefix(paragraph));
  return match ? Math.min(match[1].length, 6) : 1;
}

function _heading_text(paragraph: Paragraph, comments_map: any): string {
  const p_text = build_paragraph_text(paragraph, comments_map, false);
  let cleaned = _strip_critic_markup(p_text);
  cleaned = _strip_inline_formatting(cleaned);
  return cleaned.trim();
}

function _strip_critic_markup(text: string): string {
  if (!text) return "";
  text = text.replace(/\{--[\s\S]*?--\}/g, "");
  text = text.replace(/\{>>[\s\S]*?<<\}/g, "");
  text = text.replace(/\{\+\+([\s\S]*?)\+\+\}/g, "$1");
  text = text.replace(/\{==([\s\S]*?)==\}/g, "$1");
  return text;
}

function _strip_inline_formatting(text: string): string {
  if (!text) return "";
  text = text.replace(/\*\*(.+?)\*\*/g, "$1");
  text = text.replace(/__(.+?)__/g, "$1");
  text = text.replace(/(?<!\w)_(\S(?:.*?\S)?)_(?!\w)/g, "$1");
  return text;
}

function _determine_heading_style(paragraph: Paragraph): string {
  const [style_cache, default_pstyle] = _get_style_cache(
    paragraph._parent.part || paragraph._parent,
  );
  const pPr = findChild(paragraph._element, "w:pPr");
  let style_id = default_pstyle;

  if (pPr) {
    const pStyle = findChild(pPr, "w:pStyle");
    if (pStyle) style_id = pStyle.getAttribute("w:val") || default_pstyle;
  }

  let outline_level: number | null = null;
  let outline_level_from_style = false;
  if (pPr) {
    const oLvl = findChild(pPr, "w:outlineLvl");
    if (oLvl && /^\d+$/.test(oLvl.getAttribute("w:val") || "")) {
      outline_level = parseInt(oLvl.getAttribute("w:val") as string, 10);
    }
  }
  
  if (outline_level === null && style_id && style_cache && style_cache[style_id]) {
    outline_level = style_cache[style_id].outline_level;
    outline_level_from_style = true;
  }

  const style_name =
    style_id && style_cache && style_cache[style_id]
      ? style_cache[style_id].name
      : style_id;

  let normalized_style_name = style_name;
  if (normalized_style_name && typeof normalized_style_name === "string") {
    if (normalized_style_name.toLowerCase().startsWith("heading")) {
      normalized_style_name = normalized_style_name.replace(/^heading/i, "Heading");
    } else if (normalized_style_name.toLowerCase() === "title") {
      normalized_style_name = "Title";
    }
  }

  if (outline_level_from_style && outline_level !== null) {
    const is_heading_or_title =
      normalized_style_name &&
      (normalized_style_name.startsWith("Heading") || normalized_style_name === "Title");
    if (!is_heading_or_title) {
      outline_level = null;
    }
  }

  if (outline_level !== null && outline_level >= 0 && outline_level <= 8) {
    if (normalized_style_name && (normalized_style_name.startsWith("Heading") || normalized_style_name === "Title")) {
      return normalized_style_name;
    }
    return "(outline_level)";
  }

  if (
    normalized_style_name &&
    (normalized_style_name.startsWith("Heading") || normalized_style_name === "Title")
  )
    return normalized_style_name;

  if (normalized_style_name && /Heading[ ]?([1-6])(?![0-9])/.test(normalized_style_name))
    return normalized_style_name;

  return "(heuristic)";
}

function _safe_style_name(
  paragraph: Paragraph,
  style_cache: any,
  default_pstyle: any,
): string | null {
  const pPr = findChild(paragraph._element, "w:pPr");
  let style_id = default_pstyle;
  if (pPr) {
    const pStyle = findChild(pPr, "w:pStyle");
    if (pStyle) style_id = pStyle.getAttribute("w:val") || default_pstyle;
  }
  return style_id && style_cache && style_cache[style_id]
    ? style_cache[style_id].name
    : style_id;
}

function _find_owned_end(
  block_records: _BlockRecord[],
  heading_indices: number[],
  current_h_pos: number,
  current_level: number,
): number {
  for (
    let next_h_pos = current_h_pos + 1;
    next_h_pos < heading_indices.length;
    next_h_pos++
  ) {
    const next_idx = heading_indices[next_h_pos];
    if (_heading_level(block_records[next_idx].item) <= current_level)
      return next_idx;
  }
  return block_records.length;
}

function _collect_footnote_ids(owned_blocks: _BlockRecord[]): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const rec of owned_blocks) {
    if (!rec.is_paragraph) continue;
    for (const event of iter_paragraph_content(rec.item)) {
      if (!("type" in event)) continue;
      let fn_id = "";
      if (event.type === "footnote") fn_id = `fn-${event.id}`;
      else if (event.type === "endnote") fn_id = `en-${event.id}`;
      else continue;

      if (!seen.has(fn_id)) {
        seen.add(fn_id);
        ordered.push(fn_id);
      }
    }
  }
  return ordered;
}

function _offset_to_page(offset: number, body_page_offsets: number[]): number {
  if (!body_page_offsets || body_page_offsets.length === 0) return 1;
  let page = 1;
  for (let i = 0; i < body_page_offsets.length; i++) {
    if (offset >= body_page_offsets[i]) page = i + 1;
    else break;
  }
  return page;
}

function _extract_outline_fast(
  doc: DocumentObject,
  projected_body: string,
  body_page_offsets: number[],
  paragraph_offsets: Map<any, [number, number]> | Record<string, [number, number]>,
): OutlineNode[] {
  const paragraphs_and_tables: ["p" | "t", any][] = [];
  const seen_cells = new Set<any>();

  function walk(container: any) {
    for (const item of iter_block_items(container)) {
      const i_type = item.constructor.name;
      if (i_type === "FootnoteItem") {
        walk(item);
      } else if (item instanceof Paragraph) {
        paragraphs_and_tables.push(["p", item]);
      } else if (item instanceof Table) {
        paragraphs_and_tables.push(["t", item]);
        for (const row of item.rows) {
          for (const cell of row.cells) {
            if (seen_cells.has(cell._element)) {
              continue;
            }
            seen_cells.add(cell._element);
            walk(cell);
          }
        }
      }
    }
  }

  walk(doc);

  const heading_indices: number[] = [];
  for (let idx = 0; idx < paragraphs_and_tables.length; idx++) {
    const [kind, item] = paragraphs_and_tables[idx];
    if (kind !== "p") continue;

    let hasOffset = false;
    if (paragraph_offsets instanceof Map) {
      hasOffset = paragraph_offsets.has(item._element);
    } else {
      hasOffset = item._element in (paragraph_offsets as any);
    }
    if (!hasOffset) {
      continue;
    }

    if (!_is_heading(item)) continue;
    if (!_heading_passes_quality_filter_fast(item, projected_body, paragraph_offsets)) continue;

    heading_indices.push(idx);
  }

  if (heading_indices.length === 0) return [];

  const nodes: OutlineNode[] = [];
  for (let h_pos = 0; h_pos < heading_indices.length; h_pos++) {
    const item_idx = heading_indices[h_pos];
    const paragraph = paragraphs_and_tables[item_idx][1] as Paragraph;
    const level = _heading_level(paragraph);
    const text = _heading_text_fast(paragraph, projected_body, paragraph_offsets);
    const style = _determine_heading_style(paragraph);

    // Owned range: items strictly between this heading and the next equal-or-higher heading.
    let owned_end = item_idx;
    for (let next_h_pos = h_pos + 1; next_h_pos < heading_indices.length; next_h_pos++) {
      const next_idx = heading_indices[next_h_pos];
      const next_paragraph = paragraphs_and_tables[next_idx][1] as Paragraph;
      if (_heading_level(next_paragraph) <= level) {
        owned_end = next_idx;
        break;
      }
    }
    if (owned_end === item_idx) {
      owned_end = paragraphs_and_tables.length;
    }

    const owned = paragraphs_and_tables.slice(item_idx + 1, owned_end);

    // has_table: nearest-claim semantics (no bubbling to ancestors).
    let has_table = false;
    for (const [kind2, item2] of owned) {
      if (kind2 === "p" && _is_heading(item2)) {
        break;
      }
      if (kind2 === "t") {
        has_table = true;
        break;
      }
    }

    // Footnote IDs in document order, deduped.
    const footnote_ids = _collect_footnote_ids_fast(owned);

    // Page resolution from the paragraph's known offset.
    let para_offset: [number, number] | undefined;
    if (paragraph_offsets instanceof Map) {
      para_offset = paragraph_offsets.get(paragraph._element);
    } else {
      para_offset = paragraph_offsets[paragraph._element as any];
    }

    let page_num = 1;
    if (para_offset !== undefined) {
      const [start_offset] = para_offset;
      page_num = _offset_to_page(start_offset, body_page_offsets);
    }

    nodes.push({
      level,
      text,
      page: page_num,
      style,
      has_table,
      footnote_ids,
    });
  }

  return nodes;
}

function _heading_passes_quality_filter_fast(
  paragraph: Paragraph,
  projected_body: string,
  paragraph_offsets: Map<any, [number, number]> | Record<string, [number, number]>,
): boolean {
  const style = _determine_heading_style(paragraph);
  if (style !== "(heuristic)") return true;

  const text = _heading_text_fast(paragraph, projected_body, paragraph_offsets);
  if (!text) return false;
  const words = text.match(/\w+/g) || [];
  return words.length >= _HEURISTIC_MIN_WORDS;
}

function _heading_text_fast(
  paragraph: Paragraph,
  projected_body: string,
  paragraph_offsets: Map<any, [number, number]> | Record<string, [number, number]>,
): string {
  let offset: [number, number] | undefined;
  if (paragraph_offsets instanceof Map) {
    offset = paragraph_offsets.get(paragraph._element);
  } else {
    offset = paragraph_offsets[paragraph._element as any];
  }

  if (offset === undefined) {
    return "";
  }
  const [start, length] = offset;
  const raw = projected_body.substring(start, start + length);
  let cleaned = _strip_critic_markup(raw);
  cleaned = _strip_inline_formatting(cleaned);
  cleaned = cleaned.replace(/^#+\s+/, "");
  return cleaned.trim();
}

function _collect_footnote_ids_fast(owned_items: ["p" | "t", any][]): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const [kind, item] of owned_items) {
    if (kind !== "p") continue;
    for (const event of iter_paragraph_content(item)) {
      if (!("type" in event)) continue;
      let fn_id = "";
      if (event.type === "footnote") fn_id = `fn-${event.id}`;
      else if (event.type === "endnote") fn_id = `en-${event.id}`;
      else continue;

      if (!seen.has(fn_id)) {
        seen.add(fn_id);
        ordered.push(fn_id);
      }
    }
  }
  return ordered;
}
