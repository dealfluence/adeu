import { DocumentObject } from "./docx/bridge.js";
import { Paragraph, Table, Run, DocxEvent } from "./docx/primitives.js";
import { findAllDescendants, findChild } from "./docx/dom.js";
import { extract_comments_data } from "./comments.js";
import { RegexTimeoutError, userFindAllMatches, userSearch } from "./utils/safe-regex.js";
import {
  _get_style_cache,
  get_paragraph_prefix,
  get_run_style_markers,
  get_run_text,
  is_heading_paragraph,
  is_native_heading,
  iter_block_items,
  iter_document_parts,
  iter_paragraph_content,
} from "./utils/docx.js";

export interface TextSpan {
  start: number;
  end: number;
  text: string;
  run: Run | null;
  paragraph: Paragraph | null;
  ins_id?: string | null;
  del_id?: string | null;
  hyperlink_id?: string | null;
  comment_ids?: string[];
}

export function renumber_snapshot_ids(
  doc: DocumentObject,
): [Record<string, string>, Record<string, string>] {
  const chg_remap: Record<string, string> = {};
  let next_chg = 1;
  const body_root = doc.element;

  const chg_elements: Element[] = [];
  const all_elements = findAllDescendants(body_root, "*");
  for (const el of all_elements) {
    if (el.tagName === "w:ins" || el.tagName === "w:del") {
      chg_elements.push(el);
    }
  }

  for (const elem of chg_elements) {
    const old_id = elem.getAttribute("w:id");
    if (!old_id) continue;
    if (chg_remap[old_id]) {
      elem.setAttribute("w:id", chg_remap[old_id]);
      continue;
    }
    const new_id = next_chg.toString();
    chg_remap[old_id] = new_id;
    elem.setAttribute("w:id", new_id);
    next_chg++;
  }

  const com_remap: Record<string, string> = {};
  let next_com = 1;
  const comments_part = doc.pkg.parts.find(
    (p) =>
      p.contentType ===
      "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
  );

  if (comments_part) {
    const comments_root = comments_part._element;
    for (const c of findAllDescendants(comments_root, "w:comment")) {
      const old_id = c.getAttribute("w:id");
      if (!old_id) continue;
      if (com_remap[old_id]) {
        c.setAttribute("w:id", com_remap[old_id]);
        continue;
      }
      const new_id = next_com.toString();
      com_remap[old_id] = new_id;
      c.setAttribute("w:id", new_id);
      next_com++;
    }
  }

  for (const elem of all_elements) {
    if (
      [
        "w:commentReference",
        "w:commentRangeStart",
        "w:commentRangeEnd",
      ].includes(elem.tagName)
    ) {
      const old_id = elem.getAttribute("w:id");
      if (old_id && com_remap[old_id]) {
        elem.setAttribute("w:id", com_remap[old_id]);
      }
    }
  }

  if (comments_part) {
    for (const c of findAllDescendants(comments_part._element, "w:comment")) {
      const parent_id = c.getAttribute("w15:p");
      if (parent_id && com_remap[parent_id]) {
        c.setAttribute("w15:p", com_remap[parent_id]);
      }
    }
  }

  return [chg_remap, com_remap];
}

// Markdown style delimiters the projection emits as VIRTUAL spans around
// formatted runs (see get_run_style_markers). Literal asterisks/underscores
// typed in the document live inside real (run-backed) spans and are never
// confused with these.
const STYLE_MARKER_TEXTS = new Set(["**", "__", "*", "_"]);

export class DocumentMapper {
  public doc: DocumentObject;
  public clean_view: boolean;
  public original_view: boolean;
  public comments_map: Record<string, any>;
  public full_text: string = "";
  public spans: TextSpan[] = [];
  public appendix_start_index: number = -1;
  private _text_chunks: string[] = [];
  private _plain_projection: [string, number[]] | null = null;

  constructor(
    doc: DocumentObject,
    clean_view: boolean = false,
    original_view: boolean = false,
  ) {
    this.doc = doc;
    this.clean_view = clean_view;
    this.original_view = original_view;
    this.comments_map = extract_comments_data(doc.pkg);
    this._build_map();
  }

  private _build_map() {
    let current_offset = 0;
    this.spans = [];
    this._text_chunks = [];
    this.full_text = "";
    this._plain_projection = null;

    for (const part of iter_document_parts(this.doc)) {
      current_offset = this._map_blocks(part, current_offset);

      if (
        this.spans.length > 0 &&
        this.spans[this.spans.length - 1].text !== "\n\n"
      ) {
        this._add_virtual_text("\n\n", current_offset, null);
        current_offset += 2;
      }
    }

    while (
      this.spans.length > 0 &&
      this.spans[this.spans.length - 1].text === "\n\n"
    ) {
      this.spans.pop();
      this._text_chunks.pop();
    }

    this.full_text = this._text_chunks.join("");
    this.appendix_start_index = -1;
  }

