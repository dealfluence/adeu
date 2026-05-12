import { qn, findChild, findAllDescendants } from '../docx/dom.js';
import { Paragraph, Table, Run, NotesPart, FootnoteItem, DocxEvent } from '../docx/primitives.js';

export const QN_W_P = 'w:p';
export const QN_W_R = 'w:r';
export const QN_W_T = 'w:t';
export const QN_W_DELTEXT = 'w:delText';
export const QN_W_TAB = 'w:tab';
export const QN_W_BR = 'w:br';
export const QN_W_CR = 'w:cr';
export const QN_W_RPR = 'w:rPr';
export const QN_W_RPRCHANGE = 'w:rPrChange';
export const QN_W_COMMENTREFERENCE = 'w:commentReference';
export const QN_W_FOOTNOTEREFERENCE = 'w:footnoteReference';
export const QN_W_ENDNOTEREFERENCE = 'w:endnoteReference';
export const QN_W_FLDCHAR = 'w:fldChar';
export const QN_W_FLDCHARTYPE = 'w:fldCharType';
export const QN_W_INSTRTEXT = 'w:instrText';
export const QN_W_INS = 'w:ins';
export const QN_W_DEL = 'w:del';
export const QN_W_ID = 'w:id';
export const QN_W_AUTHOR = 'w:author';
export const QN_W_DATE = 'w:date';
export const QN_W_COMMENTRANGESTART = 'w:commentRangeStart';
export const QN_W_COMMENTRANGEEND = 'w:commentRangeEnd';
export const QN_W_HYPERLINK = 'w:hyperlink';
export const QN_R_ID = 'r:id';
export const QN_W_FLDSIMPLE = 'w:fldSimple';
export const QN_W_INSTR = 'w:instr';
export const QN_W_BOOKMARKSTART = 'w:bookmarkStart';
export const QN_W_NAME = 'w:name';
export const QN_W_SDT = 'w:sdt';
export const QN_W_SMARTTAG = 'w:smartTag';
export const QN_W_SDTCONTENT = 'w:sdtContent';
export const QN_W_B = 'w:b';
export const QN_W_I = 'w:i';
export const QN_W_VAL = 'w:val';
export const QN_W_PPR = 'w:pPr';
export const QN_W_PSTYLE = 'w:pStyle';
export const QN_W_OUTLINELVL = 'w:outlineLvl';
export const QN_W_NUMPR = 'w:numPr';
export const QN_W_NUMID = 'w:numId';
export const QN_W_ILVL = 'w:ilvl';

const _CUSTOM_HEADING_NAME_RE = /Heading[ ]?([1-6])(?![0-9])/;

