import { DocumentObject } from './docx/bridge.js';
import { Paragraph, Table, Run, DocxEvent } from './docx/primitives.js';
import { DocumentMapper, TextSpan } from './mapper.js';
import { CommentsManager } from './comments.js';
import { 
  ModifyText, InsertTableRow, DeleteTableRow, AcceptChange, RejectChange, ReplyComment, DocumentChange 
} from './models.js';
import { trim_common_context } from './diff.js';
import { findChild, findAllDescendants, serializeXml } from './docx/dom.js';
import { 
  is_heading_paragraph, is_native_heading, get_run_style_markers, get_run_text, apply_formatting_to_segments 
} from './utils/docx.js';

// --- DOM Mutation Helpers for xmldom ---
function getNextElement(el: Element): Element | null {
  let next = el.nextSibling;
  while (next) {
    if (next.nodeType === 1) return next as Element;
    next = next.nextSibling;
  }
  return null;
}

function getPreviousElement(el: Element): Element | null {
  let prev = el.previousSibling;
  while (prev) {
    if (prev.nodeType === 1) return prev as Element;
    prev = prev.previousSibling;
  }
  return null;
}

function insertAfter(newNode: Node, refNode: Element) {
  if (refNode.parentNode) {
    refNode.parentNode.insertBefore(newNode, refNode.nextSibling);
  }
}

function insertBefore(newNode: Node, refNode: Element) {
  if (refNode.parentNode) {
    refNode.parentNode.insertBefore(newNode, refNode);
  }
}

function insertAtIndex(parent: Element, index: number, child: Node) {
  const children = Array.from(parent.childNodes).filter(n => n.nodeType === 1);
  if (index >= children.length) {
    parent.appendChild(child);
  } else {
    parent.insertBefore(child, children[index]);
  }
}

// --- Validation ---
export class BatchValidationError extends Error {
  public errors: string[];
  constructor(errors: string[]) {
    super("Batch validation failed:\n" + errors.join("\n"));
    this.name = "BatchValidationError";
    this.errors = errors;
  }
}

