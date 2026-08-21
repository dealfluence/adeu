import { qn, findChild, findChildren, findAllDescendants } from "../docx/dom.js";
import {
  BALLOT_GLYPHS,
  BlockSdt,
  checkboxMark,
  isAnchored,
  type SdtEvent,
  type SdtInfo,
} from "./content-controls.js";
import {
  Paragraph,
  Table,
  Run,
  NotesPart,
  FootnoteItem,
  DocxEvent,
} from "../docx/primitives.js";

export const QN_W_P = "w:p";
export const QN_W_R = "w:r";
export const QN_W_T = "w:t";
export const QN_W_DELTEXT = "w:delText";
export const QN_W_TAB = "w:tab";
export const QN_W_BR = "w:br";
export const QN_W_CR = "w:cr";

/**
 * A page break projects as U+000C FORM FEED — the conventional plain-text page
 * separator — rather than as a newline, so that pagination can find manual
 * breaks without putting markup in the character stream an LLM reads.
 *
 * Must stay identical to python's `adeu.utils.docx.PAGE_BREAK_TOKEN`. Until
 * 2026-08-21 (CC-10) the engines disagreed here: node emitted "\n" while python
 * emitted 22 characters of literal `<w:br w:type="page"/>` markup.
 */
export const PAGE_BREAK_TOKEN = "\f";
export const QN_W_RPR = "w:rPr";
export const QN_W_RPRCHANGE = "w:rPrChange";
export const QN_W_COMMENTREFERENCE = "w:commentReference";
export const QN_W_FOOTNOTEREFERENCE = "w:footnoteReference";
export const QN_W_ENDNOTEREFERENCE = "w:endnoteReference";
export const QN_W_FLDCHAR = "w:fldChar";
export const QN_W_FLDCHARTYPE = "w:fldCharType";
export const QN_W_INSTRTEXT = "w:instrText";
export const QN_W_INS = "w:ins";
export const QN_W_DEL = "w:del";
export const QN_W_ID = "w:id";
export const QN_W_AUTHOR = "w:author";
export const QN_W_DATE = "w:date";
export const QN_W_COMMENTRANGESTART = "w:commentRangeStart";
export const QN_W_COMMENTRANGEEND = "w:commentRangeEnd";
export const QN_W_HYPERLINK = "w:hyperlink";
export const QN_R_ID = "r:id";
export const QN_W_FLDSIMPLE = "w:fldSimple";
export const QN_W_INSTR = "w:instr";
export const QN_W_BOOKMARKSTART = "w:bookmarkStart";
export const QN_W_NAME = "w:name";
export const QN_W_SDT = "w:sdt";
export const QN_W_SMARTTAG = "w:smartTag";
export const QN_W_SDTCONTENT = "w:sdtContent";
export const QN_W_B = "w:b";
export const QN_W_I = "w:i";
export const QN_W_VAL = "w:val";
export const QN_W_PPR = "w:pPr";
export const QN_W_PSTYLE = "w:pStyle";
export const QN_W_OUTLINELVL = "w:outlineLvl";
export const QN_W_NUMPR = "w:numPr";
export const QN_W_NUMID = "w:numId";
export const QN_W_ILVL = "w:ilvl";
export const QN_W_DRAWING = "w:drawing";
export const QN_W_OBJECT = "w:object";
export const QN_W_PICT = "w:pict";
export const QN_WP_DOCPR = "wp:docPr";
export const QN_V_IMAGEDATA = "v:imagedata";
export const QN_O_TITLE = "o:title";
export const QN_W_TXBXCONTENT = "w:txbxContent";
export const QN_MC_ALTERNATECONTENT = "mc:AlternateContent";
export const QN_MC_CHOICE = "mc:Choice";
export const QN_MC_FALLBACK = "mc:Fallback";

// Disclosure cap for text projected out of a floating text box marker — long
// boxed content is truncated, the marker is a disclosure, not a full view.
const _TEXTBOX_DISCLOSURE_CAP = 300;

/**
 * Concatenated, whitespace-normalized visible text of every w:txbxContent
 * under `graphic_el` (a w:drawing / w:object / w:pict), truncated to
 * _TEXTBOX_DISCLOSURE_CAP chars. Empty string when the shape holds no text
 * (plain images, watermark textpaths).
 */
function _textbox_text(graphic_el: Element): string {
  const parts: string[] = [];
  for (const tc of findAllDescendants(graphic_el, QN_W_TXBXCONTENT)) {
    for (const t of findAllDescendants(tc, QN_W_T)) {
      if (t.textContent) parts.push(t.textContent);
    }
  }
  let text = parts.join(" ").split(/\s+/).filter(Boolean).join(" ");
  if (text.length > _TEXTBOX_DISCLOSURE_CAP) {
    text = text.slice(0, _TEXTBOX_DISCLOSURE_CAP).trimEnd() + "…";
  }
  return text;
}

/**
 * Projects a read-only marker for a graphic run child (w:drawing, w:object
 * or w:pict), rendered downstream as ![alt](docx-image:id).
 *
 * Floating text boxes get their text quoted in the alt ("Text box: …") so
 * boxed obligations are DISCLOSED instead of silently invisible
 * (QA 2026-07-23 customer C4). Textpath-only VML shapes (watermarks) still
 * project nothing — sanitize's watermark audit reports those.
 */
function* _graphic_marker_events(child: Element): Generator<DocxEvent> {
  const tag = child.tagName;
  if (tag === QN_W_DRAWING || tag === QN_W_OBJECT) {
    // Inline image/object: project a read-only marker so the agent can
    // see that a material element exists here (QA 2026-07-18 M5).
    const doc_pr = findAllDescendants(child, QN_WP_DOCPR)[0] || null;
    let alt = "";
    let img_id = "0";
    if (doc_pr) {
      alt = doc_pr.getAttribute("descr") || doc_pr.getAttribute("title") || "";
      img_id = doc_pr.getAttribute("id") || "0";
    }
    const boxed = _textbox_text(child);
    if (boxed) {
      alt = alt ? `Text box (${alt}): ${boxed}` : `Text box: ${boxed}`;
    }
    yield { type: "image", id: img_id, date: alt };
  } else if (tag === QN_W_PICT) {
    // Legacy VML. Actual images (v:imagedata) and text boxes get a marker;
    // textpath-only shapes (watermarks) stay silent.
    const imagedata = findAllDescendants(child, QN_V_IMAGEDATA)[0] || null;
    let alt = imagedata ? imagedata.getAttribute(QN_O_TITLE) || "" : "";
    const boxed = _textbox_text(child);
    if (boxed) {
      alt = alt ? `Text box (${alt}): ${boxed}` : `Text box: ${boxed}`;
    }
    if (boxed || imagedata) {
      yield { type: "image", id: "vml", date: alt };
    }
  }
}