export function _get_style_cache(part: any): [Record<string, any>, string | null] {
  const pkg = part.package || part.pkg || (part.part ? part.part.pkg : null);
  if (pkg && pkg._adeu_style_cache) {
    return pkg._adeu_style_cache;
  }

  const cache: Record<string, any> = {};
  let default_pstyle: string | null = null;
  const raw_styles: Record<string, any> = {};

  const stylesPart = pkg?.getPartByPath('word/styles.xml');
  if (!stylesPart) {
    const result: [Record<string, any>, string | null] = [cache, null];
    if (pkg) pkg._adeu_style_cache = result;
    return result;
  }

  const styles = findAllDescendants(stylesPart._element, 'w:style');
  for (const s of styles) {
    const s_id = s.getAttribute('w:styleId');
    if (!s_id) continue;

    const s_type = s.getAttribute('w:type');
    const is_default = s.getAttribute('w:default') === '1' || s.getAttribute('w:default') === 'true';

    if (s_type === 'paragraph' && is_default) default_pstyle = s_id;

    const name_el = findChild(s, 'w:name');
    const name = name_el ? name_el.getAttribute('w:val') : s_id;

    const based_on_el = findChild(s, 'w:basedOn');
    const based_on = based_on_el ? based_on_el.getAttribute('w:val') : null;

    let outline_lvl: number | null = null;
    const pPr = findChild(s, 'w:pPr');
    if (pPr) {
      const oLvl = findChild(pPr, 'w:outlineLvl');
      if (oLvl) {
        const val = oLvl.getAttribute('w:val');
        if (val && /^\d+$/.test(val)) outline_lvl = parseInt(val, 10);
      }
    }

    let bold: boolean | null = null;
    const rPr = findChild(s, 'w:rPr');
    if (rPr) {
      const b = findChild(rPr, 'w:b');
      if (b) {
        const val = b.getAttribute('w:val');
        bold = val !== '0' && val !== 'false' && val !== 'off';
      }
    }

    raw_styles[s_id] = { name, based_on, outline_level: outline_lvl, bold };
  }

  const resolve_style = (s_id: string, visited: Set<string>): any => {
    if (cache[s_id]) return cache[s_id];
    if (visited.has(s_id) || !raw_styles[s_id]) return { name: s_id, outline_level: null, bold: false };

    visited.add(s_id);
    const raw = raw_styles[s_id];
    const based_on_id = raw.based_on;

    let o_lvl = raw.outline_level;
    let bold_val = raw.bold !== null ? raw.bold : false;

    if (based_on_id) {
      const parent = resolve_style(based_on_id, visited);
      if (o_lvl === null) o_lvl = parent.outline_level;
      if (raw.bold === null) bold_val = parent.bold;
    }

    const resolved = { name: raw.name, outline_level: o_lvl, bold: bold_val };
    cache[s_id] = resolved;
    return resolved;
  };

  for (const s_id in raw_styles) resolve_style(s_id, new Set());

  const result: [Record<string, any>, string | null] = [cache, default_pstyle];
  if (pkg) pkg._adeu_style_cache = result;
  return result;
}

function _detect_heading_level_from_name(name: string): number | null {
  if (!name) return null;
  const match = name.match(_CUSTOM_HEADING_NAME_RE);
  return match ? parseInt(match[1], 10) : null;
}

