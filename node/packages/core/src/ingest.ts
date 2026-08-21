import { DocumentObject } from "./docx/bridge.js";
import { Paragraph, Table, Run, DocxEvent } from "./docx/primitives.js";
import {
  _get_style_cache,
  compute_change_pair_map,
  get_paragraph_prefix,
  paragraph_mark_is_deleted,
  is_heading_paragraph,
  is_native_heading,
  get_run_style_markers,
  get_run_text,
  apply_formatting_to_segments,
  iter_block_items,
  iter_document_parts_with_kind,
  iter_paragraph_content,
} from "./utils/docx.js";
import { findChild } from "./docx/dom.js";
import {
  assignOrdinals,
  BlockSdt,
  CHECKBOX_CLOSE,
  CHECKBOX_OPEN,
  checkboxMark,
  closeToken,
  isAnchored,
  isSdtEvent,
  openToken,
  partElement,
  wrappingSdt,
  type SdtInfo,
} from "./utils/content-controls.js";
import { resolve_cell_anchor } from "./docx/cell-anchor.js";
import { build_structural_appendix } from "./domain.js";
import { extract_comments_data } from "./comments.js";
import { escape_critic_tokens } from "./utils/text.js";

/** Text-space extent of one projected table row plus its cell texts. */
export interface RowGeometry {
  start: number;
  end: number;
  cells: string[];
}

/** Text-space extent of one projected top-level table. */
export interface TableGeometry {
  start: number;
  end: number;
  rows: RowGeometry[];
}

/**
 * Structural map of a projection: which offset ranges belong to which OPC
 * part, and where top-level table rows live. Produced in the SAME pass as
 * the text, so offsets always agree with it. Consumed by the diff pipeline
 * to keep generated edits from crossing part boundaries (QA 2026-07-18 C1)
 * and to emit structured row operations for table changes (QA C2).
 */
export interface ExtractStructure {
  part_ranges: [number, number, string][]; // [start, end, kind]
  tables: TableGeometry[];
}

export async function extractTextFromBuffer(
  buffer: Buffer,
  cleanView = false,
  includeAppendix = true,
): Promise<string> {
  const doc = await DocumentObject.load(buffer);
  return _extractTextFromDoc(doc, cleanView, includeAppendix) as string;
}

export function _extractTextFromDoc(
  doc: DocumentObject,
  cleanView = false,
  includeAppendix = true,
  return_paragraph_offsets = false,
  return_structure = false,
):
  | string
  | { text: string; paragraph_offsets: Map<any, [number, number]> }
  | { text: string; structure: ExtractStructure } {
  const comments_map = extract_comments_data(doc.pkg);

  const full_text: string[] = [];
  const paragraph_offsets = new Map<any, [number, number]>();
  const structure: ExtractStructure | null = return_structure
    ? { part_ranges: [], tables: [] }
    : null;
  let cursor = 0;

  // Ordinals are assigned ONCE, over the parts in projection order, and the
  // resulting map is threaded through every level below. Spec-projection.md §9
  // requires this to be a single shared pre-pass rather than a counter each
  // producer maintains: a counter is exactly the shape of bug CC-12 was (two
  // producers agreeing with each other and both wrong).
  const sdtInfos = assignOrdinals(
    Array.from(iter_document_parts_with_kind(doc)).map(([part]) =>
      partElement(part),
    ),
  );

  for (const [part, part_kind] of iter_document_parts_with_kind(doc)) {
    const part_cursor = full_text.length > 0 ? cursor + 2 : cursor;
    const part_text = _extract_blocks(
      part,
      comments_map,
      cleanView,
      part_cursor,
      return_paragraph_offsets ? paragraph_offsets : undefined,
      structure ? structure.tables : undefined,
      sdtInfos,
    );
    if (part_text) {
      if (full_text.length > 0) cursor += 2;
      full_text.push(part_text);
      if (structure) {
        structure.part_ranges.push([cursor, cursor + part_text.length, part_kind]);
      }
      cursor += part_text.length;
    }
  }

  let base_text = full_text.join("\n\n");

  if (includeAppendix) {
    const appendix = build_structural_appendix(doc, base_text);
    if (appendix) base_text += appendix;
  }

  if (return_paragraph_offsets) {
    return { text: base_text, paragraph_offsets };
  }
  if (structure) {
    return { text: base_text, structure };
  }
  return base_text;
}