const _CUSTOM_HEADING_NAME_RE = /Heading[ ]?([1-6])(?![0-9])/;

/**
 * Resolves the DocxPackage owning `obj`, whatever wrapper it is (Part,
 * DocumentObject, Cell/Row/Table/NotesPart chains). Returns null when no
 * package is reachable.
 */
function _resolve_package(obj: any): any {
  let cur = obj;
  const seen = new Set<any>();
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    if (cur.package) return cur.package;
    if (cur.pkg) return cur.pkg;
    if (cur.part && (cur.part.package || cur.part.pkg)) {
      return cur.part.package || cur.part.pkg;
    }
    cur = cur._parent || cur.part || null;
  }
  return null;
}

export function _get_style_cache(
  part: any,
): [Record<string, any>, string | null] {
  const pkg = part.package || part.pkg || (part.part ? part.part.pkg : null);
  if (pkg && pkg._adeu_style_cache) {
    return pkg._adeu_style_cache;
  }

  const cache: Record<string, any> = {};
  let default_pstyle: string | null = null;
  const raw_styles: Record<string, any> = {};

  const stylesPart = pkg?.getPartByPath("word/styles.xml");
  if (!stylesPart) {
    const result: [Record<string, any>, string | null] = [cache, null];
    if (pkg) pkg._adeu_style_cache = result;
    return result;
  }

  const styles = findAllDescendants(stylesPart._element, "w:style");
  for (const s of styles) {
    const s_id = s.getAttribute("w:styleId");
    if (!s_id) continue;

    const s_type = s.getAttribute("w:type");
    const is_default =
      s.getAttribute("w:default") === "1" ||
      s.getAttribute("w:default") === "true";

    if (s_type === "paragraph" && is_default) default_pstyle = s_id;

    const name_el = findChild(s, "w:name");
    let name = name_el ? name_el.getAttribute("w:val") : s_id;

    if (name && typeof name === "string") {
      if (name.toLowerCase().startsWith("heading")) {
        name = name.replace(/^heading/i, "Heading");
      } else if (name.toLowerCase() === "title") {
        name = "Title";
      }
    }

    const based_on_el = findChild(s, "w:basedOn");
    const based_on = based_on_el ? based_on_el.getAttribute("w:val") : null;

    let outline_lvl: number | null = null;
    let num_id: string | null = null;
    let num_ilvl: number | null = null;
    const pPr = findChild(s, "w:pPr");
    if (pPr) {
      const oLvl = findChild(pPr, "w:outlineLvl");
      if (oLvl) {
        const val = oLvl.getAttribute("w:val");
        if (val && /^\d+$/.test(val)) outline_lvl = parseInt(val, 10);
      }
      // Style-level list binding: Word's built-in "List Bullet" /
      // "List Number" styles carry <w:numPr> in styles.xml, not on the
      // paragraph. Without this, style-based lists project as plain
      // paragraphs and the agent loses ordered-vs-unordered semantics
      // (QA 2026-07-18 M4).
      const numPr = findChild(pPr, "w:numPr");
      if (numPr) {
        const numId_el = findChild(numPr, "w:numId");
        if (numId_el) {
          const n_val = numId_el.getAttribute("w:val");
          if (n_val && n_val !== "0") num_id = n_val;
        }
        const ilvl_el = findChild(numPr, "w:ilvl");
        if (ilvl_el) {
          const i_val = ilvl_el.getAttribute("w:val");
          if (i_val && /^\d+$/.test(i_val)) num_ilvl = parseInt(i_val, 10);
        }
      }
    }

    let bold: boolean | null = null;
    const rPr = findChild(s, "w:rPr");
    if (rPr) {
      const b = findChild(rPr, "w:b");
      if (b) {
        const val = b.getAttribute("w:val");
        bold = val !== "0" && val !== "false" && val !== "off";
      }
    }

    raw_styles[s_id] = {
      name,
      based_on,
      outline_level: outline_lvl,
      bold,
      num_id,
      num_ilvl,
    };
  }

  const resolve_style = (s_id: string, visited: Set<string>): any => {
    if (cache[s_id]) return cache[s_id];
    if (visited.has(s_id) || !raw_styles[s_id])
      return {
        name: s_id,
        outline_level: null,
        bold: false,
        num_id: null,
        num_ilvl: null,
      };

    visited.add(s_id);
    const raw = raw_styles[s_id];
    const based_on_id = raw.based_on;

    let o_lvl = raw.outline_level;
    let bold_val = raw.bold !== null ? raw.bold : false;
    let n_id = raw.num_id;
    let n_ilvl = raw.num_ilvl;

    if (based_on_id) {
      const parent = resolve_style(based_on_id, visited);
      if (o_lvl === null) o_lvl = parent.outline_level;
      if (raw.bold === null) bold_val = parent.bold;
      if (n_id === null) n_id = parent.num_id ?? null;
      if (n_ilvl === null) n_ilvl = parent.num_ilvl ?? null;
    }

    const resolved = {
      name: raw.name,
      outline_level: o_lvl,
      bold: bold_val,
      num_id: n_id,
      num_ilvl: n_ilvl,
    };
    cache[s_id] = resolved;
    return resolved;
  };

  for (const s_id in raw_styles) resolve_style(s_id, new Set());

  const result: [Record<string, any>, string | null] = [cache, default_pstyle];
  if (pkg) pkg._adeu_style_cache = result;
  return result;
}

/**
 * Parses word/numbering.xml once per package into
 * {numId: {ilvl: numFmt}} (e.g. {"5": {0: "decimal", 1: "lowerLetter"}}).
 *
 * Used to distinguish bullet lists from ordered lists in the projection
 * (QA 2026-07-18 M4). Missing part / malformed XML yields an empty cache,
 * which projects every list with the bullet marker (the historical default).
 */