export function is_native_heading(paragraph: Paragraph, style_cache?: Record<string, any>, default_pstyle?: string | null): boolean {
  if (!style_cache) {
    [style_cache, default_pstyle] = _get_style_cache(paragraph._parent.part || paragraph._parent);
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
  if (style_info && style_info.outline_level !== null && style_info.outline_level >= 0 && style_info.outline_level <= 8) {
    return true;
  }

  const style_name = style_info ? style_info.name : null;
  if (style_name?.startsWith('Heading')) return true;
  if (style_name === 'Title') return true;
  if (style_name && style_name !== 'Normal') {
    if (_detect_heading_level_from_name(style_name) !== null) return true;
  }

  return false;
}

export function get_paragraph_prefix(paragraph: Paragraph, style_cache?: Record<string, any>, default_pstyle?: string | null): string {
  if (!style_cache) {
    [style_cache, default_pstyle] = _get_style_cache(paragraph._parent.part || paragraph._parent);
  }
  const pPr = findChild(paragraph._element, QN_W_PPR);

  if (pPr) {
    const oLvl = findChild(pPr, QN_W_OUTLINELVL);
    if (oLvl) {
      const val = oLvl.getAttribute(QN_W_VAL);
      if (val && /^\d+$/.test(val)) {
        const lvl = parseInt(val, 10);
        if (lvl >= 0 && lvl <= 8) return '#'.repeat(lvl + 1) + ' ';
      }
    }
  }

  let style_id = default_pstyle;
  if (pPr) {
    const pStyle = findChild(pPr, QN_W_PSTYLE);
    if (pStyle) style_id = pStyle.getAttribute(QN_W_VAL) || default_pstyle;
  }

  const style_info = style_id && style_cache ? style_cache[style_id] : null;
  if (style_info && style_info.outline_level !== null && style_info.outline_level >= 0 && style_info.outline_level <= 8) {
    return '#'.repeat(style_info.outline_level + 1) + ' ';
  }

  const style_name = style_info ? style_info.name : null;
  if (style_name?.startsWith('Heading')) {
    const match = style_name.replace('Heading', '').trim();
    if (/^\d+$/.test(match)) return '#'.repeat(parseInt(match, 10)) + ' ';
  }

  if (style_name === 'Title') return '# ';

  if (pPr) {
    const numPr = findChild(pPr, QN_W_NUMPR);
    if (numPr) {
      const numId = findChild(numPr, QN_W_NUMID);
      if (numId && numId.getAttribute(QN_W_VAL) !== '0') {
        let level = 0;
        const ilvl = findChild(numPr, QN_W_ILVL);
        if (ilvl) {
          const valAttr = ilvl.getAttribute(QN_W_VAL);
          if (valAttr) level = parseInt(valAttr, 10) || 0;
        }
        return '    '.repeat(level) + '* ';
      }
    }
  }

  if (style_name && style_name !== 'Normal') {
    const custom_level = _detect_heading_level_from_name(style_name);
    if (custom_level !== null) return '#'.repeat(custom_level) + ' ';
  }

  if (!style_name || style_name === 'Normal') {
    const text = paragraph.text.trim();
    if (text && text.length < 100 && text === text.toUpperCase()) {
      let is_bold = false;
      if (style_info?.bold) {
        is_bold = true;
      } else {
        const runs = findAllDescendants(paragraph._element, QN_W_R);
        for (const r of runs) {
          const tList = findAllDescendants(r, QN_W_T);
          const tText = tList.map(t => t.textContent || '').join('');
          if (tText.trim()) {
            const rPr_run = findChild(r, QN_W_RPR);
            if (rPr_run) {
              const b = findChild(rPr_run, QN_W_B);
              if (b && b.getAttribute(QN_W_VAL) !== '0' && b.getAttribute(QN_W_VAL) !== 'false') {
                is_bold = true;
              }
            }
            break;
          }
        }
      }
      if (is_bold) return '## ';
    }
  }

  return '';
}

export function is_heading_paragraph(paragraph: Paragraph, style_cache?: Record<string, any>, default_pstyle?: string | null): boolean {
  const prefix = get_paragraph_prefix(paragraph, style_cache, default_pstyle);
  if (!prefix) return false;
  const stripped = prefix.trimEnd();
  return stripped.length > 0 && stripped === '#'.repeat(stripped.length);
}

export function get_run_style_markers(run: Run, is_heading: boolean | null = null): [string, string] {
  let prefix = '';
  let suffix = '';

  const rPr = findChild(run._element, QN_W_RPR);
  let is_bold = false;
  let is_italic = false;

  if (rPr) {
    const b = findChild(rPr, QN_W_B);
    if (b && b.getAttribute(QN_W_VAL) !== '0' && b.getAttribute(QN_W_VAL) !== 'false') is_bold = true;

    const i = findChild(rPr, QN_W_I);
    if (i && i.getAttribute(QN_W_VAL) !== '0' && i.getAttribute(QN_W_VAL) !== 'false') is_italic = true;
  }

  if (is_heading === null) {
    const parent = run._parent;
    is_heading = parent instanceof Paragraph ? is_native_heading(parent) : false;
  }

  if (is_bold && !is_heading) {
    prefix += '**';
    suffix = '**' + suffix;
  }

  if (is_italic) {
    prefix += '_';
    suffix = '_' + suffix;
  }

  return [prefix, suffix];
}

export function apply_formatting_to_segments(text: string, prefix: string, suffix: string): string {
  if (!prefix && !suffix) return text;
  if (!text) return '';
  if (!text.includes('\n')) return `${prefix}${text}${suffix}`;

  const parts = text.split('\n');
  return parts.map(p => p ? `${prefix}${p}${suffix}` : '').join('\n');
}

export function get_run_text(run: Run): string {
  let text = '';
  for (let i = 0; i < run._element.childNodes.length; i++) {
    const child = run._element.childNodes[i] as Element;
    if (child.nodeType !== 1) continue;
    
    if (child.tagName === QN_W_T || child.tagName === QN_W_DELTEXT) {
      const raw = child.textContent || '';
      text += raw.replace(/\t/g, ' ');
    } else if (child.tagName === QN_W_TAB) {
      text += ' ';
    } else if (child.tagName === QN_W_BR || child.tagName === QN_W_CR) {
      text += '\n';
    }
  }
  return text;
}

export function* iter_block_items(parent: any): Generator<Paragraph | Table | FootnoteItem> {
  const parent_elm = parent._element || parent.element || parent;

  if (parent.constructor.name === 'NotesPart') {
    const tag = parent.note_type === 'fn' ? 'w:footnote' : 'w:endnote';
    const notes = findAllDescendants(parent_elm, tag);
    for (const child of notes) {
      if (child.getAttribute('w:type') === 'separator' || child.getAttribute('w:type') === 'continuationSeparator') continue;
      yield new FootnoteItem(child, parent, parent.note_type);
    }
    return;
  }

  for (let i = 0; i < parent_elm.childNodes.length; i++) {
    const child = parent_elm.childNodes[i] as Element;
    if (child.nodeType !== 1) continue;

    if (child.tagName === QN_W_P) {
      yield new Paragraph(child, parent);
    } else if (child.tagName === 'w:tbl') {
      yield new Table(child, parent);
    }
  }
}

export function* iter_document_parts(doc: any): Generator<any> {
  // Simplified for TS port - just yield main document and notes for ingestion
  yield doc;

  const fnPart = doc.pkg.getPartByPath('word/footnotes.xml');
  const enPart = doc.pkg.getPartByPath('word/endnotes.xml');

  if (fnPart) yield new NotesPart(fnPart, 'fn');
  if (enPart) yield new NotesPart(enPart, 'en');
}

function _is_page_instr(instr: string): boolean {
  if (!instr) return false;
  const parts = instr.toUpperCase().trim().split(/\s+/);
  return parts.length > 0 && (parts[0] === 'PAGE' || parts[0] === 'NUMPAGES');
}

export function _get_part(parent: any): any {
  if (!parent) return null;
  if (parent.part) return parent.part;
  if (parent.pkg && parent.pkg.mainDocumentPart) return parent.pkg.mainDocumentPart;
  if (parent._parent) return _get_part(parent._parent);
  return null;
}

export function* iter_paragraph_content(paragraph: Paragraph): Generator<Run | DocxEvent> {
  let in_complex_field = false;
  let current_instr = '';
  let hide_result = false;

  function* process_run_element(r_element: Element): Generator<Run | DocxEvent> {
    let c_id: string | null = null;
    const rPr = findChild(r_element, QN_W_RPR);
    if (rPr) {
      const rPrChange = findChild(rPr, QN_W_RPRCHANGE);
      if (rPrChange) {
        c_id = rPrChange.getAttribute(QN_W_ID);
        yield { type: 'fmt_start', id: c_id!, author: rPrChange.getAttribute(QN_W_AUTHOR) || undefined, date: rPrChange.getAttribute(QN_W_DATE) || undefined };
      }
    }

    for (let i = 0; i < r_element.childNodes.length; i++) {
      const child = r_element.childNodes[i] as Element;
      if (child.nodeType !== 1) continue;

      const tag = child.tagName;
      if (tag === QN_W_COMMENTREFERENCE) {
        const ref_id = child.getAttribute(QN_W_ID);
        if (ref_id) yield { type: 'ref', id: ref_id };
      } else if (tag === QN_W_FOOTNOTEREFERENCE) {
        const f_id = child.getAttribute(QN_W_ID);
        if (f_id) yield { type: 'footnote', id: f_id };
      } else if (tag === QN_W_ENDNOTEREFERENCE) {
        const e_id = child.getAttribute(QN_W_ID);
        if (e_id) yield { type: 'endnote', id: e_id };
      } else if (tag === QN_W_FLDCHAR) {
        const fld_type = child.getAttribute(QN_W_FLDCHARTYPE);
        if (fld_type === 'begin') {
          in_complex_field = true;
          current_instr = '';
        } else if (fld_type === 'separate') {
          if (_is_page_instr(current_instr)) hide_result = true;
          else {
            const parts = current_instr.trim().split(/\s+/);
            if (parts.length > 1 && parts[0] === 'REF') yield { type: 'xref_start', id: parts[1] };
          }
        } else if (fld_type === 'end') {
          if (!hide_result) {
            const parts = current_instr.trim().split(/\s+/);
            if (parts.length > 1 && parts[0] === 'REF') yield { type: 'xref_end', id: parts[1] };
          }
          in_complex_field = false;
          current_instr = '';
          hide_result = false;
        }
      } else if (tag === QN_W_INSTRTEXT && in_complex_field && !hide_result) {
        current_instr += child.textContent || '';
      }
    }

    if (!hide_result) yield new Run(r_element, paragraph);
    if (c_id !== null) yield { type: 'fmt_end', id: c_id };
  }

  function* traverse_node(node: Element): Generator<Run | DocxEvent> {
    for (let i = 0; i < node.childNodes.length; i++) {
      const child = node.childNodes[i] as Element;
      if (child.nodeType !== 1) continue;

      const tag = child.tagName;
      if (tag === QN_W_R) yield* process_run_element(child);
      else if (tag === QN_W_INS) {
        const i_id = child.getAttribute(QN_W_ID)!;
        yield { type: 'ins_start', id: i_id, author: child.getAttribute(QN_W_AUTHOR) || undefined, date: child.getAttribute(QN_W_DATE) || undefined };
        yield* traverse_node(child);
        yield { type: 'ins_end', id: i_id };
      } else if (tag === QN_W_DEL) {
        const d_id = child.getAttribute(QN_W_ID)!;
        yield { type: 'del_start', id: d_id, author: child.getAttribute(QN_W_AUTHOR) || undefined, date: child.getAttribute(QN_W_DATE) || undefined };
        yield* traverse_node(child);
        yield { type: 'del_end', id: d_id };
      } else if (tag === QN_W_COMMENTRANGESTART) yield { type: 'start', id: child.getAttribute(QN_W_ID)! };
      else if (tag === QN_W_COMMENTRANGEEND) yield { type: 'end', id: child.getAttribute(QN_W_ID)! };
      else if (tag === QN_W_HYPERLINK) {
        const rId = child.getAttribute(QN_R_ID) || child.getAttribute('id');
        let url = '';
        const part = _get_part(paragraph._parent);
        if (rId && part) {
          const rel = part.rels.get(rId);
          if (rel && rel.isExternal) url = rel.target;
        }
        if (url) yield { type: 'hyperlink_start', id: rId!, date: url };
        yield* traverse_node(child);
        if (url) yield { type: 'hyperlink_end', id: rId!, date: url };
      } else if (tag === QN_W_FLDSIMPLE) {
        const instr = child.getAttribute(QN_W_INSTR) || '';
        const parts = instr.trim().split(/\s+/);
        const target = (parts.length > 1 && parts[0] === 'REF') ? parts[1] : '';
        if (target) yield { type: 'xref_start', id: target };
        yield* traverse_node(child);
        if (target) yield { type: 'xref_end', id: target };
      } else if (tag === QN_W_BOOKMARKSTART) {
        const b_name = child.getAttribute(QN_W_NAME);
        if (b_name && (!b_name.startsWith('_') || b_name.startsWith('_Ref'))) yield { type: 'bookmark', id: b_name };
      } else if (tag === QN_W_SDT || tag === QN_W_SMARTTAG || tag === QN_W_SDTCONTENT) {
        yield* traverse_node(child);
      }
    }
  }

  yield* traverse_node(paragraph._element);
}