/**
 * table_acc: optional list collecting TableGeometry for TOP-LEVEL tables.
 * Deliberately not forwarded into cells — nested tables stay invisible to
 * the structured row-op diff, whose row pairing assumes one flat grid.
 */
function _extract_blocks(
  container: any,
  comments_map: any,
  cleanView: boolean,
  cursor: number,
  paragraph_offsets?: Map<any, [number, number]>,
  table_acc?: TableGeometry[],
  sdtInfos?: Map<any, SdtInfo>,
  inCell = false,
): string {
  const part = container.part || container;
  const [style_cache, default_pstyle] = _get_style_cache(part);

  const blocks: string[] = [];
  let local_cursor = cursor;
  let is_first_block = true;
  let is_first_para = true;

  if (container.constructor && container.constructor.name === "NotesPart") {
    const header =
      container.note_type === "fn" ? "## Footnotes" : "## Endnotes";
    const sep = `---\n${header}`;
    blocks.push(sep);
    local_cursor += sep.length;
    is_first_block = false;
  }

  for (const item of iter_block_items(container, !!sdtInfos)) {
    if (!is_first_block) local_cursor += 2;
    const block_start = local_cursor;

    if (item instanceof BlockSdt) {
      // A block-level content control. Recurse into its contents exactly as a
      // Table recurses into its rows, then bracket the result with token
      // lines: open token on its own line, a single "\n" joining it to the
      // wrapped content, close token on its own line (spec §3/§5). The
      // surrounding "\n\n" comes from the block join, as for any other block.
      const info = sdtInfos ? sdtInfos.get(item.element) : undefined;
      const anchored = !!info && isAnchored(info);
      // Spec §3 exception: inside a table cell a block-level anchor renders
      // INLINE. A row is one projected line, so token lines would break the
      // "|" grammar and desynchronise the column count.
      const joiner = inCell ? "" : "\n";
      const open_tok = anchored ? `${openToken(info!)}${joiner}` : "";
      // Pass a container SHIM, not the bare sdtContent element: this engine
      // derives the OPC part via `container.part`, and handing it a raw
      // element silently lost the part — which broke hyperlink relationship
      // resolution inside every block-level control ("[text](mailto:...)"
      // degraded to bare text). Python takes `part=` as an explicit argument
      // and so never had the hazard.
      const inner = _extract_blocks(
        {
          _element: findChild(item.element, "w:sdtContent"),
          part: (container as any).part || container,
        },
        comments_map,
        cleanView,
        block_start + open_tok.length,
        paragraph_offsets,
        undefined,
        sdtInfos,
        inCell,
      );
      if (inner) {
        const full = anchored
          ? `${open_tok}${inner}${joiner}${closeToken(info!)}`
          : inner;
        blocks.push(full);
        local_cursor = block_start + full.length;
        is_first_block = false;
      } else if (!is_first_block) {
        // Projects nothing: the reader drops the block AND its separator,
        // same contract as an empty table.
        local_cursor -= 2;
      }
      is_first_para = false;
    } else if (item.constructor.name === "FootnoteItem") {
      const fn_text = _extract_blocks(
        item,
        comments_map,
        cleanView,
        block_start,
        paragraph_offsets,
        undefined,
        sdtInfos,
      );
      if (fn_text) {
        blocks.push(fn_text);
        local_cursor = block_start + fn_text.length;
        is_first_block = false;
      } else if (!is_first_block) {
        local_cursor -= 2;
      }
    } else if (item instanceof Paragraph) {
      let prefix = get_paragraph_prefix(item, style_cache, default_pstyle);
      if (is_first_para && container.constructor.name === "FootnoteItem") {
        prefix = `[^${container.note_type}-${container.id}]: ` + prefix;
      }
      const p_text = build_paragraph_text(
        item,
        comments_map,
        cleanView,
        style_cache,
        default_pstyle,
        sdtInfos,
      );
      if (cleanView && !p_text && paragraph_mark_is_deleted(item._element)) {
        // Accepting a tracked paragraph-mark deletion merges the paragraph
        // away; when nothing visible survives inside it, the accepted view
        // must not render an empty container. Twin of python
        // ingest._extract_blocks (QA round 3, finding 2.4) — without it the
        // clean view grew a stray blank line per deleted-mark paragraph
        // ("Alpha\n\n\n\nBeta" where python gives "Alpha\n\nBeta").
        if (!is_first_block) local_cursor -= 2;
        is_first_para = false;
        continue;
      }
      const full_block = prefix + p_text;
      blocks.push(full_block);
      if (paragraph_offsets) {
        paragraph_offsets.set(item._element, [block_start, full_block.length]);
      }
      local_cursor = block_start + full_block.length;
      is_first_para = false;
      is_first_block = false;
    } else if (item instanceof Table) {
      const geometry: TableGeometry | null = table_acc
        ? { start: block_start, end: block_start, rows: [] }
        : null;
      const table_text = extract_table(
        item,
        comments_map,
        cleanView,
        block_start,
        paragraph_offsets,
        geometry,
        sdtInfos,
      );
      if (table_text) {
        blocks.push(table_text);
        local_cursor = block_start + table_text.length;
        is_first_block = false;
        if (geometry && table_acc) {
          geometry.end = block_start + table_text.length;
          table_acc.push(geometry);
        }
      } else if (!is_first_block) {
        local_cursor -= 2;
      }
      is_first_para = false;
    }
  }

  return blocks.join("\n\n");
}