export function _get_numbering_cache(
  part: any,
): Record<string, Record<number, string>> {
  const pkg = _resolve_package(part);
  if (!pkg) return {};
  if (pkg._adeu_numbering_cache) return pkg._adeu_numbering_cache;

  const cache: Record<string, Record<number, string>> = {};
  let numbering_root: Element | null = null;
  try {
    const numberingPart = (pkg.parts || []).find((p: any) =>
      String(p.partname).endsWith("/numbering.xml"),
    );
    if (numberingPart) numbering_root = numberingPart._element;
  } catch {
    numbering_root = null;
  }

  if (numbering_root) {
    const abstract_fmts: Record<string, Record<number, string>> = {};
    for (const abstract of findAllDescendants(numbering_root, "w:abstractNum")) {
      const a_id = abstract.getAttribute("w:abstractNumId");
      if (a_id === null) continue;
      const lvl_map: Record<number, string> = {};
      for (const lvl of findAllDescendants(abstract, "w:lvl")) {
        const ilvl_val = lvl.getAttribute("w:ilvl");
        const fmt_el = findChild(lvl, "w:numFmt");
        if (ilvl_val !== null && /^-?\d+$/.test(ilvl_val) && fmt_el) {
          const fmt = fmt_el.getAttribute("w:val");
          if (fmt) lvl_map[parseInt(ilvl_val, 10)] = fmt;
        }
      }
      abstract_fmts[a_id] = lvl_map;
    }

    for (const num of findAllDescendants(numbering_root, "w:num")) {
      const n_id = num.getAttribute("w:numId");
      const a_ref = findChild(num, "w:abstractNumId");
      if (n_id === null || !a_ref) continue;
      const a_id = a_ref.getAttribute("w:val");
      if (a_id !== null && abstract_fmts[a_id] !== undefined) {
        cache[n_id] = abstract_fmts[a_id];
      }
    }
  }

  pkg._adeu_numbering_cache = cache;
  return cache;
}

/**
 * Markdown marker for a list paragraph: '* ' for bullets, '1. ' for every
 * numbered format (Markdown renderers renumber sequentially). Unknown
 * numbering (no numbering.xml entry) keeps the historical '* '.
 */
export function get_list_marker(
  paragraph_part: any,
  num_id: string | null,
  ilvl: number,
): string {
  let fmt: string | null = null;
  if (num_id !== null && num_id !== undefined) {
    const lvl_map = _get_numbering_cache(paragraph_part)[num_id];
    if (lvl_map && Object.keys(lvl_map).length > 0) {
      fmt = lvl_map[ilvl] !== undefined ? lvl_map[ilvl] : null;
      if (fmt === null) {
        // Fall back to the nearest defined level at or below ilvl.
        for (let lookup = ilvl; lookup >= 0; lookup--) {
          if (lvl_map[lookup] !== undefined) {
            fmt = lvl_map[lookup];
            break;
          }
        }
      }
    }
  }
  if (fmt !== null && fmt !== "bullet") return "1. ";
  return "* ";
}

function _detect_heading_level_from_name(name: string): number | null {
  if (!name) return null;
  const match = name.match(_CUSTOM_HEADING_NAME_RE);
  return match ? parseInt(match[1], 10) : null;
}

/**
 * True when the paragraph's own break is a pending tracked deletion
 * (`<w:del>` inside pPr/rPr) — accepting it removes the paragraph container.
 *
 * Twin of python `adeu.utils.docx.paragraph_mark_is_deleted`. Shared by both
 * Virtual Text producers: `_extract_blocks` drops such a paragraph from the
 * clean view when nothing visible survives inside it, and
 * `DocumentMapper._map_blocks` must drop it identically. Keeping ONE predicate
 * is what keeps the twins byte-identical.
 */
export function paragraph_mark_is_deleted(p_element: any): boolean {
  const pPr = findChild(p_element, QN_W_PPR);
  if (!pPr) return false;
  const rPr = findChild(pPr, QN_W_RPR);
  return !!rPr && !!findChild(rPr, QN_W_DEL);
}