  private _map_blocks(container: any, offset: number): number {
    let current = offset;
    const c_type = container.constructor.name;
    const part = container.part || container;
    const [style_cache, default_pstyle] = _get_style_cache(part);

    if (c_type === "NotesPart") {
      const header =
        container.note_type === "fn" ? "## Footnotes" : "## Endnotes";
      const sep = `---\n${header}`;
      this._add_virtual_text(sep, current, null);
      current += sep.length;
      this._add_virtual_text("\n\n", current, null);
      current += 2;
    }

    let is_first_para = true;
    let previous_item: any = null;

    for (const item of iter_block_items(container)) {
      const i_type = item.constructor.name;

      if (i_type === "FootnoteItem") {
        current = this._map_blocks(item, current);
      } else if (item instanceof Paragraph) {
        if (!is_first_para) {
          const prev_para =
            previous_item instanceof Paragraph ? previous_item : null;
          this._add_virtual_text("\n\n", current, prev_para);
          current += 2;
        }

        let prefix = get_paragraph_prefix(item, style_cache, default_pstyle);
        if (is_first_para && c_type === "FootnoteItem") {
          prefix = `[^${container.note_type}-${container.id}]: ` + prefix;
        }
        if (prefix) {
          this._add_virtual_text(prefix, current, item);
          current += prefix.length;
        }

        current = this._map_paragraph_content(
          item,
          current,
          style_cache,
          default_pstyle,
        );
        is_first_para = false;
        previous_item = item;
      } else if (item instanceof Table) {
        if (!is_first_para) {
          const prev_para =
            previous_item instanceof Paragraph ? previous_item : null;
          this._add_virtual_text("\n\n", current, prev_para);
          current += 2;
        }
        current = this._map_table(item, current);
        is_first_para = false;
        previous_item = item;
      }
    }

    return current;
  }

  private _map_table(table: Table, offset: number): number {
    let current = offset;
    let rows_processed = 0;

    for (const row of table.rows) {
      const tr = row._element;
      const trPr = findChild(tr, "w:trPr");
      const ins = trPr ? findChild(trPr, "w:ins") : null;
      const del_node = trPr ? findChild(trPr, "w:del") : null;

      if (this.clean_view && del_node) continue;
      if (this.original_view && ins) continue;

      if (rows_processed > 0) {
        this._add_virtual_text("\n", current, null);
        current += 1;
      }

      if (ins && !this.clean_view && !this.original_view) {
        this._add_virtual_text("{++ ", current, null);
        current += 4;
      } else if (del_node && !this.clean_view && !this.original_view) {
        this._add_virtual_text("{-- ", current, null);
        current += 4;
      }

      const seen_cells = new Set();
      let cells_processed = 0;

      for (const cell of row.cells) {
        if (seen_cells.has(cell)) continue;
        seen_cells.add(cell);

        if (cells_processed > 0) {
          this._add_virtual_text(" | ", current, null);
          current += 3;
        }

        const cell_start = current;
        current = this._map_blocks(cell, current);

        // Parity with ingest.extract_table: emit a {#cell:<paraId>} anchor and
        // bind a zero-width span to the cell's first paragraph so the engine
        // can resolve "write into this cell" even when the cell is empty
        // (pPr-only paragraph with no run).
        if (!this.clean_view && !this.original_view) {
          const firstP = cell._element.getElementsByTagName("w:p")[0] as
            | Element
            | undefined;
          const paraId = firstP ? firstP.getAttribute("w14:paraId") : null;
          if (paraId && firstP) {
            // Zero-width span bound to the empty cell paragraph: gives
            // get_insertion_anchor a paragraph to land on. Placed at the anchor
            // token offset so resolution targets THIS cell, not a neighbour.
            const cellPara = new Paragraph(firstP, cell);
            this._add_virtual_text("", current, cellPara);
            const anchor = `{#cell:${paraId}}`;
            this._add_virtual_text(anchor, current, cellPara);
            current += anchor.length;
          }
        }
        cells_processed += 1;
      }

      if (ins && !this.clean_view && !this.original_view) {
        const suffix = ` |Chg:${ins.getAttribute("w:id")}++}`;
        this._add_virtual_text(suffix, current, null);
        current += suffix.length;
      } else if (del_node && !this.clean_view && !this.original_view) {
        const suffix = ` |Chg:${del_node.getAttribute("w:id")}--}`;
        this._add_virtual_text(suffix, current, null);
        current += suffix.length;
      }

      rows_processed += 1;
    }

    return current;
  }