/** The SdtInfo of the control wrapping this w:tr/w:tc, when it anchors. */
function anchoredWrapper(
  element: any,
  sdtInfos?: Map<any, SdtInfo>,
): SdtInfo | null {
  if (!sdtInfos) return null;
  const sdt = wrappingSdt(element);
  if (!sdt) return null;
  const info = sdtInfos.get(sdt);
  return info && isAnchored(info) ? info : null;
}

export function extract_table(
  table: Table,
  comments_map: any,
  cleanView: boolean,
  cursor: number,
  paragraph_offsets?: Map<any, [number, number]>,
  geometry?: TableGeometry | null,
  sdtInfos?: Map<any, SdtInfo>,
): string {
  const rows_text: string[] = [];
  let rows_processed = 0;
  let local_cursor = cursor;

  for (const row of table.rows) {
    const cell_texts: string[] = [];
    const seen_cells = new Set();

    const trPr = findChild(row._element, "w:trPr");
    const ins = trPr ? findChild(trPr, "w:ins") : null;
    const del_node = trPr ? findChild(trPr, "w:del") : null;

    if (cleanView && del_node) continue;

    const row_start = local_cursor + (rows_processed > 0 ? 1 : 0);
    const wrapper_prefix_len =
      !cleanView && ins ? 4 : !cleanView && del_node ? 4 : 0;

    let cell_cursor = row_start + wrapper_prefix_len;
    let first_cell = true;

    for (const cell of row.cells) {
      if (seen_cells.has(cell)) continue;
      seen_cells.add(cell);

      if (!first_cell) cell_cursor += 3;

      const cellInfo = anchoredWrapper(cell._element, sdtInfos);
      const cellOpen = cellInfo ? openToken(cellInfo) : "";
      let cell_content = _extract_blocks(
        cell,
        comments_map,
        cleanView,
        cell_cursor + cellOpen.length,
        paragraph_offsets,
        undefined,
        sdtInfos,
        true,
      );
      if (cellInfo) {
        // Cell-level control (sdtContent > w:tc): anchors render inline inside
        // this cell's segment (spec §3).
        cell_content = `${cellOpen}${cell_content}${closeToken(cellInfo)}`;
      }
      // Emit a stable, document-native anchor for this cell so empty/short
      // value cells are addressable by the engine. Reuses the {#...} bookmark
      // projection (already protected by validate_edit_strings and resolvable
      // via the mapper). We key on the cell's first paragraph w14:paraId, which
      // Word assigns and keeps stable across reads. Fallback-id derivation
      // (and its whole-document index) lives in resolve_cell_anchor — shared
      // with the mapper twin and cached per Document to avoid the historical
      // O(empty cells × document size) rescans.
      if (!cleanView) {
        const { paraId } = resolve_cell_anchor(
          cell._element,
          !cell_content || cell_content.trim() === "",
        );
        if (paraId) {
          // Only pad when the cell text does not already end in a space:
          // emphasis hoists trailing whitespace out of its closing marker, so
          // a bold cell commonly ends "**Label** " and padding unconditionally
          // produced "**Label**  {#cell:...}" — two spaces where python emits
          // one. Mirrors python ingest.py's `separator`.
          const space_pad =
            cell_content && !cell_content.endsWith(" ") ? " " : "";
          cell_content = cell_content + `${space_pad}{#cell:${paraId}}`;
        }
      }
      cell_texts.push(cell_content);
      cell_cursor += cell_content.length;
      first_cell = false;
    }

    let row_str = cell_texts.join(" | ");

    // Row-level control (sdtContent > w:tr): open token before the first
    // cell's text, close after the last, on the row's line (spec §3). Applied
    // before the tracked-change wrapper below so a row that is both controlled
    // and inserted reads "{++ {#cc:N}...{#/cc:N} ++}" — the CriticMarkup is
    // about the row's existence, the anchor about its identity.
    const rowInfo = anchoredWrapper(row._element, sdtInfos);
    if (rowInfo) {
      row_str = `${openToken(rowInfo)}${row_str}${closeToken(rowInfo)}`;
    }

    if (!cleanView) {
      // The change bubble is SEPARATED from the cell content (mirroring the
      // normal `{++text++}{>>[Chg:N insert] Author<<}` insertion shape) — the
      // old ` |Chg:N++}` suffix glued the id onto the last cell through a
      // pipe, so the row read as if it had an extra cell named "Chg:N"
      // (QA 2026-07-23 F21a). Twin rendering in mapper._map_table — the two
      // MUST stay byte-identical (Virtual Text contract).
      if (ins) {
        row_str = `{++ ${row_str} ++}{>>[Chg:${ins.getAttribute("w:id")} insert] ${ins.getAttribute("w:author") || "Unknown"}<<}`;
      } else if (del_node) {
        row_str = `{-- ${row_str} --}{>>[Chg:${del_node.getAttribute("w:id")} delete] ${del_node.getAttribute("w:author") || "Unknown"}<<}`;
      }
    }

    rows_text.push(row_str);
    local_cursor = row_start + row_str.length;
    if (geometry) {
      geometry.rows.push({
        start: row_start,
        end: local_cursor,
        cells: [...cell_texts],
      });
    }
    rows_processed++;

    if (rows_processed === 1) {
      const num_cols = cell_texts.length;
      if (num_cols > 0) {
        const divider_str = Array(num_cols).fill("---").join(" | ");
        rows_text.push(divider_str);
        local_cursor += 1 + divider_str.length;
      }
    }
  }

  return rows_text.join("\n");
}