export function is_native_heading(
  paragraph: Paragraph,
  style_cache?: Record<string, any>,
  default_pstyle?: string | null,
): boolean {
  if (!style_cache) {
    [style_cache, default_pstyle] = _get_style_cache(
      paragraph._parent.part || paragraph._parent,
    );
  }
  const pPr = findChild(paragraph._element, QN_W_PPR);

  if (pPr) {
    const oLvl = findChild(pPr, QN_W_OUTLINELVL);
    if (oLvl) {
      const val = oLvl.getAttribute(QN_W_VAL);
      if (val && /^\d+$/.test(val)) {
        const lvl = parseInt(val, 10);
        if (lvl >= 0 && lvl <= 8) return true;
      }
    }
  }

  let style_id = default_pstyle;
  if (pPr) {
    const pStyle = findChild(pPr, QN_W_PSTYLE);
    if (pStyle) style_id = pStyle.getAttribute(QN_W_VAL) || default_pstyle;
  }

  const style_info = style_id && style_cache ? style_cache[style_id] : null;
  if (
    style_info &&
    style_info.outline_level !== null &&
    style_info.outline_level >= 0 &&
    style_info.outline_level <= 8
  ) {
    return true;
  }

  let style_name = style_info ? style_info.name : style_id; // FALLBACK TO ID
  if (style_name && typeof style_name === "string" && style_name.toLowerCase().startsWith("heading")) {
    style_name = style_name.replace(/^heading/i, "Heading");
  }
  if (style_name?.startsWith("Heading")) return true;
  if (style_name === "Title") return true;
  if (style_name && style_name !== "Normal") {
    if (_detect_heading_level_from_name(style_name) !== null) return true;
  }

  return false;
}
export function get_paragraph_prefix(
  paragraph: Paragraph,
  style_cache?: Record<string, any>,
  default_pstyle?: string | null,
): string {
  if (!style_cache) {
    [style_cache, default_pstyle] = _get_style_cache(
      paragraph._parent.part || paragraph._parent,
    );
  }
  const pPr = findChild(paragraph._element, QN_W_PPR);

  if (pPr) {
    const oLvl = findChild(pPr, QN_W_OUTLINELVL);
    if (oLvl) {
      const val = oLvl.getAttribute(QN_W_VAL);
      if (val && /^\d+$/.test(val)) {
        const lvl = parseInt(val, 10);
        if (lvl >= 0 && lvl <= 8) return "#".repeat(lvl + 1) + " ";
      }
    }
  }

  let style_id = default_pstyle;
  if (pPr) {
    const pStyle = findChild(pPr, QN_W_PSTYLE);
    if (pStyle) style_id = pStyle.getAttribute(QN_W_VAL) || default_pstyle;
  }

  const style_info = style_id && style_cache ? style_cache[style_id] : null;
  if (
    style_info &&
    style_info.outline_level !== null &&
    style_info.outline_level >= 0 &&
    style_info.outline_level <= 8
  ) {
    return "#".repeat(style_info.outline_level + 1) + " ";
  }

  let style_name = style_info ? style_info.name : style_id; // FALLBACK TO ID
  if (style_name && typeof style_name === "string" && style_name.toLowerCase().startsWith("heading")) {
    style_name = style_name.replace(/^heading/i, "Heading");
  }
  if (style_name?.startsWith("Heading")) {
    const match = style_name.replace("Heading", "").trim();
    if (/^\d+$/.test(match)) return "#".repeat(parseInt(match, 10)) + " ";
  }

  if (style_name === "Title") return "# ";

  // Check for List Formatting (direct paragraph numPr first, then the
  // style chain — Word's built-in List Bullet/List Number styles keep their
  // numPr in styles.xml, QA 2026-07-18 M4).
  let list_num_id: string | null = null;
  let list_ilvl: number | null = null;
  let numbering_disabled = false;
  if (pPr) {
    const numPr = findChild(pPr, QN_W_NUMPR);
    if (numPr) {
      const numId = findChild(numPr, QN_W_NUMID);
      if (numId) {
        const val = numId.getAttribute(QN_W_VAL);
        if (val === "0") {
          // ECMA-376 §17.9.15: a direct numId of 0 REMOVES the numbering a
          // style would otherwise apply.
          numbering_disabled = true;
        } else if (val) {
          list_num_id = val;
          const ilvl = findChild(numPr, QN_W_ILVL);
          if (ilvl) {
            const valAttr = ilvl.getAttribute(QN_W_VAL);
            if (valAttr !== null && /^\d+$/.test(valAttr)) {
              list_ilvl = parseInt(valAttr, 10);
            }
          }
        }
      }
    }
  }
  if (list_num_id === null && !numbering_disabled && style_info) {
    const style_num_id = style_info.num_id;
    if (style_num_id) {
      list_num_id = style_num_id;
      if (list_ilvl === null) {
        list_ilvl =
          style_info.num_ilvl !== undefined ? style_info.num_ilvl : null;
      }
    }
  }
  if (list_num_id !== null) {
    const level = list_ilvl !== null ? list_ilvl : 0;
    const marker = get_list_marker(
      paragraph._parent ? paragraph._parent.part || paragraph._parent : null,
      list_num_id,
      level,
    );
    return "    ".repeat(level) + marker;
  }

  if (style_name && style_name !== "Normal") {
    const custom_level = _detect_heading_level_from_name(style_name);
    if (custom_level !== null) return "#".repeat(custom_level) + " ";
  }

  if (!style_name || style_name === "Normal") {
    let is_inside_tc = false;
    let curr: Element | null = paragraph._element;
    while (curr) {
      if (curr.tagName === "w:tc") {
        is_inside_tc = true;
        break;
      }
      curr = curr.parentNode as Element | null;
    }
    if (is_inside_tc) {
      return "";
    }

    const text = paragraph.text.trim();
    if (text && text.length < 100 && text === text.toUpperCase()) {
      let is_bold = false;
      if (style_info?.bold) {
        is_bold = true;
      } else {
        const runs = findAllDescendants(paragraph._element, QN_W_R);
        for (const r of runs) {
          const tList = findAllDescendants(r, QN_W_T);
          const tText = tList.map((t) => t.textContent || "").join("");
          if (tText.trim()) {
            const rPr_run = findChild(r, QN_W_RPR);
            if (rPr_run) {
              const b = findChild(rPr_run, QN_W_B);
              if (
                b &&
                b.getAttribute(QN_W_VAL) !== "0" &&
                b.getAttribute(QN_W_VAL) !== "false"
              ) {
                is_bold = true;
              }
            }
            break;
          }
        }
      }
      if (is_bold) return "## ";
    }
  }

  return "";
}

export function is_heading_paragraph(
  paragraph: Paragraph,
  style_cache?: Record<string, any>,
  default_pstyle?: string | null,
): boolean {
  const prefix = get_paragraph_prefix(paragraph, style_cache, default_pstyle);
  if (!prefix) return false;
  const stripped = prefix.trimEnd();
  return stripped.length > 0 && stripped === "#".repeat(stripped.length);
}

export function get_run_style_markers(
  run: Run,
  is_heading: boolean | null = null,
): [string, string] {
  // The checkbox mark is chrome, not prose: emphasis on it would project
  // `[**x**]` and hand every marker-stripping pass something to mangle.
  if (run.projTextOverride !== undefined) return ["", ""];
  let prefix = "";
  let suffix = "";

  const rPr = findChild(run._element, QN_W_RPR);
  let is_bold = false;
  let is_italic = false;

  if (rPr) {
    const b = findChild(rPr, QN_W_B);
    if (
      b &&
      b.getAttribute(QN_W_VAL) !== "0" &&
      b.getAttribute(QN_W_VAL) !== "false"
    )
      is_bold = true;

    const i = findChild(rPr, QN_W_I);
    if (
      i &&
      i.getAttribute(QN_W_VAL) !== "0" &&
      i.getAttribute(QN_W_VAL) !== "false"
    )
      is_italic = true;
  }

  if (is_heading === null) {
    const parent = run._parent;
    is_heading =
      parent instanceof Paragraph ? is_native_heading(parent) : false;
  }

  if (is_bold && !is_heading) {
    prefix += "**";
    suffix = "**" + suffix;
  }

  if (is_italic) {
    prefix += "_";
    suffix = "_" + suffix;
  }

  return [prefix, suffix];
}

/**
 * Splits `text` into [leading_ws, core, trailing_ws]. Emphasis markers must
 * wrap only the core: `**The Supplier **` (a bold run with a trailing space)
 * is malformed Markdown — CommonMark requires the closing delimiter to hug
 * non-whitespace — and it poisons every downstream CriticMarkup consumer
 * (QA 2026-07-19 F-03/F-10). Fully-whitespace text yields ["", "", text] so
 * callers skip the markers entirely.
 */
export function split_boundary_whitespace(
  text: string,
): [string, string, string] {
  const core = text.trim();
  if (!core) return ["", "", text];
  const lead_len = text.length - text.trimStart().length;
  return [
    text.substring(0, lead_len),
    text.substring(lead_len, lead_len + core.length),
    text.substring(lead_len + core.length),
  ];
}