  private _strip_markdown_formatting(text: string): string {
    let result = text;
    result = result.replace(/^#+\s*/gm, "");
    result = result.replace(/\*\*(\w[\w\s]*\w|\w{2,})\*\*/g, "$1");
    result = result.replace(/__(\w[\w\s]*\w|\w{2,})__/g, "$1");
    result = result.replace(/(?<!\w)_(\w[\w\s]*\w|\w{2,})_(?!\w)/g, "$1");
    result = result.replace(/(?<!\w)\*(\w[\w\s]*\w|\w{2,})\*(?!\w)/g, "$1");
    return result;
  }

  private _map_paragraph_content(
    paragraph: Paragraph,
    start_offset: number,
    style_cache?: any,
    default_pstyle?: string | null,
  ): number {
    let current = start_offset;

    const span: TextSpan = {
      start: current,
      end: current,
      text: "",
      run: null,
      paragraph,
    };
    this.spans.push(span);

    const active_ids = new Set<string>();
    const active_ins: Record<string, DocxEvent> = {};
    const active_del: Record<string, DocxEvent> = {};
    const active_fmt: Record<string, DocxEvent> = {};

    let deferred_meta_states: any[] = [];
    let current_wrappers: [string, string] = ["", ""];
    let current_style: [string, string] = ["", ""];
    let active_hyperlink_id: string | null = null;
    let pending_runs: [
      string,
      string,
      Run | null,
      string | null,
      string | null,
      string[],
    ][] = [];

    const flush_pending_runs = () => {
      if (pending_runs.length === 0) return;
      const [s_tok, e_tok] = current_wrappers;
      if (s_tok) {
        this._add_virtual_text(s_tok, current, paragraph);
        current += s_tok.length;
      }
      for (const [kind, txt, r_obj, i_id, d_id, c_ids] of pending_runs) {
        if (kind === "virtual") {
          this._add_virtual_text(txt, current, paragraph, active_hyperlink_id);
        } else {
          const s: TextSpan = {
            start: current,
            end: current + txt.length,
            text: txt,
            run: r_obj,
            paragraph,
            ins_id: i_id || undefined,
            del_id: d_id || undefined,
            hyperlink_id: active_hyperlink_id || undefined,
            comment_ids: c_ids.length > 0 ? c_ids : undefined,
          };
          this.spans.push(s);
          this._text_chunks.push(txt);
        }
        current += txt.length;
      }
      if (e_tok) {
        this._add_virtual_text(e_tok, current, paragraph);
        current += e_tok.length;
      }
      pending_runs = [];
    };

    const items = Array.from(iter_paragraph_content(paragraph));
    const is_heading = is_heading_paragraph(
      paragraph,
      style_cache,
      default_pstyle,
    );
    const native_heading = is_native_heading(
      paragraph,
      style_cache,
      default_pstyle,
    );
    let leading_strip_active = is_heading;

    for (let i = 0; i < items.length; i++) {
      const item = items[i];

      if (item instanceof Run) {
        const [prefix, suffix] = get_run_style_markers(item, native_heading);
        const run_parts: [string, string, Run | null][] = [];
        const text = get_run_text(item);

        if (leading_strip_active) {
          if (text === "" || /^\s*$/.test(text)) continue;
          leading_strip_active = false;
        }

        if (text.includes("\n") && (prefix || suffix)) {
          const parts = text.split("\n");
          for (let idx = 0; idx < parts.length; idx++) {
            if (idx > 0) run_parts.push(["real", "\n", item]);
            if (parts[idx]) {
              if (prefix) run_parts.push(["virtual", prefix, null]);
              run_parts.push(["real", parts[idx], item]);
              if (suffix) run_parts.push(["virtual", suffix, null]);
            }
          }
        } else {
          if (prefix) run_parts.push(["virtual", prefix, null]);
          if (text) run_parts.push(["real", text, item]);
          if (suffix) run_parts.push(["virtual", suffix, null]);
        }

        if (this.clean_view && Object.keys(active_del).length > 0) {
          // pass
        }
        if (this.original_view && Object.keys(active_ins).length > 0) {
          // pass
        }

        const full_seg_text = run_parts.map((x) => x[1]).join("");
        const curr_ins_id = Object.keys(active_ins).pop() || null;
        const curr_del_id = Object.keys(active_del).pop() || null;

        if (
          full_seg_text &&
          !(this.clean_view && curr_del_id) &&
          !(this.original_view && curr_ins_id)
        ) {
          const new_wrappers =
            this.clean_view || this.original_view
              ? (["", ""] as [string, string])
              : this._get_wrappers(
                  curr_ins_id,
                  curr_del_id,
                  active_ids,
                  active_fmt,
                );
          const new_style: [string, string] = [prefix, suffix];

          if (
            pending_runs.length > 0 &&
            new_wrappers[0] === current_wrappers[0] &&
            new_wrappers[1] === current_wrappers[1]
          ) {
            let skip_leading_prefix = false;
            if (
              new_style[0] === current_style[0] &&
              new_style[1] === current_style[1] &&
              current_style[0] !== "" &&
              pending_runs[pending_runs.length - 1][0] === "virtual" &&
              pending_runs[pending_runs.length - 1][1] === current_style[1]
            ) {
              pending_runs.pop();
              skip_leading_prefix = true;
            }

            const curr_comment_ids = Array.from(active_ids);
            for (const [kind, txt, r_obj] of run_parts) {
              if (
                skip_leading_prefix &&
                kind === "virtual" &&
                txt === new_style[0]
              ) {
                skip_leading_prefix = false;
                continue;
              }
              pending_runs.push([
                kind,
                txt,
                r_obj,
                curr_ins_id,
                curr_del_id,
                curr_comment_ids,
              ]);
            }
            current_style = new_style;
          } else {
            flush_pending_runs();
            current_wrappers = new_wrappers;
            current_style = new_style;
            const curr_comment_ids = Array.from(active_ids);
            for (const [kind, txt, r_obj] of run_parts) {
              pending_runs.push([
                kind,
                txt,
                r_obj,
                curr_ins_id,
                curr_del_id,
                curr_comment_ids,
              ]);
            }
          }
        }

        if (!this.clean_view && !this.original_view) {
          const has_meta =
            Object.keys(active_ins).length > 0 ||
            Object.keys(active_del).length > 0 ||
            active_ids.size > 0 ||
            Object.keys(active_fmt).length > 0;
          if (has_meta) {
            deferred_meta_states.push([
              { ...active_ins },
              { ...active_del },
              new Set(active_ids),
              { ...active_fmt },
            ]);
          }

          let should_defer = false;
          const has_any_meta =
            curr_ins_id !== null ||
            curr_del_id !== null ||
            Object.keys(active_fmt).length > 0 ||
            active_ids.size > 0;

          if (has_any_meta) {
            let j = i + 1;
            let next_has_meta = false;
            let temp_ins_count = Object.keys(active_ins).length;
            let temp_del_count = Object.keys(active_del).length;
            let temp_fmt_count = Object.keys(active_fmt).length;
            const temp_comment_ids = new Set(active_ids);

            while (j < items.length) {
              const next_item = items[j];
              if (next_item instanceof Run) {
                if (!get_run_text(next_item)) {
                  j++;
                  continue;
                }
                if (
                  temp_ins_count > 0 ||
                  temp_del_count > 0 ||
                  temp_fmt_count > 0 ||
                  temp_comment_ids.size > 0
                ) {
                  next_has_meta = true;
                }
                break;
              } else {
                const ev = next_item as DocxEvent;
                if (ev.type === "ins_start") temp_ins_count++;
                else if (ev.type === "ins_end")
                  temp_ins_count = Math.max(0, temp_ins_count - 1);
                else if (ev.type === "del_start") temp_del_count++;
                else if (ev.type === "del_end")
                  temp_del_count = Math.max(0, temp_del_count - 1);
                else if (ev.type === "fmt_start") temp_fmt_count++;
                else if (ev.type === "fmt_end")
                  temp_fmt_count = Math.max(0, temp_fmt_count - 1);
                else if (ev.type === "start") temp_comment_ids.add(ev.id);
                else if (ev.type === "end") temp_comment_ids.delete(ev.id);
              }
              j++;
            }

            if (next_has_meta) should_defer = true;
          }

          if (!should_defer && deferred_meta_states.length > 0) {
            const meta_block =
              this._build_merged_meta_block(deferred_meta_states);
            if (meta_block) {
              flush_pending_runs();
              current_wrappers = ["", ""];
              current_style = ["", ""];
              const full_meta = `{>>${meta_block}<<}`;
              this._add_virtual_text(full_meta, current, paragraph);
              current += full_meta.length;
            }
            deferred_meta_states = [];
          }
        }
      } else {
        const ev = item as DocxEvent;
        leading_strip_active = false;
        flush_pending_runs();
        current_wrappers = ["", ""];
        current_style = ["", ""];

        if (ev.type === "start") active_ids.add(ev.id);
        else if (ev.type === "end") active_ids.delete(ev.id);
        else if (ev.type === "ins_start") active_ins[ev.id] = ev;
        else if (ev.type === "ins_end") delete active_ins[ev.id];
        else if (ev.type === "del_start") active_del[ev.id] = ev;
        else if (ev.type === "del_end") delete active_del[ev.id];
        else if (ev.type === "fmt_start") active_fmt[ev.id] = ev;
        else if (ev.type === "fmt_end") delete active_fmt[ev.id];
        else if (ev.type === "footnote" || ev.type === "endnote") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          const prefix_str = ev.type === "footnote" ? "fn" : "en";
          const txt = `[^${prefix_str}-${ev.id}]`;
          this._add_virtual_text(txt, current, paragraph);
          current += txt.length;
        } else if (ev.type === "hyperlink_start") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          this._add_virtual_text("[", current, paragraph, ev.id);
          current += 1;
          active_hyperlink_id = ev.id;
        } else if (ev.type === "hyperlink_end") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          const txt = `](${ev.date})`;
          this._add_virtual_text(txt, current, paragraph, ev.id);
          current += txt.length;
          active_hyperlink_id = null;
        } else if (ev.type === "xref_start") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          this._add_virtual_text("[~", current, paragraph);
          current += 2;
        } else if (ev.type === "xref_end") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          const txt = `~](#${ev.id})`;
          this._add_virtual_text(txt, current, paragraph);
          current += txt.length;
        } else if (ev.type === "bookmark") {
          flush_pending_runs();
          current_wrappers = ["", ""];
          current_style = ["", ""];
          const txt = `{#${ev.id}}`;
          this._add_virtual_text(txt, current, paragraph);
          current += txt.length;
        }
      }
    }

    flush_pending_runs();

    if (deferred_meta_states.length > 0) {
      const meta_block = this._build_merged_meta_block(deferred_meta_states);
      if (meta_block) {
        const full_meta = `{>>${meta_block}<<}`;
        this._add_virtual_text(full_meta, current, paragraph);
        current += full_meta.length;
      }
    }

    return current;
  }

  private _get_wrappers(
    ins_id: string | null,
    del_id: string | null,
    active_ids: Set<string>,
    active_fmt: Record<string, DocxEvent>,
  ): [string, string] {
    if (del_id) return ["{--", "--}"];
    if (ins_id) return ["{++", "++}"];
    if (active_ids.size > 0 || Object.keys(active_fmt).length > 0)
      return ["{==", "==}"];
    return ["", ""];
  }

  private _build_merged_meta_block(states_list: any[]): string {
    const change_lines: string[] = [];
    const comment_lines: string[] = [];
    const seen_sigs = new Set<string>();

    for (const [ins_map, del_map, comments_set, fmt_map] of states_list) {
      for (const [uid, meta] of Object.entries(
        ins_map as Record<string, DocxEvent>,
      )) {
        const sig = `Chg:${uid}`;
        if (!seen_sigs.has(sig)) {
          const auth = meta.author || "Unknown";
          change_lines.push(`[${sig} insert] ${auth}`);
          seen_sigs.add(sig);
        }
      }
      for (const [uid, meta] of Object.entries(
        del_map as Record<string, DocxEvent>,
      )) {
        const sig = `Chg:${uid}`;
        if (!seen_sigs.has(sig)) {
          const auth = meta.author || "Unknown";
          change_lines.push(`[${sig} delete] ${auth}`);
          seen_sigs.add(sig);
        }
      }
      for (const [uid, meta] of Object.entries(
        fmt_map as Record<string, DocxEvent>,
      )) {
        const sig = `Chg:${uid}`;
        if (!seen_sigs.has(sig)) {
          const auth = meta.author || "Unknown";
          change_lines.push(`[${sig} format] ${auth}`);
          seen_sigs.add(sig);
        }
      }

      const sorted_ids = Array.from(comments_set as Set<string>).sort();
      for (const c_id of sorted_ids) {
        if (!this.comments_map[c_id]) continue;
        const sig = `Com:${c_id}`;
        if (!seen_sigs.has(sig)) {
          const data = this.comments_map[c_id];
          let header = `[${sig}] ${data.author}`;
          if (data.date) header += ` @ ${data.date}`;
          if (data.resolved) header += `(RESOLVED)`;
          comment_lines.push(`${header}: ${data.text}`);
          seen_sigs.add(sig);
        }
      }
    }

    return [...change_lines, ...comment_lines].join("\n");
  }

  private _add_virtual_text(
    text: string,
    offset: number,
    context_paragraph: Paragraph | null,
    hyperlink_id: string | null = null,
  ) {
    const span: TextSpan = {
      start: offset,
      end: offset + text.length,
      text,
      run: null,
      paragraph: context_paragraph,
      hyperlink_id: hyperlink_id || undefined,
    };
    this.spans.push(span);
    this._text_chunks.push(text);
  }

  private _replace_smart_quotes(text: string): string {
    return text
      .replace(/“/g, '"')
      .replace(/”/g, '"')
      .replace(/‘/g, "'")
      .replace(/’/g, "'");
  }

  private _make_fuzzy_regex(target_text: string): string {
    target_text = this._strip_markdown_formatting(target_text);
    target_text = this._replace_smart_quotes(target_text);

    const parts: string[] = [];
    const token_pattern = /(\[_+\])|(\s+)|(['"])|([.,;:\/\-\[\](){}+=$?*!|#^<>\\%&@~`_])/g;

    let last_idx = 0;
    let match;
    const escapeRegExp = (str: string) =>
      str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

    while ((match = token_pattern.exec(target_text)) !== null) {
      const literal = target_text.substring(last_idx, match.index);
      if (literal) parts.push(escapeRegExp(literal));

      const g_placeholder = match[1];
      const g_space = match[2];
      const g_quote = match[3];
      const g_punct = match[4];

      if (g_placeholder) {
        parts.push("\\[_+\\]");
      } else if (g_space) {
        parts.push("(?:\\*\\*|__|\\*|_)?");
        parts.push("\\s+");
        parts.push("(?:\\*\\*|__|\\*|_)?");
      } else if (g_quote) {
        if (g_quote === "'") parts.push("[\u2018\u2019']");
        else parts.push('["\u201c\u201d]');
      } else if (g_punct) {
        parts.push("(?:\\*\\*|__|\\*|_)?");
        parts.push(escapeRegExp(g_punct));
        parts.push("(?:\\*\\*|__|\\*|_)?");
      }

      last_idx = token_pattern.lastIndex;
    }

    const remaining = target_text.substring(last_idx);
    if (remaining) parts.push(escapeRegExp(remaining));

    return parts.join("");
  }

  /**
   * Returns [plain_text, offset_map] where plain_text is full_text with the
   * VIRTUAL markdown style delimiters (bold/italic markers emitted around
   * formatted runs) removed, and offset_map[i] is the full_text index of
   * plain_text[i].
   *
   * Formatting run boundaries can fall mid-word (e.g. a paragraph projected
   * as "**Al**pha"), where neither exact matching nor the whitespace-anchored
   * fuzzy regex can find the plain target "Alpha". Matching against this
   * projection and mapping the span back to full_text closes that gap (QA H2).
   *
   * Built lazily and invalidated by _build_map(): most batches never need it.
   */
  private _get_plain_projection(): [string, number[]] {
    if (this._plain_projection === null) {
      const chunks: string[] = [];
      const offsets: number[] = [];
      for (const s of this.spans) {
        if (
          s.run === null &&
          s.paragraph !== null &&
          STYLE_MARKER_TEXTS.has(s.text)
        ) {
          continue;
        }
        chunks.push(s.text);
        for (let k = s.start; k < s.end; k++) offsets.push(k);
      }
      this._plain_projection = [chunks.join(""), offsets];
    }
    return this._plain_projection;
  }

  /**
   * Matches a markdown-stripped target against the plain projection and maps
   * each hit back to a [start, length] span in full_text. Interior style
   * markers end up inside the returned span (so "Alpha" over "**Al**pha"
   * resolves to the "Al**pha" range); markers just outside the matched
   * characters are excluded.
   */
  private _find_plain_projection_matches(
    target_text: string,
  ): [number, number][] {
    const [plain_text, offsets] = this._get_plain_projection();
    if (plain_text.length === this.full_text.length) {
      return []; // No virtual style markers anywhere; nothing new to find.
    }
    const norm_target = this._replace_smart_quotes(
      this._strip_markdown_formatting(target_text),
    );
    if (!norm_target) return [];
    const norm_plain = this._replace_smart_quotes(plain_text);
    const results: [number, number][] = [];
    let from = 0;
    while (true) {
      const p_start = norm_plain.indexOf(norm_target, from);
      if (p_start === -1) break;
      const p_end = p_start + norm_target.length;
      const raw_start = offsets[p_start];
      const raw_end = offsets[p_end - 1] + 1;
      results.push([raw_start, raw_end - raw_start]);
      from = p_end;
    }
    return results;
  }

  public find_match_index(
    target_text: string,
    is_regex: boolean = false,
  ): [number, number] {
    if (is_regex) {
      // User/LLM-supplied pattern: run it under a wall-clock budget so a
      // catastrophic pattern cannot hang the event loop (QA 2026-07-17 F5).
      // RegexTimeoutError propagates for a clean per-edit error; only
      // invalid-pattern errors mean "no match" here.
      try {
        const match = userSearch(target_text, this.full_text);
        if (match) return [match.start, match.end - match.start];
      } catch (e) {
        if (e instanceof RegexTimeoutError) throw e;
      }
      return [-1, 0];
    }

    let start_idx = this.full_text.indexOf(target_text);
    if (start_idx !== -1) return [start_idx, target_text.length];

    const norm_full = this._replace_smart_quotes(this.full_text);
    const norm_target = this._replace_smart_quotes(target_text);
    start_idx = norm_full.indexOf(norm_target);
    if (start_idx !== -1) return [start_idx, target_text.length];

    const stripped_target = this._strip_markdown_formatting(target_text);
    if (this.full_text.includes(stripped_target)) {
      start_idx = this.full_text.indexOf(stripped_target);
      return [start_idx, stripped_target.length];
    }

    // 3.5 Plain-projection match: the target crosses a formatting run
    // boundary (possibly mid-word), so the projection carries style markers
    // the plain target doesn't have (QA H2).
    const plain_first = this._find_plain_projection_matches(target_text);
    if (plain_first.length > 0) return plain_first[0];

    try {
      const pattern = new RegExp(this._make_fuzzy_regex(target_text));
      const match = pattern.exec(this.full_text);
      if (match) return [match.index, match[0].length];
    } catch (e) {}

    return [-1, 0];
  }

  public find_all_match_indices(
    target_text: string,
    is_regex: boolean = false,
  ): [number, number][] {
    if (!target_text) return [];

    if (is_regex) {
      // Budgeted like find_match_index above (QA 2026-07-17 F5).
      try {
        const matches = userFindAllMatches(target_text, this.full_text);
        if (matches.length > 0) return matches.map((m) => [m.start, m.end - m.start]);
      } catch (e) {
        if (e instanceof RegexTimeoutError) throw e;
      }
      return [];
    }
    // Exact tiers use plain indexOf scans, NOT RegExp: building a RegExp from
    // an arbitrarily long escaped target throws "regular expression too
    // large" for oversized inputs, crashing validation instead of returning
    // a clean not-found (QA C2 hardening).
    const findAllLiteral = (
      haystack: string,
      needle: string,
    ): [number, number][] => {
      const out: [number, number][] = [];
      if (!needle) return out;
      let from = 0;
      while (true) {
        const idx = haystack.indexOf(needle, from);
        if (idx === -1) break;
        out.push([idx, needle.length]);
        from = idx + needle.length;
      }
      return out;
    };

    let matches = findAllLiteral(this.full_text, target_text);
    if (matches.length > 0) return matches;

    const norm_full = this._replace_smart_quotes(this.full_text);
    const norm_target = this._replace_smart_quotes(target_text);
    matches = findAllLiteral(norm_full, norm_target);
    if (matches.length > 0) return matches;

    const stripped_target = this._strip_markdown_formatting(target_text);
    matches = findAllLiteral(this.full_text, stripped_target);
    if (matches.length > 0) return matches;

    // 3.5 Plain-projection match (target spans a bold/italic run boundary,
    // possibly mid-word). See _find_plain_projection_matches (QA H2).
    const plain_matches = this._find_plain_projection_matches(target_text);
    if (plain_matches.length > 0) return plain_matches;

    try {
      const pattern = new RegExp(this._make_fuzzy_regex(target_text), "g");
      const fuzzy = [...this.full_text.matchAll(pattern)];
      if (fuzzy.length > 0) return fuzzy.map((m) => [m.index!, m[0].length]);
    } catch (e) {}

    return [];
  }

  public find_target_runs(target_text: string): Run[] {
    const [start_idx, length] = this.find_match_index(target_text);
    if (start_idx === -1) return [];
    return this._resolve_runs_at_range(start_idx, start_idx + length);
  }

  public find_target_runs_by_index(
    start_index: number,
    length: number,
    rebuild_map = true,
  ): Run[] {
    return this._resolve_runs_at_range(
      start_index,
      start_index + length,
      rebuild_map,
    );
  }

  public get_virtual_spans_in_range(
    start_index: number,
    length: number,
  ): TextSpan[] {
    const end_index = start_index + length;
    return this.spans.filter(
      (s) =>
        s.run === null &&
        s.text === "\n\n" &&
        s.start >= start_index &&
        s.end <= end_index,
    );
  }

  private _resolve_runs_at_range(
    start_idx: number,
    end_idx: number,
    rebuild_map = true,
  ): Run[] {
    const affected_spans = this.spans.filter(
      (s) => s.end > start_idx && s.start < end_idx,
    );
    if (affected_spans.length === 0) return [];

    const working_runs = affected_spans
      .filter((s) => s.run !== null)
      .map((s) => s.run!);
    if (working_runs.length === 0) return [];

    let dom_modified = false;

    const first_real_span = affected_spans.find((s) => s.run !== null);
    let start_split_adjustment = 0;

    if (first_real_span) {
      const local_start = start_idx - first_real_span.start;
      if (local_start > 0) {
        const idx_in_working = 0;
        const [, right_run] = this._split_run_at_index(
          working_runs[idx_in_working],
          local_start,
        );
        working_runs[idx_in_working] = right_run;
        dom_modified = true;
        start_split_adjustment = local_start;
      }
    }

    const last_real_span = [...affected_spans]
      .reverse()
      .find((s) => s.run !== null);

    if (last_real_span) {
      const is_same_run = first_real_span === last_real_span;
      const run_to_split = working_runs[working_runs.length - 1];
      let overlap_end = Math.min(last_real_span.end, end_idx);
      let local_end = overlap_end - last_real_span.start;

      if (is_same_run && start_split_adjustment > 0) {
        local_end -= start_split_adjustment;
      }

      const run_text = get_run_text(run_to_split);
      if (local_end > 0 && local_end < run_text.length) {
        const [left_run] = this._split_run_at_index(run_to_split, local_end);
        working_runs[working_runs.length - 1] = left_run;
        dom_modified = true;
      }
    }

    if (dom_modified && rebuild_map) {
      this._build_map();
    }

    return working_runs;
  }

  public get_insertion_anchor(
    index: number,
    rebuild_map = true,
  ): [Run | null, Paragraph | null] {
    const preceding = this.spans.filter((s) => s.end === index);
    if (preceding.length > 0) {
      for (let i = preceding.length - 1; i >= 0; i--) {
        if (preceding[i].run) return [preceding[i].run, preceding[i].paragraph];
      }
      for (let i = preceding.length - 1; i >= 0; i--) {
        const para = preceding[i].paragraph;
        if (para) {
          // Every span ending exactly here is virtual (CriticMarkup
          // wrappers, {>>...<<} meta blocks, prefixes). If real text
          // precedes this index in the SAME paragraph, anchor after its
          // last run: falling back to the bare paragraph would drop the
          // insertion at paragraph start, ahead of the very redlines and
          // comment ranges that fence off the true position (mirrors the
          // Python mapper).
          for (let j = this.spans.length - 1; j >= 0; j--) {
            const prev = this.spans[j];
            if (
              prev.end <= index &&
              prev.run !== null &&
              prev.paragraph === para
            ) {
              return [prev.run, prev.paragraph];
            }
          }
          return [null, para];
        }
      }
    }

    const containing = this.spans.filter(
      (s) => s.start < index && index < s.end,
    );
    if (containing.length > 0) {
      const span = containing[0];
      if (span.run === null) {
        if (span.paragraph === null) {
          return this.get_insertion_anchor(span.end, rebuild_map);
        }
        return [null, span.paragraph];
      } else {
        const offset = index - span.start;
        const [left] = this._split_run_at_index(span.run, offset);
        if (rebuild_map) this._build_map();
        return [left, span.paragraph];
      }
    }

    if (index === 0 && this.spans.length > 0) {
      for (const s of this.spans) if (s.run) return [s.run, s.paragraph];
      for (const s of this.spans) if (s.paragraph) return [null, s.paragraph];
      return [null, null];
    }

    const preceding_gap = this.spans.filter((s) => s.end < index);
    if (preceding_gap.length > 0) {
      for (let i = preceding_gap.length - 1; i >= 0; i--) {
        if (preceding_gap[i].run)
          return [preceding_gap[i].run, preceding_gap[i].paragraph];
      }
      for (let i = preceding_gap.length - 1; i >= 0; i--) {
        if (preceding_gap[i].paragraph)
          return [null, preceding_gap[i].paragraph];
      }
    }

    return [null, null];
  }

  private _split_run_at_index(run: Run, split_index: number): [Run, Run] {
    const text = get_run_text(run);
    const left_text = text.substring(0, split_index);
    const right_text = text.substring(split_index);

    this._set_run_text_elements(run._element, left_text);

    const new_r_element = run._element.cloneNode(true) as Element;
    this._set_run_text_elements(new_r_element, right_text);

    if (run._element.parentNode) {
      run._element.parentNode.insertBefore(
        new_r_element,
        run._element.nextSibling,
      );
    }

    const new_run = new Run(new_r_element, run._parent);
    return [run, new_run];
  }

  private _set_run_text_elements(r_element: Element, new_text: string) {
    const to_remove: Element[] = [];
    for (let i = 0; i < r_element.childNodes.length; i++) {
      const child = r_element.childNodes[i] as Element;
      if (
        child.nodeType === 1 &&
        ["w:t", "w:delText", "w:br", "w:cr", "w:tab"].includes(child.tagName)
      ) {
        to_remove.push(child);
      }
    }
    for (const child of to_remove) {
      r_element.removeChild(child);
    }

    const doc = r_element.ownerDocument;
    if (doc) {
      const new_t = doc.createElement("w:t");
      new_t.textContent = new_text;
      if (new_text.trim() !== new_text) {
        new_t.setAttribute("xml:space", "preserve");
      }
      r_element.appendChild(new_t);
    }
  }

  public get_context_at_range(
    start_idx: number,
    end_idx: number,
  ): TextSpan | null {
    const real_spans = this.spans.filter(
      (s) => s.run && s.end > start_idx && s.start < end_idx,
    );
    if (real_spans.length > 0) return real_spans[0];
    return null;
  }
}