export function build_paragraph_text(
  paragraph: Paragraph,
  comments_map: any,
  cleanView: boolean,
  style_cache?: any,
  default_pstyle?: string | null,
  sdtInfos?: Map<any, SdtInfo>,
): string {
  const parts: string[] = [];
  const active_ins: Record<string, DocxEvent> = {};
  const active_del: Record<string, DocxEvent> = {};
  const active_comments: Set<string> = new Set();
  const active_fmt: Record<string, DocxEvent> = {};
  const deferred_meta_states: any[] = [];

  let pending_text = "";
  let current_wrappers: [string, string] = ["", ""];
  let current_style: [string, string] = ["", ""];

  const items = Array.from(iter_paragraph_content(paragraph, sdtInfos));
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
      const text = get_run_text(item);

      if (cleanView && Object.keys(active_del).length > 0) continue;

      if (leading_strip_active) {
        if (!text || !text.trim()) continue;
        leading_strip_active = false;
      }

      const seg = apply_formatting_to_segments(text, prefix, suffix);
      if (seg) {
        const new_wrappers = cleanView
          ? (["", ""] as [string, string])
          : _get_wrappers(active_ins, active_del, active_comments, active_fmt);
        const new_style: [string, string] = [prefix, suffix];

        if (
          pending_text &&
          new_wrappers[0] === current_wrappers[0] &&
          new_wrappers[1] === current_wrappers[1]
        ) {
          // Hoisted leading whitespace may sit before the incoming segment's
          // opening marker ("**A**" + " **B**" -> "**A B**"), mirroring the
          // mapper's part-level elision exactly (QA 2026-07-19 F-03).
          const escaped_prefix = new_style[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          const lead_match =
            new_style[0] !== "" || new_style[1] !== ""
              ? seg.match(new RegExp("^(\\s*)" + escaped_prefix))
              : null;
          // Trailing whitespace is hoisted OUT of the closing marker, so the
          // pending group commonly ends "**A** " rather than "**A**". Testing
          // endsWith() against the literal tail therefore misses the elision
          // and yields "**A** **B**" instead of "**A B**". Ignore trailing
          // whitespace for the test, then put it back. Mirrors python
          // ingest.build_paragraph_text exactly (CC-10 follow-up).
          let trailing_ws = "";
          for (let k = pending_text.length - 1; k >= 0; k--) {
            const ch = pending_text[k]!;
            if (/\s/.test(ch)) trailing_ws = ch + trailing_ws;
            else break;
          }
          const pending_without_ws = trailing_ws
            ? pending_text.slice(0, -trailing_ws.length)
            : pending_text;
          if (
            new_style[0] === current_style[0] &&
            new_style[1] === current_style[1] &&
            !(current_style[0] === "" && current_style[1] === "") &&
            pending_without_ws.endsWith(current_style[1]) &&
            lead_match !== null
          ) {
            pending_text =
              pending_without_ws.slice(0, -current_style[1].length) +
              trailing_ws +
              lead_match[1] +
              seg.slice(lead_match[0].length);
          } else {
            pending_text += seg;
          }
          current_style = new_style;
        } else {
          if (pending_text)
            parts.push(
              `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
            );
          pending_text = seg;
          current_wrappers = new_wrappers;
          current_style = new_style;
        }

        if (!cleanView) {
          const has_meta =
            Object.keys(active_ins).length > 0 ||
            Object.keys(active_del).length > 0 ||
            active_comments.size > 0 ||
            Object.keys(active_fmt).length > 0;
          if (has_meta) {
            deferred_meta_states.push([
              { ...active_ins },
              { ...active_del },
              new Set(active_comments),
              { ...active_fmt },
            ]);
          }

          let should_defer = false;
          const has_any_meta =
            Object.keys(active_ins).length > 0 ||
            Object.keys(active_del).length > 0 ||
            Object.keys(active_fmt).length > 0 ||
            active_comments.size > 0;

          if (has_any_meta) {
            let j = i + 1;
            let next_has_meta = false;
            let temp_ins = Object.keys(active_ins).length;
            let temp_del = Object.keys(active_del).length;
            let temp_fmt = Object.keys(active_fmt).length;
            const temp_comments = new Set(active_comments);

            while (j < items.length) {
              const next_item = items[j];
              if (next_item instanceof Run) {
                if (!get_run_text(next_item)) {
                  j++;
                  continue;
                }
                if (
                  temp_ins > 0 ||
                  temp_del > 0 ||
                  temp_fmt > 0 ||
                  temp_comments.size > 0
                )
                  next_has_meta = true;
                break;
              } else {
                const ev = next_item as DocxEvent;
                if (ev.type === "ins_start") temp_ins++;
                else if (ev.type === "ins_end")
                  temp_ins = Math.max(0, temp_ins - 1);
                else if (ev.type === "del_start") temp_del++;
                else if (ev.type === "del_end")
                  temp_del = Math.max(0, temp_del - 1);
                else if (ev.type === "fmt_start") temp_fmt++;
                else if (ev.type === "fmt_end")
                  temp_fmt = Math.max(0, temp_fmt - 1);
                else if (ev.type === "start") temp_comments.add(ev.id);
                else if (ev.type === "end") temp_comments.delete(ev.id);
              }
              j++;
            }
            if (next_has_meta) should_defer = true;
          }

          if (!should_defer && deferred_meta_states.length > 0) {
            const meta_block = _build_merged_meta_block(
              deferred_meta_states,
              comments_map,
            );
            if (meta_block) {
              if (pending_text) {
                parts.push(
                  `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
                );
                pending_text = "";
                current_wrappers = ["", ""];
                current_style = ["", ""];
              }
              parts.push(`{>>${meta_block}<<}`);
            }
            deferred_meta_states.length = 0; // clear
          }
        }
      }
    } else if (isSdtEvent(item)) {
      // Content-control boundary. Handled BEFORE the DocxEvent branch and as a
      // distinct shape rather than another `ev.type` case: DocxEvent is a
      // four-string record, and an anchor needs the whole SdtInfo (flags,
      // class, placeholder text), so folding it in would have meant a parallel
      // out-of-band lookup at every consumer.
      //
      // Heading content has begun: an anchor is addressable text, so the
      // leading-whitespace strip stops here exactly as it does for every other
      // non-Run event.
      leading_strip_active = false;
      // Anchor tokens are structural and must NOT be swept into the emphasis /
      // CriticMarkup group being accumulated: a control inside a bold span
      // would otherwise emit `**{#cc:3}text**`, and every marker-stripping
      // pass would mangle the token.
      if (pending_text) {
        parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);
        pending_text = "";
        current_wrappers = ["", ""];
        current_style = ["", ""];
      }
      const info = item.info;
      if (item.type === "sdt_start") {
        parts.push(openToken(info));
        // The placeholder bubble is virtual chrome: raw view only, dropped in
        // the clean view because an unfilled field has no accepted-state
        // content (spec §6).
        if (!cleanView && info.showingPlaceholder && info.placeholderText) {
          parts.push(`{>>placeholder: ${info.placeholderText}<<}`);
        }
      } else if (item.type === "checkbox_start") {
        // Checkbox tokens persist in the clean view: they are structural
        // (spec §6), like the anchors, not commentary.
        parts.push(CHECKBOX_OPEN);
      } else if (item.type === "checkbox_mark") {
        // Fallback only — the mark is normally a real run emitted by the
        // traversal, and arrives through the Run branch.
        parts.push(checkboxMark(info));
      } else if (item.type === "checkbox_end") {
        parts.push(CHECKBOX_CLOSE);
      } else {
        parts.push(closeToken(info));
      }
    } else {
      const ev = item as DocxEvent;
      leading_strip_active = false;

      if (
        ![
          "ins_start",
          "ins_end",
          "del_start",
          "del_end",
          "fmt_start",
          "fmt_end",
        ].includes(ev.type)
      ) {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
      }

      if (ev.type === "start") active_comments.add(ev.id);
      else if (ev.type === "end") active_comments.delete(ev.id);
      else if (ev.type === "ins_start") active_ins[ev.id] = ev;
      else if (ev.type === "ins_end") delete active_ins[ev.id];
      else if (ev.type === "del_start") active_del[ev.id] = ev;
      else if (ev.type === "del_end") delete active_del[ev.id];
      else if (ev.type === "fmt_start") active_fmt[ev.id] = ev;
      else if (ev.type === "fmt_end") delete active_fmt[ev.id];
      else if (ev.type === "image") {
        // Read-only image marker (QA 2026-07-18 M5); hidden with its run in
        // clean view when it sits inside an active tracked deletion.
        if (!(cleanView && Object.keys(active_del).length > 0)) {
          const alt = (ev.date || "image")
            .replace(/\]/g, ")")
            .replace(/\n/g, " ");
          parts.push(`![${alt}](docx-image:${ev.id})`);
        }
      } else if (ev.type === "footnote" || ev.type === "endnote") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push(`[^${ev.type === "footnote" ? "fn" : "en"}-${ev.id}]`);
      } else if (ev.type === "hyperlink_start") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push("[");
      } else if (ev.type === "hyperlink_end") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push(`](${ev.date})`);
      } else if (ev.type === "xref_start") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push("[~");
      } else if (ev.type === "xref_end") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push(`~](#${ev.id})`);
      } else if (ev.type === "bookmark") {
        if (pending_text) {
          parts.push(
            `${current_wrappers[0]}${pending_text}${current_wrappers[1]}`,
          );
          pending_text = "";
          current_wrappers = ["", ""];
          current_style = ["", ""];
        }
        parts.push(`{#${ev.id}}`);
      }
    }
  }

  if (pending_text)
    parts.push(`${current_wrappers[0]}${pending_text}${current_wrappers[1]}`);

  if (deferred_meta_states.length > 0) {
    const meta_block = _build_merged_meta_block(
      deferred_meta_states,
      comments_map,
    );
    if (meta_block) parts.push(`{>>${meta_block}<<}`);
  }

  return parts.join("");
}