/**
 * Maps each tracked-change id in a merged meta bubble to the OTHER ids of its
 * resolution group, rendered ready for the bubble line suffix
 * (`uid -> "Chg:2"` / `uid -> "Chg:2, Chg:3"`).
 *
 * A replacement is stored as a contiguous same-author <w:del> + <w:ins> pair
 * with two distinct w:id values, but both engines resolve such a group as ONE
 * unit — accepting or rejecting either side decides the whole replacement.
 * Projecting the two ids side by side with no linkage implied they were
 * independently resolvable (QA 2026-07-19 ADEU-QA-004); every meta-block
 * builder (Python/Node ingest + mapper) uses this map to annotate grouped
 * lines as `(pairs with Chg:N)`.
 *
 * Grouping mirrors the engines' `_get_paired_nodes` walk: consecutive ins/del
 * ids in bubble order group while the author stays the same; any state
 * carrying NO active ins/del (a comment- or format-only run between changes —
 * physical text separating the elements) breaks the group. `states_list`
 * entries are [ins_map, del_map, comments_set, fmt_map] snapshots in document
 * order, as accumulated by the ingest/mapper state machines.
 */
export function compute_change_pair_map(
  states_list: any[],
): Record<string, string> {
  const groups: Array<Array<[string, string]>> = [];
  let current: Array<[string, string]> = [];
  const seen_ids = new Set<string>();

  for (const [ins_map, del_map] of states_list) {
    const ins_entries = Object.entries(ins_map as Record<string, any>);
    const del_entries = Object.entries(del_map as Record<string, any>);
    if (ins_entries.length === 0 && del_entries.length === 0) {
      // A run with only comment/format meta sits between the tracked
      // elements: they are not siblings, the engine will not group them.
      if (current.length > 0) {
        groups.push(current);
        current = [];
      }
      continue;
    }
    const state_new: Array<[string, string]> = [];
    for (const [uid, meta] of ins_entries) {
      if (!seen_ids.has(uid)) {
        state_new.push([uid, (meta && meta.author) || "Unknown"]);
      }
    }
    for (const [uid, meta] of del_entries) {
      if (!seen_ids.has(uid)) {
        state_new.push([uid, (meta && meta.author) || "Unknown"]);
      }
    }
    for (const [uid, author] of state_new) {
      seen_ids.add(uid);
      if (current.length > 0 && current[current.length - 1][1] !== author) {
        groups.push(current);
        current = [];
      }
      current.push([uid, author]);
    }
  }
  if (current.length > 0) groups.push(current);

  const pair_map: Record<string, string> = {};
  for (const group of groups) {
    if (group.length < 2) continue;
    for (const [uid] of group) {
      pair_map[uid] = group
        .filter(([u]) => u !== uid)
        .map(([u]) => `Chg:${u}`)
        .join(", ");
    }
  }
  return pair_map;
}

export function apply_formatting_to_segments(
  text: string,
  prefix: string,
  suffix: string,
): string {
  if (!prefix && !suffix) return text;
  if (!text) return "";

  const wrap = (segment: string): string => {
    const [lead, core, trail] = split_boundary_whitespace(segment);
    if (!core) return segment;
    return `${lead}${prefix}${core}${suffix}${trail}`;
  };

  if (!text.includes("\n")) return wrap(text);

  const parts = text.split("\n");
  return parts.map((p) => (p ? wrap(p) : "")).join("\n");
}

/** Does this control contain a ballot-glyph run to hang the mark on? */
/**
 * The mark for a ballot run that sits inside a tracked revision.
 *
 * `undefined` when the run is not inside one, which is the ordinary case and
 * keeps `w14:checked` authoritative.
 */
function revisionBallotMark(rElement: Element, text: string): string | undefined {
  let node: any = rElement.parentNode;
  while (node) {
    const tag = node.tagName;
    if (tag === "w:ins" || tag === "w:del") {
      return text === "\u2611" || text === "\u2612" ? "x" : " ";
    }
    node = node.parentNode;
  }
  return undefined;
}

function hasBallotRun(sdtEl: Element): boolean {
  const content = findChild(sdtEl, QN_W_SDTCONTENT);
  if (!content) return false;
  for (const r of findAllDescendants(content, QN_W_R)) {
    let text = "";
    for (const t of findAllDescendants(r, QN_W_T)) text += t.textContent || "";
    if (BALLOT_GLYPHS.has(text)) return true;
  }
  return false;
}

/**
 * The checkbox control this run is the glyph of, or `undefined`.
 *
 * Gated on the run's text being a ballot glyph FIRST, which is what keeps this
 * affordable: the walk runs for roughly 7,700 runs in the largest corpus
 * document rather than for all 559,000 of them.
 *
 * The gate is also the correctness boundary. `odot_uic_drywell` carries two
 * bare `U+2610` runs in ordinary prose, and the nearest enclosing `w:sdt`
 * decides their fate: no control, or a control that is not a checkbox, means
 * the glyph is prose and stays a glyph.
 */
function enclosingCheckbox(
  rElement: Element,
  text: string,
  sdtInfos?: Map<Element, SdtInfo>,
): SdtInfo | undefined {
  if (!sdtInfos || !BALLOT_GLYPHS.has(text)) return undefined;
  let node = rElement.parentNode as Element | null;
  while (node && node.nodeType === 1) {
    if (node.tagName === QN_W_SDT) {
      const info = sdtInfos.get(node);
      return info && info.cls === "checkbox" ? info : undefined;
    }
    node = node.parentNode as Element | null;
  }
  return undefined;
}

export function get_run_text(run: Run): string {
  // A checkbox mark projects its own character, not the glyph in `w:t`.
  if (run.projTextOverride !== undefined) return run.projTextOverride;
  let text = "";
  for (let i = 0; i < run._element.childNodes.length; i++) {
    const child = run._element.childNodes[i] as Element;
    if (child.nodeType !== 1) continue;

    if (child.tagName === QN_W_T || child.tagName === QN_W_DELTEXT) {
      const raw = child.textContent || "";
      text += raw.replace(/\t/g, " ");
    } else if (child.tagName === QN_W_TAB || child.tagName === "w:ptab") {
      // w:ptab is an absolute-position tab; it separates content like w:tab.
      text += " ";
    } else if (child.tagName === "w:noBreakHyphen") {
      // A real hyphen glyph. Dropping it merged the words either side
      // ("e-mail" projected as "email"). w:softHyphen is deliberately NOT
      // handled: it is an optional break hint Word renders only when the line
      // actually breaks, so projecting nothing is correct. w:sym is likewise
      // still dropped — symbol fonts map glyphs into the Unicode private-use
      // area, so the code point alone does not identify the character; that
      // needs a font-aware decision (CC-1 owns checkbox glyphs).
      text += "-";
    } else if (child.tagName === QN_W_BR) {
      text +=
        child.getAttribute("w:type") === "page" ? PAGE_BREAK_TOKEN : "\n";
    } else if (child.tagName === QN_W_CR) {
      text += "\n";
    }
  }
  return text;
}