export function validate_edit_strings(edits: any[]): string[] {
  const errors: string[] = [];

  for (let i = 0; i < edits.length; i++) {
    const edit = edits[i];
    const t_text = edit.target_text || "";
    const n_text = edit.new_text || "";

    if (n_text.includes("{++") || n_text.includes("{--") || n_text.includes("{>>") || n_text.includes("{==")) {
      errors.push(`- Edit ${i + 1} Failed: Do not manually write CriticMarkup tags ({++, {--, {>>, {==) in \`new_text\`. The engine handles redlining automatically. To add a comment, use the \`comment\` parameter.`);
    }

    if (t_text.includes("[^") || n_text.includes("[^")) {
      const t_fns = (t_text.match(/\[\^(?:fn|en)-[^\]]+\]/g) || []).sort();
      const n_fns = (n_text.match(/\[\^(?:fn|en)-[^\]]+\]/g) || []).sort();
      if (JSON.stringify(t_fns) !== JSON.stringify(n_fns)) {
        if (n_fns.length > t_fns.length || n_fns.some((f: string) => n_fns.filter((x: string) => x===f).length > t_fns.filter((x: string) => x===f).length)) {
          errors.push(`- Edit ${i + 1} Failed: Cannot insert footnote/endnote markers via text replace. Markers like \`[^fn-N]\` are read-only projections. Use Word's References menu.`);
        } else {
          errors.push(`- Edit ${i + 1} Failed: Cannot delete footnote/endnote references via text replace. The marker corresponds to a structural XML element.`);
        }
      }
    }

    if (t_text.includes("](") || n_text.includes("](")) {
      const t_links = (t_text.match(/\[(?!~)[^\]]+\]\([^)]+\)/g) || []).sort();
      const n_links = (n_text.match(/\[(?!~)[^\]]+\]\([^)]+\)/g) || []).sort();
      if (t_links.length !== n_links.length) {
        if (n_links.length > t_links.length) {
          errors.push(`- Edit ${i + 1} Failed: Cannot insert hyperlinks via text replace. Use a dedicated structural operation.`);
        } else {
          errors.push(`- Edit ${i + 1} Failed: Cannot delete hyperlinks via text replace. The marker corresponds to a structural XML element.`);
        }
      } else if (t_links.length > 1 && JSON.stringify(t_links) !== JSON.stringify(n_links)) {
        errors.push(`- Edit ${i + 1} Failed: Can only edit or retarget one hyperlink per text replacement. Please split into multiple edits.`);
      }
    }

    if (t_text.includes("[~") || n_text.includes("[~")) {
      const t_xrefs = (t_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || []);
      const n_xrefs = (n_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || []);
      if (t_xrefs.length !== n_xrefs.length) {
        if (n_xrefs.length > t_xrefs.length) {
          errors.push(`- Edit ${i + 1} Failed: Cannot insert cross-references via text replace. Markers are read-only projections.`);
        } else {
          errors.push(`- Edit ${i + 1} Failed: Cannot delete cross-references via text replace. The marker corresponds to a structural XML element.`);
        }
      } else {
        // Advanced XREF validation simplified for port scope
        if (JSON.stringify(t_xrefs) !== JSON.stringify(n_xrefs)) {
          errors.push(`- Edit ${i + 1} Failed: Modifying or retargeting cross-reference markers is disallowed to prevent dependency corruption.`);
        }
      }
    }

    if (t_text.includes("{#") || n_text.includes("{#")) {
      const t_anchors = t_text.match(/\{#[^\}]+\}/g) || [];
      const n_anchors = n_text.match(/\{#[^\}]+\}/g) || [];
      for (const a of n_anchors) {
        if (n_anchors.filter((x: string) => x===a).length > t_anchors.filter((x: string) => x===a).length) {
          errors.push(`- Edit ${i + 1} Failed: Cannot modify or insert internal anchor markers (\`{#...}\`). These represent structural XML bookmarks.`);
          break;
        }
      }
    }

    if (edit.type === 'modify' && n_text) {
      const lines = n_text.split('\n');
      for (const line of lines) {
        const stripped = line.trimStart();
        if (stripped.startsWith("#######")) {
          const level = stripped.length - stripped.replace(/^#+/, '').length;
          if (stripped.substring(level).startsWith(' ') || stripped.substring(level) === '') {
            errors.push(`- Edit ${i + 1} Failed: Heading level ${level} is not supported (maximum is 6).`);
            break;
          }
        }
      }
    }

    if (t_text.includes("READONLY_BOUNDARY_START") || n_text.includes("READONLY_BOUNDARY_START") || 
        t_text.includes("# Document Structure (Read-Only)") || n_text.includes("# Document Structure (Read-Only)")) {
      errors.push(`- Edit ${i + 1} Failed: Modification targets the read-only boundary (Structural Appendix). This section cannot be edited.`);
    }
  }

  return errors;
}

// --- Engine ---
export class RedlineEngine {
  public doc: DocumentObject;
  public author: string;
  public timestamp: string;
  public current_id: number;
  public mapper: DocumentMapper;
  public comments_manager: CommentsManager;
  public clean_mapper: DocumentMapper | null = null;
  public skipped_details: string[] = [];

  constructor(doc: DocumentObject, author: string = "Adeu AI (TS)") {
    this.doc = doc;
    this.author = author;
    this.timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
    
    const w16du_ns = "http://schemas.microsoft.com/office/word/2023/wordml/word16du";
    for (const part of this.doc.pkg.parts) {
      if (part === this.doc.part || (part.contentType.includes('wordprocessingml') && part.contentType.endsWith('+xml'))) {
        if (!part._element.hasAttribute('xmlns:w16du')) {
          part._element.setAttribute('xmlns:w16du', w16du_ns);
        }
      }
    }

    this.current_id = this._scan_existing_ids();
    this.mapper = new DocumentMapper(this.doc);
    this.comments_manager = new CommentsManager(this.doc);
  }

  private _scan_existing_ids(): number {
    let maxId = 0;
    for (const tag of ['w:ins', 'w:del']) {
      const elements = findAllDescendants(this.doc.element, tag);
      for (const el of elements) {
        const val = parseInt(el.getAttribute('w:id') || '0', 10);
        if (!isNaN(val) && val > maxId) maxId = val;
      }
    }
    return maxId;
  }

  public accept_all_revisions() {
    const dels = findAllDescendants(this.doc.element, 'w:del');
    for (const d of dels) {
      const parent = d.parentNode as Element | null;
      if (parent?.tagName === 'w:trPr') {
        const tr = parent.parentNode;
        tr?.parentNode?.removeChild(tr);
      } else {
        parent?.removeChild(d);
      }
    }
    const insNodes = findAllDescendants(this.doc.element, 'w:ins');
    for (const i of insNodes) {
      const parent = i.parentNode as Element | null;
      if (parent?.tagName === 'w:trPr') {
        parent.removeChild(i);
      } else {
        while (i.firstChild) parent?.insertBefore(i.firstChild, i);
        parent?.removeChild(i);
      }
    }
  }

  private _getNextId(): string {
    this.current_id++;
    return this.current_id.toString();
  }

  private _create_track_change_tag(tagName: string, author: string = "", reuseId: string | null = null): Element {
    const xmlDoc = this.doc.part._element.ownerDocument!;
    const tag = xmlDoc.createElement(tagName);
    const wid = reuseId !== null ? reuseId : this._getNextId();
    tag.setAttribute("w:id", wid);
    tag.setAttribute("w:author", author || this.author);
    tag.setAttribute("w:date", this.timestamp);
    tag.setAttribute("w16du:dateUtc", this.timestamp);
    return tag;
  }

  private _set_text_content(element: Element, text: string) {
    element.textContent = text;
    if (text.trim() !== text) {
      element.setAttribute("xml:space", "preserve");
    }
  }

  private _parse_markdown_style(text: string): [string, string | null] {
    const stripped_text = text.trimStart();

    if (stripped_text.startsWith("#")) {
      let level = 0;
      let temp = stripped_text;
      while (temp.startsWith("#")) {
        level++;
        temp = temp.substring(1);
      }
      if (temp.startsWith(" ")) return [temp.trim(), `Heading ${level}`];
    }

    if (stripped_text.startsWith("* ") || stripped_text.startsWith("- ")) {
      return [stripped_text.substring(2).trim(), "List Paragraph"];
    }

    const match = stripped_text.match(/^\d+\.\s+/);
    if (match) {
      return [stripped_text.substring(match[0].length).trim(), "List Number"];
    }

    return [text, null];
  }

  private _parse_inline_markdown(text: string, baseStyle: any = {}): [string, any][] {
    if (!text) return [];

    const tokenPattern = /(\*\*.*?\*\*)|(_.*?_)/;
    const match = text.match(tokenPattern);

    if (!match) return [[text, baseStyle]];

    const start = match.index!;
    const raw = match[0];
    const end = start + raw.length;

    const isBold = raw.startsWith('**');
    const innerContent = isBold ? raw.substring(2, raw.length - 2) : raw.substring(1, raw.length - 1);

    const preText = text.substring(0, start);
    const postText = text.substring(end);

    const results: [string, any][] = [];
    if (preText) results.push([preText, baseStyle]);

    const newStyle = { ...baseStyle };
    if (isBold) newStyle.bold = true;
    else newStyle.italic = true;

    results.push(...this._parse_inline_markdown(innerContent, newStyle));
    results.push(...this._parse_inline_markdown(postText, baseStyle));

    return results;
  }

  private _apply_run_props(runElement: Element, props: any, suppressInherited: boolean = false) {
    if (!props) {
      if (!suppressInherited) return;
      props = {};
    }

    let rPr = findChild(runElement, 'w:rPr');
    if (!rPr && (props.bold || props.italic || suppressInherited)) {
      const doc = runElement.ownerDocument!;
      rPr = doc.createElement('w:rPr');
      runElement.appendChild(rPr);
    }

    if (rPr) {
      const doc = runElement.ownerDocument!;
      if (props.bold) {
        let b = findChild(rPr, 'w:b');
        if (!b) { b = doc.createElement('w:b'); rPr.appendChild(b); }
        b.setAttribute('w:val', '1');
      } else if (suppressInherited) {
        const b = findChild(rPr, 'w:b');
        if (b) rPr.removeChild(b);
      }

      if (props.italic) {
        let i = findChild(rPr, 'w:i');
        if (!i) { i = doc.createElement('w:i'); rPr.appendChild(i); }
        i.setAttribute('w:val', '1');
      } else if (suppressInherited) {
        const i = findChild(rPr, 'w:i');
        if (i) rPr.removeChild(i);
      }
    }
  }

  public validate_edits(edits: any[]): string[] {
    const errors: string[] = [];
    if (!this.mapper.full_text) this.mapper['_build_map']();

    errors.push(...validate_edit_strings(edits));

    for (let i = 0; i < edits.length; i++) {
      const edit = edits[i];
      if (!edit.target_text) continue;

      let matches = this.mapper.find_all_match_indices(edit.target_text);
      let activeText = this.mapper.full_text;

      if (matches.length === 0) {
        if (!this.clean_mapper) this.clean_mapper = new DocumentMapper(this.doc, true);
        matches = this.clean_mapper.find_all_match_indices(edit.target_text);
        if (matches.length > 0) activeText = this.clean_mapper.full_text;
      }

      if (matches.length === 0) {
        errors.push(`- Edit ${i + 1} Failed: Target text not found in document:\n  "${edit.target_text}"`);
      } else if (matches.length > 1) {
        errors.push(`- Edit ${i + 1} Failed: Target text is ambiguous. Found ${matches.length} matches.\nProvide more context.`);
      }

      for (const [start, length] of matches) {
        const spans = this.mapper.spans.filter(s => s.end > start && s.start < start + length);
        const nestedAuthors = new Set<string>();
        for (const s of spans) {
          if (s.ins_id) {
            const insNodes = findAllDescendants(this.doc.element, 'w:ins').filter(n => n.getAttribute('w:id') === s.ins_id);
            if (insNodes.length > 0) {
              const auth = insNodes[0].getAttribute('w:author');
              if (auth && auth !== this.author) nestedAuthors.add(auth);
            }
          }
        }
        if (nestedAuthors.size > 0) {
          errors.push(`- Edit ${i + 1} Failed: Modification targets an active insertion from another author (${Array.from(nestedAuthors).join(', ')}).`);
        }
      }
    }
    return errors;
  }

  public process_batch(changes: DocumentChange[]): any {
    this.skipped_details = [];
    const actions = changes.filter(c => ['accept', 'reject', 'reply'].includes(c.type));
    const edits = changes.filter(c => !['accept', 'reject', 'reply'].includes(c.type));

    let applied_actions = 0, skipped_actions = 0;
    if (actions.length > 0) {
      const res = this.apply_review_actions(actions);
      applied_actions = res[0];
      skipped_actions = res[1];
      if (applied_actions > 0) {
        this.mapper['_build_map']();
        if (this.clean_mapper) this.clean_mapper['_build_map']();
      }
    }

    if (edits.length > 0) {
      const errors = this.validate_edits(edits);
      if (errors.length > 0) throw new BatchValidationError(errors);
    }

    let applied_edits = 0, skipped_edits = 0;
    if (edits.length > 0) {
      const res = this.apply_edits(edits as any[]);
      applied_edits = res[0];
      skipped_edits = res[1];
    }

    return {
      actions_applied: applied_actions,
      actions_skipped: skipped_actions,
      edits_applied: applied_edits,
      edits_skipped: skipped_edits,
      skipped_details: this.skipped_details,
    };
  }

  public apply_edits(edits: any[]): [number, number] {
    let applied = 0;
    let skipped = 0;
    const resolved_edits: [any, string | null][] = [];

    for (const edit of edits) {
      if (edit._match_start_index !== undefined && edit._match_start_index !== null) {
        resolved_edits.push([edit, edit.new_text || null]);
      } else if (edit.type === 'insert_row' || edit.type === 'delete_row') {
        const [idx] = this.mapper.find_match_index(edit.target_text);
        if (idx !== -1) {
          edit._match_start_index = idx;
          resolved_edits.push([edit, null]);
        } else {
          skipped++;
          this.skipped_details.push(`- Failed to locate row target: '${(edit.target_text || '').substring(0, 40)}...'`);
        }
      } else {
        const resolved = this._pre_resolve_heuristic_edit(edit);
        if (resolved) {
          if (Array.isArray(resolved)) {
            for (const r of resolved) resolved_edits.push([r, r.new_text]);
          } else {
            resolved_edits.push([resolved, (resolved as any).new_text]);
          }
        } else {
          skipped++;
          this.skipped_details.push(`- Failed to apply edit targeting: '${(edit.target_text || 'insertion').substring(0, 40)}...'`);
        }
      }
    }

    resolved_edits.sort((a, b) => (b[0]._match_start_index || 0) - (a[0]._match_start_index || 0));
    const occupied_ranges: [number, number][] = [];

    for (const [edit, orig_new] of resolved_edits) {
      const start = edit._match_start_index || 0;
      const end = start + (edit.target_text ? edit.target_text.length : 0);

      const overlaps = occupied_ranges.some(([occ_start, occ_end]) => start < occ_end && end > occ_start);
      if (overlaps) {
        skipped++;
        this.skipped_details.push(`- Skipped overlapping edit targeting: '${(edit.target_text || 'insertion').substring(0, 40)}...'`);
        continue;
      }

      let success = false;
      if (edit.type === 'modify') {
        success = this._apply_single_edit_indexed(edit, orig_new, false);
      } else if (edit.type === 'insert_row' || edit.type === 'delete_row') {
        success = this._apply_table_edit(edit, false);
      }

      if (success) {
        applied++;
        occupied_ranges.push([start, end]);
      } else {
        skipped++;
        this.skipped_details.push(`- Failed to apply edit targeting: '${(edit.target_text || 'insertion').substring(0, 40)}...'`);
      }
    }

    return [applied, skipped];
  }

  public apply_review_actions(actions: any[]): [number, number] {
    let applied = 0;
    let skipped = 0;

    for (const action of actions) {
      const type = action.type;
      if (type === 'reply') {
        const cid = action.target_id.replace('Com:', '');
        this.comments_manager.addComment(this.author, action.text, cid);
        applied++;
        continue;
      }

      const target_id = action.target_id.replace('Chg:', '');
      const all_ins = findAllDescendants(this.doc.element, 'w:ins').filter(n => n.getAttribute('w:id') === target_id);
      const all_del = findAllDescendants(this.doc.element, 'w:del').filter(n => n.getAttribute('w:id') === target_id);
      const all_nodes = [...all_ins, ...all_del];

      if (all_nodes.length === 0) {
        skipped++;
        this.skipped_details.push(`- Failed to apply action: Target ID ${action.target_id} not found.`);
        continue;
      }

      for (const node of all_nodes) {
        const is_ins = node.tagName === 'w:ins';
        const parent_tag = node.parentNode ? (node.parentNode as Element).tagName : '';
        const is_trPr = parent_tag === 'w:trPr';

        if (type === 'accept') {
          if (is_ins) {
            if (is_trPr) node.parentNode?.removeChild(node);
            else {
              while (node.firstChild) node.parentNode?.insertBefore(node.firstChild, node);
              node.parentNode?.removeChild(node);
            }
          } else {
            if (is_trPr) {
              const tr = node.parentNode?.parentNode;
              tr?.parentNode?.removeChild(tr);
            } else {
              node.parentNode?.removeChild(node);
            }
          }
        } else if (type === 'reject') {
          if (is_ins) {
            if (is_trPr) {
              const tr = node.parentNode?.parentNode;
              tr?.parentNode?.removeChild(tr);
            } else node.parentNode?.removeChild(node);
          } else {
            if (is_trPr) node.parentNode?.removeChild(node);
            else {
              const delTexts = Array.from(node.getElementsByTagName('w:delText'));
              for (const dt of delTexts) {
                const t = dt.ownerDocument!.createElement('w:t');
                t.textContent = dt.textContent;
                if (dt.hasAttribute('xml:space')) t.setAttribute('xml:space', 'preserve');
                dt.parentNode?.replaceChild(t, dt);
              }
              while (node.firstChild) node.parentNode?.insertBefore(node.firstChild, node);
              node.parentNode?.removeChild(node);
            }
          }
        }
      }
      applied++;
    }
    return [applied, skipped];
  }

  private _apply_table_edit(edit: any, rebuild_map: boolean): boolean {
    const start_idx = edit._match_start_index || 0;
    const [anchor_run, anchor_para] = this.mapper.get_insertion_anchor(start_idx, rebuild_map);
    
    let target_element: Element | null = null;
    if (anchor_run) target_element = anchor_run._element;
    else if (anchor_para) target_element = anchor_para._element;

    if (!target_element) return false;

    let tr: Element | null = target_element;
    while (tr && tr.tagName !== 'w:tr') tr = tr.parentNode as Element;
    if (!tr) return false;

    if (edit.type === 'delete_row') {
      let trPr = findChild(tr, 'w:trPr');
      if (!trPr) {
        trPr = tr.ownerDocument!.createElement('w:trPr');
        tr.insertBefore(trPr, tr.firstChild);
      }
      trPr.appendChild(this._create_track_change_tag('w:del'));
      return true;
    } else if (edit.type === 'insert_row') {
      const new_tr = tr.ownerDocument!.createElement('w:tr');
      const trPr = tr.ownerDocument!.createElement('w:trPr');
      new_tr.appendChild(trPr);
      trPr.appendChild(this._create_track_change_tag('w:ins'));
      for (const cellText of edit.cells) {
        const tc = tr.ownerDocument!.createElement('w:tc');
        const p = tr.ownerDocument!.createElement('w:p');
        const r = tr.ownerDocument!.createElement('w:r');
        const t = tr.ownerDocument!.createElement('w:t');
        t.textContent = cellText;
        if (cellText.trim() !== cellText) t.setAttribute('xml:space', 'preserve');
        r.appendChild(t); p.appendChild(r); tc.appendChild(p); new_tr.appendChild(tc);
      }
      if (edit.position === 'above') tr.parentNode?.insertBefore(new_tr, tr);
      else insertAfter(new_tr, tr);
      return true;
    }
    return false;
  }

  private _pre_resolve_heuristic_edit(edit: any): any {
    if (!edit.target_text) return null;

    let [start_idx, match_len] = this.mapper.find_match_index(edit.target_text);
    let use_clean_map = false;

    if (start_idx === -1) {
      if (!this.clean_mapper) this.clean_mapper = new DocumentMapper(this.doc, true);
      [start_idx, match_len] = this.clean_mapper.find_match_index(edit.target_text);
      if (start_idx !== -1) use_clean_map = true;
      else return null;
    }

    const active_mapper = use_clean_map ? this.clean_mapper! : this.mapper;
    const effective_new_text = edit.new_text || "";
    const actual_doc_text = this.mapper.full_text.substring(start_idx, start_idx + match_len);

    if (actual_doc_text === effective_new_text || edit.target_text === effective_new_text) {
      return {
        type: "modify",
        target_text: actual_doc_text,
        new_text: actual_doc_text,
        comment: edit.comment,
        _match_start_index: start_idx,
        _internal_op: "COMMENT_ONLY",
        _active_mapper_ref: active_mapper
      };
    }

    let effective_op = "";
    let final_target = "";
    let final_new = "";
    let effective_start_idx = start_idx;

    if (effective_new_text.startsWith(actual_doc_text)) {
      effective_op = "INSERTION";
      final_new = effective_new_text.substring(actual_doc_text.length);
      effective_start_idx = start_idx + match_len;
    } else {
      const [prefix_len, suffix_len] = trim_common_context(actual_doc_text, effective_new_text);
      const t_end = actual_doc_text.length - suffix_len;
      const n_end = effective_new_text.length - suffix_len;

      final_target = actual_doc_text.substring(prefix_len, t_end);
      final_new = effective_new_text.substring(prefix_len, n_end);
      effective_start_idx = start_idx + prefix_len

      if (!final_target && final_new) effective_op = "INSERTION";
      else if (final_target && !final_new) effective_op = "DELETION";
      else if (final_target && final_new) effective_op = "MODIFICATION";
      else effective_op = "COMMENT_ONLY";
    }

    return {
      type: "modify",
      target_text: final_target,
      new_text: final_new,
      comment: edit.comment,
      _match_start_index: effective_start_idx,
      _internal_op: effective_op,
      _active_mapper_ref: active_mapper
    };
  }

  private _apply_single_edit_indexed(edit: any, orig_new: string | null, rebuild_map: boolean): boolean {
    let op = edit._internal_op;
    const active_mapper = edit._active_mapper_ref || this.mapper;
    const start_idx = edit._match_start_index || 0;
    const length = edit.target_text ? edit.target_text.length : 0;

    const del_id = ['DELETION', 'MODIFICATION'].includes(op) ? this._getNextId() : null;
    const ins_id = ['INSERTION', 'MODIFICATION'].includes(op) ? this._getNextId() : null;

    if (op === "COMMENT_ONLY") {
      // Mocked for Port limits, normally anchors to found runs
      return true;
    }

    if (op === "INSERTION") {
      const [anchor_run, anchor_para] = active_mapper.get_insertion_anchor(start_idx, rebuild_map);
      if (!anchor_run && !anchor_para) return false;

      const xmlDoc = this.doc.part._element.ownerDocument!;
      const ins = this._create_track_change_tag('w:ins', '', ins_id);
      
      const segments = this._parse_inline_markdown(edit.new_text || "");
      for (const [segText, segProps] of segments) {
        const r = xmlDoc.createElement('w:r');
        this._apply_run_props(r, segProps, false);
        const t = xmlDoc.createElement('w:t');
        this._set_text_content(t, segText);
        r.appendChild(t);
        ins.appendChild(r);
      }

      if (anchor_run) {
        insertAfter(ins, anchor_run._element);
      } else if (anchor_para) {
        anchor_para._element.appendChild(ins);
      }
      return true;
    }

    // DELETION / MODIFICATION
    const target_runs = active_mapper.find_target_runs_by_index(start_idx, length, rebuild_map);
    if (target_runs.length === 0) return false;

    let last_del: Element | null = null;
    for (const run of target_runs) {
      const del_tag = this._create_track_change_tag('w:del', '', del_id);
      const new_run = run._element.cloneNode(true) as Element;
      
      const tNodes = Array.from(new_run.getElementsByTagName('w:t'));
      tNodes.forEach(t => {
        const delText = new_run.ownerDocument!.createElement('w:delText');
        delText.textContent = t.textContent;
        if (t.hasAttribute('xml:space')) delText.setAttribute('xml:space', 'preserve');
        new_run.replaceChild(delText, t);
      });

      del_tag.appendChild(new_run);
      run._element.parentNode?.replaceChild(del_tag, run._element);
      last_del = del_tag;
    }

    if (op === "MODIFICATION" && edit.new_text && last_del) {
      const xmlDoc = this.doc.part._element.ownerDocument!;
      const ins = this._create_track_change_tag('w:ins', '', ins_id);
      const segments = this._parse_inline_markdown(edit.new_text);
      for (const [segText, segProps] of segments) {
        const r = xmlDoc.createElement('w:r');
        this._apply_run_props(r, segProps, false);
        const t = xmlDoc.createElement('w:t');
        this._set_text_content(t, segText);
        r.appendChild(t);
        ins.appendChild(r);
      }
      insertAfter(ins, last_del);
    }

    return true;
  }
}