function _get_wrappers(
  ins: any,
  del: any,
  comments: Set<string>,
  fmt: any,
): [string, string] {
  if (Object.keys(del).length > 0) return ["{--", "--}"];
  if (Object.keys(ins).length > 0) return ["{++", "++}"];
  if (comments.size > 0 || Object.keys(fmt).length > 0) return ["{==", "==}"];
  return ["", ""];
}

function _build_merged_meta_block(
  states_list: any[],
  comments_map: any,
): string {
  const change_lines: string[] = [];
  const comment_lines: string[] = [];
  const seen_sigs = new Set<string>();

  // Ids of one resolution group (a replacement's contiguous same-author
  // del+ins pair) must not read as independently resolvable — either side
  // resolves the whole group (QA 2026-07-19 ADEU-QA-004).
  const pair_map = compute_change_pair_map(states_list);
  const pairSuffix = (uid: string): string =>
    pair_map[uid] ? ` (pairs with ${pair_map[uid]})` : "";

  for (const [ins_map, del_map, comments_set, fmt_map] of states_list) {
    for (const [uid, meta] of Object.entries(
      ins_map as Record<string, DocxEvent>,
    )) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(
          `[${sig} insert] ${meta.author || "Unknown"}${pairSuffix(uid)}`,
        );
        seen_sigs.add(sig);
      }
    }
    for (const [uid, meta] of Object.entries(
      del_map as Record<string, DocxEvent>,
    )) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(
          `[${sig} delete] ${meta.author || "Unknown"}${pairSuffix(uid)}`,
        );
        seen_sigs.add(sig);
      }
    }
    for (const [uid, meta] of Object.entries(
      fmt_map as Record<string, DocxEvent>,
    )) {
      const sig = `Chg:${uid}`;
      if (!seen_sigs.has(sig)) {
        change_lines.push(`[${sig} format] ${meta.author || "Unknown"}`);
        seen_sigs.add(sig);
      }
    }

    // Threaded Comment Resolution Tree
    const children_map: Record<string, string[]> = {};
    for (const [c_id, data] of Object.entries(
      comments_map as Record<string, any>,
    )) {
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
      comment_lines.push(`${header}: ${escape_critic_tokens(data.text)}`);
      seen_sigs.add(sig);

      if (children_map[cid]) {
        const children = children_map[cid].sort((a, b) =>
          (comments_map[a]?.date || "").localeCompare(
            comments_map[b]?.date || "",
          ),
        );
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

  return [...change_lines, ...comment_lines].join("\n");
}