export function* iter_block_items(
  parent: any,
  emit_sdt = false,
): Generator<Paragraph | Table | FootnoteItem | BlockSdt> {
  const parent_elm = parent._element || parent.element || parent;

  if (parent.constructor.name === "NotesPart") {
    const tag = parent.note_type === "fn" ? "w:footnote" : "w:endnote";
    const notes = findAllDescendants(parent_elm, tag);
    for (const child of notes) {
      if (
        child.getAttribute("w:type") === "separator" ||
        child.getAttribute("w:type") === "continuationSeparator"
      )
        continue;
      // Word reserves non-positive note ids (-1 separator, 0 continuation
      // separator). Some generators omit the w:type attribute on them, so
      // filter by id as well — they must never surface as user footnotes
      // like "[^fn--1]:" (QA 2026-07-18 M6).
      const note_id = child.getAttribute("w:id");
      if (
        note_id !== null &&
        /^\s*[-+]?\d+\s*$/.test(note_id) &&
        parseInt(note_id, 10) <= 0
      )
        continue;
      yield new FootnoteItem(child, parent, parent.note_type);
    }
    return;
  }

  yield* _iter_block_children(parent_elm, parent, emit_sdt);
}

/**
 * Yields the block items among `parent_elm`'s children, descending into
 * block-level w:sdt content controls: Word renders w:sdtContent children as
 * ordinary flowed body text, so skipping them silently hides live document
 * content from every view (QA 2026-07-23 customer C4). Recursion covers
 * nested controls; group/repeating sections project their inner blocks.
 */
function* _iter_block_children(
  parent_elm: Element,
  parent: any,
  emit_sdt = false,
): Generator<Paragraph | Table | BlockSdt> {
  for (let i = 0; i < parent_elm.childNodes.length; i++) {
    const child = parent_elm.childNodes[i] as Element;
    if (child.nodeType !== 1) continue;

    if (child.tagName === QN_W_P) {
      yield new Paragraph(child, parent);
    } else if (child.tagName === "w:tbl") {
      yield new Table(child, parent);
    } else if (child.tagName === QN_W_SDT) {
      const sdt_content = findChild(child, QN_W_SDTCONTENT);
      if (sdt_content) {
        if (emit_sdt) {
          // Yield the control as ONE unit and do NOT descend: the consumer
          // recurses into sdtContent itself, exactly as it already does for a
          // Table. That is what makes a block-level control a single block
          // that can be wrapped in its token lines — paired boundary events
          // would instead have forced every consumer to grow a nesting stack
          // and to re-derive the block separators inside it.
          yield new BlockSdt(child);
        } else {
          yield* _iter_block_children(sdt_content, parent, emit_sdt);
        }
      }
    }
  }
}

export function* iter_document_parts(doc: any): Generator<any> {
  for (const [container] of iter_document_parts_with_kind(doc)) {
    yield container;
  }
}

/**
 * Like iter_document_parts, but yields [container, kind] where kind is one
 * of "header" / "body" / "footer" / "footnotes" / "endnotes".
 *
 * The kind sequence defines the document's structural part layout. The
 * projection flattens all parts into one string, so diff/apply need these
 * kinds to refuse (or correctly re-anchor) edits that would otherwise cross
 * an OPC part boundary — the QA 2026-07-18 C1 failure wrote a final body
 * paragraph into word/footer1.xml.
 */
/**
 * Every `w:sectPr` in document order: one per section-terminating paragraph
 * (`w:p/w:pPr/w:sectPr`), with the body-level `w:sectPr` last — the order
 * python-docx's `doc.sections` yields.
 */
function* _iter_sect_pr(doc: any): Generator<Element> {
  const body: Element = doc.element;
  for (let i = 0; i < body.childNodes.length; i++) {
    const child = body.childNodes[i] as Element;
    if (child.nodeType !== 1 || child.tagName !== QN_W_P) continue;
    const pPr = findChild(child, "w:pPr");
    const sectPr = pPr && findChild(pPr, "w:sectPr");
    if (sectPr) yield sectPr;
  }
  const bodySectPr = findChild(body, "w:sectPr");
  if (bodySectPr) yield bodySectPr;
}

/** `w:settings/w:evenAndOddHeaders` — the document-wide even/odd toggle. */
function _odd_and_even_pages(doc: any): boolean {
  const settings = doc.pkg.getPartByPath("word/settings.xml");
  if (!settings) return false;
  return findChild(settings._element, "w:evenAndOddHeaders") !== null;
}

/**
 * Header/footer parts a section actually references, in Word's own precedence
 * order: primary, then first-page (only when the section sets `w:titlePg`),
 * then even-page (only when the document sets `w:evenAndOddHeaders`).
 *
 * A section that carries no reference of a given type inherits the previous
 * section's — Word's "Link to Previous" — so skipping the absent reference is
 * exactly what stops inherited headers from projecting twice.
 *
 * This mirrors python `iter_document_parts_with_kind._iter_section_parts`.
 * Before this existed the TS port simply listed every header/footer part in
 * the package, which projected parts Word never renders: unreferenced orphans,
 * first-page headers in sections without `w:titlePg`, and even-page headers in
 * documents without `w:evenAndOddHeaders`.
 */
function* _iter_section_parts(
  doc: any,
  kind: "header" | "footer",
): Generator<any> {
  const refTag = kind === "header" ? "w:headerReference" : "w:footerReference";
  const oddAndEven = _odd_and_even_pages(doc);

  for (const sectPr of _iter_sect_pr(doc)) {
    const refs = findChildren(sectPr, refTag);
    const byType = (t: string) =>
      refs.find((r) => (r.getAttribute("w:type") || "default") === t);

    const resolve = (ref: Element | undefined): any => {
      if (!ref) return undefined;
      const rId = ref.getAttribute("r:id");
      if (!rId) return undefined;
      const rel = doc.part.rels.get(rId);
      if (!rel) return undefined;
      const target = rel.target.replace(/^\/?word\//, "").replace(/^\//, "");
      return doc.pkg.getPartByPath("word/" + target);
    };

    // 1. Primary
    const primary = resolve(byType("default"));
    if (primary) yield primary;

    // 2. First page — only when this section opts in.
    if (findChild(sectPr, "w:titlePg") !== null) {
      const first = resolve(byType("first"));
      if (first) yield first;
    }

    // 3. Even page — only when the document opts in.
    if (oddAndEven) {
      const even = resolve(byType("even"));
      if (even) yield even;
    }
  }
}

export function* iter_document_parts_with_kind(
  doc: any,
): Generator<[any, string]> {
  // 1. Headers
  for (const h of _iter_section_parts(doc, "header")) yield [h, "header"];

  // 2. Main Document Body
  yield [doc, "body"];

  // 3. Footers
  for (const f of _iter_section_parts(doc, "footer")) yield [f, "footer"];

  // 4. Notes
  const fnPart = doc.pkg.getPartByPath("word/footnotes.xml");
  const enPart = doc.pkg.getPartByPath("word/endnotes.xml");

  if (fnPart) yield [new NotesPart(fnPart, "fn"), "footnotes"];
  if (enPart) yield [new NotesPart(enPart, "en"), "endnotes"];
}

function _is_page_instr(instr: string): boolean {
  if (!instr) return false;
  const parts = instr.toUpperCase().trim().split(/\s+/);
  return parts.length > 0 && (parts[0] === "PAGE" || parts[0] === "NUMPAGES");
}

export function _get_part(parent: any): any {
  if (!parent) return null;
  if (parent.part) return parent.part;
  if (parent.pkg && parent.pkg.mainDocumentPart)
    return parent.pkg.mainDocumentPart;
  if (parent._parent) return _get_part(parent._parent);
  return null;
}

export function* iter_paragraph_content(
  paragraph: Paragraph,
  sdtInfos?: Map<any, SdtInfo>,
): Generator<Run | DocxEvent | SdtEvent> {
  let in_complex_field = false;
  let current_instr = "";
  let hide_result = false;
  // The content controls currently open around the walk position, outermost
  // first. Snapshotted onto every `Run` so consumers can answer "which
  // controls enclose this text" without an ancestor walk per run. Tracks every
  // control, anchored or not - see `Run.sdtStack`.
  const sdtStack: SdtInfo[] = [];

  function* process_run_element(
    r_element: Element,
  ): Generator<Run | DocxEvent | SdtEvent> {
    let c_id: string | null = null;
    const rPr = findChild(r_element, QN_W_RPR);
    if (rPr) {
      const rPrChange = findChild(rPr, QN_W_RPRCHANGE);
      if (rPrChange) {
        c_id = rPrChange.getAttribute(QN_W_ID);
        yield {
          type: "fmt_start",
          id: c_id!,
          author: rPrChange.getAttribute(QN_W_AUTHOR) || undefined,
          date: rPrChange.getAttribute(QN_W_DATE) || undefined,
        };
      }
    }

    for (let i = 0; i < r_element.childNodes.length; i++) {
      const child = r_element.childNodes[i] as Element;
      if (child.nodeType !== 1) continue;

      const tag = child.tagName;
      if (tag === QN_W_DRAWING || tag === QN_W_OBJECT || tag === QN_W_PICT) {
        // Read-only graphic marker, rendered as ![alt](docx-image:id);
        // floating text boxes disclose their text in the alt
        // (QA 2026-07-18 M5, QA 2026-07-23 customer C4).
        yield* _graphic_marker_events(child);
      } else if (tag === QN_MC_ALTERNATECONTENT) {
        // Modern floating shapes (Word 2010+ text boxes, images) arrive
        // wrapped in mc:AlternateContent; without this branch they project
        // NOTHING — not even a marker (QA 2026-07-23 customer C4). Prefer
        // the mc:Choice payload, fall back to mc:Fallback, and project
        // exactly one marker through the same path as bare drawings/picts.
        let payload: Element | null = null;
        for (const wrapper_tag of [QN_MC_CHOICE, QN_MC_FALLBACK]) {
          for (const wrapper of findChildren(child, wrapper_tag)) {
            payload =
              findChild(wrapper, QN_W_DRAWING) ||
              findChild(wrapper, QN_W_OBJECT) ||
              findChild(wrapper, QN_W_PICT);
            if (payload) break;
          }
          if (payload) break;
        }
        if (payload) {
          yield* _graphic_marker_events(payload);
        }
      } else if (tag === QN_W_COMMENTREFERENCE) {
        const ref_id = child.getAttribute(QN_W_ID);
        if (ref_id) yield { type: "ref", id: ref_id };
      } else if (tag === QN_W_FOOTNOTEREFERENCE) {
        const f_id = child.getAttribute(QN_W_ID);
        if (f_id) yield { type: "footnote", id: f_id };
      } else if (tag === QN_W_ENDNOTEREFERENCE) {
        const e_id = child.getAttribute(QN_W_ID);
        if (e_id) yield { type: "endnote", id: e_id };
      } else if (tag === QN_W_FLDCHAR) {
        const fld_type = child.getAttribute(QN_W_FLDCHARTYPE);
        if (fld_type === "begin") {
          in_complex_field = true;
          current_instr = "";
        } else if (fld_type === "separate") {
          if (_is_page_instr(current_instr)) hide_result = true;
          else {
            const parts = current_instr.trim().split(/\s+/);
            if (parts.length > 1 && parts[0] === "REF")
              yield { type: "xref_start", id: parts[1] };
          }
        } else if (fld_type === "end") {
          if (!hide_result) {
            const parts = current_instr.trim().split(/\s+/);
            if (parts.length > 1 && parts[0] === "REF")
              yield { type: "xref_end", id: parts[1] };
          }
          in_complex_field = false;
          current_instr = "";
          hide_result = false;
        }
      } else if (tag === QN_W_INSTRTEXT && in_complex_field && !hide_result) {
        current_instr += child.textContent || "";
      }
    }

    if (!hide_result) {
      const run = new Run(r_element, paragraph);
      // Snapshot, not the live array: the stack keeps mutating as the walk
      // continues, and a shared reference would leave every run reporting the
      // controls open at the END of the paragraph. Empty stacks share one
      // frozen array, which is the common case by a wide margin.
      if (sdtStack.length) run.sdtStack = sdtStack.slice();
      const cbInfo = enclosingCheckbox(r_element, get_run_text(run), sdtInfos);
      if (cbInfo) {
        // Spec §4, twin of the python branch. `[` and `]` are virtual chrome;
        // the mark is a REAL run-backed span. One character replaces one
        // character (U+2612 -> `x`), so no offset arithmetic downstream has to
        // learn about a width difference.
        //
        // Done HERE, at run emission, rather than in the `w:sdt` branch,
        // because a checkbox control is not always inline: 11 of
        // `odot_uic_drywell`'s 19 checkboxes wrap a whole `w:tc` (a checkbox
        // column in a form table), and that path never reaches the sdt branch.
        //
        // The mark normally comes from `w14:checked` (the settled value - see
        // checkboxMark), but a run inside a revision projects ITS OWN glyph
        // instead. A tracked toggle writes two glyph runs, an inserted new
        // state and a deleted old one, and the attribute can only describe
        // one of them: taking it for both rendered two identical boxes, a
        // toggle that appears to change nothing. Reading each revision run's
        // own glyph makes the pending change legible, and the clean view
        // keeps exactly one box because the deleted half never reaches it
        // (A4.6).
        const runText = get_run_text(run);
        run.projTextOverride = revisionBallotMark(r_element, runText) ?? checkboxMark(cbInfo);
        yield { type: "checkbox_start", info: cbInfo } as SdtEvent;
        yield run;
        yield { type: "checkbox_end", info: cbInfo } as SdtEvent;
      } else {
        yield run;
      }
    }
    if (c_id !== null) yield { type: "fmt_end", id: c_id };
  }

  function* traverse_node(node: Element): Generator<Run | DocxEvent | SdtEvent> {
    for (let i = 0; i < node.childNodes.length; i++) {
      const child = node.childNodes[i] as Element;
      if (child.nodeType !== 1) continue;

      const tag = child.tagName;
      if (tag === QN_W_R) yield* process_run_element(child);
      else if (tag === QN_W_INS) {
        const i_id = child.getAttribute(QN_W_ID)!;
        yield {
          type: "ins_start",
          id: i_id,
          author: child.getAttribute(QN_W_AUTHOR) || undefined,
          date: child.getAttribute(QN_W_DATE) || undefined,
        };
        yield* traverse_node(child);
        yield { type: "ins_end", id: i_id };
      } else if (tag === QN_W_DEL) {
        const d_id = child.getAttribute(QN_W_ID)!;
        yield {
          type: "del_start",
          id: d_id,
          author: child.getAttribute(QN_W_AUTHOR) || undefined,
          date: child.getAttribute(QN_W_DATE) || undefined,
        };
        yield* traverse_node(child);
        yield { type: "del_end", id: d_id };
      } else if (tag === QN_W_COMMENTRANGESTART)
        yield { type: "start", id: child.getAttribute(QN_W_ID)! };
      else if (tag === QN_W_COMMENTRANGEEND)
        yield { type: "end", id: child.getAttribute(QN_W_ID)! };
      else if (tag === QN_W_HYPERLINK) {
        const rId = child.getAttribute(QN_R_ID) || child.getAttribute("id");
        let url = "";
        const part = _get_part(paragraph._parent);
        if (rId && part) {
          const rel = part.rels.get(rId);
          if (rel && rel.isExternal) url = rel.target;
        }
        if (url) yield { type: "hyperlink_start", id: rId!, date: url };
        yield* traverse_node(child);
        if (url) yield { type: "hyperlink_end", id: rId!, date: url };
      } else if (tag === QN_W_FLDSIMPLE) {
        const instr = child.getAttribute(QN_W_INSTR) || "";
        const parts = instr.trim().split(/\s+/);
        const target = parts.length > 1 && parts[0] === "REF" ? parts[1] : "";
        if (target) yield { type: "xref_start", id: target };
        yield* traverse_node(child);
        if (target) yield { type: "xref_end", id: target };
      } else if (tag === QN_W_BOOKMARKSTART) {
        const b_name = child.getAttribute(QN_W_NAME);
        if (b_name && (!b_name.startsWith("_") || b_name.startsWith("_Ref")))
          yield { type: "bookmark", id: b_name };
      } else if (
        tag === QN_W_SDT ||
        tag === QN_W_SMARTTAG ||
        tag === QN_W_SDTCONTENT
      ) {
        // Content controls were historically transparent here: the boundary
        // was erased and only the contents projected. When the caller supplies
        // the ordinal map (ingest and the mapper do; outline/sanitize
        // deliberately do not) the boundary becomes visible as a pair of
        // events, and an ANCHORED control's contents are bracketed by them.
        const info = sdtInfos ? sdtInfos.get(child) : undefined;
        // Track the control regardless of whether it ANCHORS. Anchoring
        // decides whether a `{#cc:N}` token projects; enclosure decides which
        // gates apply, and the two are not the same question. A
        // `sdtContentLocked` picture control projects no token and is still
        // locked.
        const pushed = !!info && tag === QN_W_SDT;
        if (pushed) sdtStack.push(info);
        try {
          if (info && info.cls === "checkbox" && !hasBallotRun(child)) {
            // Degenerate control: Word always writes the glyph run, but a
            // generator might not. Emit the token virtually so it stays three
            // characters wide instead of collapsing to `[]`. The NORMAL case is
            // absent by design — the glyph run substitutes itself at run
            // emission, which covers every path that reaches a run.
            yield { type: "checkbox_start", info } as SdtEvent;
            yield { type: "checkbox_mark", info } as SdtEvent;
            yield { type: "checkbox_end", info } as SdtEvent;
          } else if (!info || !isAnchored(info)) {
            yield* traverse_node(child);
          } else {
            yield { type: "sdt_start", info } as SdtEvent;
            if (!info.showingPlaceholder) {
              yield* traverse_node(child);
            }
            // Ghost text NEVER projects as body text (spec §3, A1.4). The
            // placeholder run lives in sdtContent like any other run, so
            // descending would emit "Click or tap here to enter text." as if
            // the user had typed it. The bubble that replaces it is chrome,
            // added by the consumer, because only the consumer knows whether
            // this is the clean view.
            yield { type: "sdt_end", info } as SdtEvent;
          }
        } finally {
          if (pushed) sdtStack.pop();
        }
      }
    }
  }

  yield* traverse_node(paragraph._element);
}
