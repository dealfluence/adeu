import { DocumentObject } from "./docx/bridge.js";
import { Paragraph, Table, Run, DocxEvent } from "./docx/primitives.js";
import { DocumentMapper, TextSpan } from "./mapper.js";
import { CommentsManager } from "./comments.js";
import {
  ModifyText,
  InsertTableRow,
  DeleteTableRow,
  AcceptChange,
  RejectChange,
  ReplyComment,
  DocumentChange,
} from "./models.js";
import { trim_common_context } from "./diff.js";
import { findChild, findAllDescendants, serializeXml } from "./docx/dom.js";
import {
  is_heading_paragraph,
  is_native_heading,
  get_run_style_markers,
  get_run_text,
  apply_formatting_to_segments,
} from "./utils/docx.js";
import { format_ambiguity_error } from "./markup.js";

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
  const children = Array.from(parent.childNodes).filter(
    (n) => n.nodeType === 1,
  );
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

    if (
      n_text.includes("{++") ||
      n_text.includes("{--") ||
      n_text.includes("{>>") ||
      n_text.includes("{==")
    ) {
      errors.push(
        `- Edit ${i + 1} Failed: Do not manually write CriticMarkup tags ({++, {--, {>>, {==) in \`new_text\`. The engine handles redlining automatically. To add a comment, use the \`comment\` parameter.`,
      );
    }

    if (t_text.includes("[^") || n_text.includes("[^")) {
      const t_fns = (t_text.match(/\[\^(?:fn|en)-[^\]]+\]/g) || []).sort();
      const n_fns = (n_text.match(/\[\^(?:fn|en)-[^\]]+\]/g) || []).sort();
      if (JSON.stringify(t_fns) !== JSON.stringify(n_fns)) {
        if (
          n_fns.length > t_fns.length ||
          n_fns.some(
            (f: string) =>
              n_fns.filter((x: string) => x === f).length >
              t_fns.filter((x: string) => x === f).length,
          )
        ) {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot insert footnote/endnote markers via text replace. Markers like \`[^fn-N]\` are read-only projections. Use Word's References menu.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot delete footnote/endnote references via text replace. The marker corresponds to a structural XML element.`,
          );
        }
      }
    }

    if (t_text.includes("](") || n_text.includes("](")) {
      const t_links = (t_text.match(/\[(?!~)[^\]]+\]\([^)]+\)/g) || []).sort();
      const n_links = (n_text.match(/\[(?!~)[^\]]+\]\([^)]+\)/g) || []).sort();
      if (t_links.length !== n_links.length) {
        if (n_links.length > t_links.length) {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot insert hyperlinks via text replace. Use a dedicated structural operation.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot delete hyperlinks via text replace. The marker corresponds to a structural XML element.`,
          );
        }
      } else if (
        t_links.length > 1 &&
        JSON.stringify(t_links) !== JSON.stringify(n_links)
      ) {
        errors.push(
          `- Edit ${i + 1} Failed: Can only edit or retarget one hyperlink per text replacement. Please split into multiple edits.`,
        );
      }
    }

    if (t_text.includes("[~") || n_text.includes("[~")) {
      const t_xrefs = t_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || [];
      const n_xrefs = n_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || [];
      if (t_xrefs.length !== n_xrefs.length) {
        if (n_xrefs.length > t_xrefs.length) {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot insert cross-references via text replace. Markers are read-only projections.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot delete cross-references via text replace. The marker corresponds to a structural XML element.`,
          );
        }
      } else {
        // Advanced XREF validation simplified for port scope
        if (JSON.stringify(t_xrefs) !== JSON.stringify(n_xrefs)) {
          errors.push(
            `- Edit ${i + 1} Failed: Modifying or retargeting cross-reference markers is disallowed to prevent dependency corruption.`,
          );
        }
      }
    }

    if (t_text.includes("{#") || n_text.includes("{#")) {
      const t_anchors = t_text.match(/\{#[^\}]+\}/g) || [];
      const n_anchors = n_text.match(/\{#[^\}]+\}/g) || [];
      for (const a of n_anchors) {
        if (
          n_anchors.filter((x: string) => x === a).length >
          t_anchors.filter((x: string) => x === a).length
        ) {
          errors.push(
            `- Edit ${i + 1} Failed: Cannot modify or insert internal anchor markers (\`{#...}\`). These represent structural XML bookmarks.`,
          );
          break;
        }
      }
    }

    if (edit.type === "modify" && n_text) {
      const lines = n_text.split(/[\r\n]+/);
      for (const line of lines) {
        const stripped = line.trimStart();
        if (stripped.startsWith("#######")) {
          const level = stripped.length - stripped.replace(/^#+/, "").length;
          if (
            stripped.substring(level).startsWith(" ") ||
            stripped.substring(level) === ""
          ) {
            errors.push(
              `- Edit ${i + 1} Failed: Heading level ${level} is not supported (maximum is 6).`,
            );
            break;
          }
        }
      }
    }

    if (
      t_text.includes("READONLY_BOUNDARY_START") ||
      n_text.includes("READONLY_BOUNDARY_START") ||
      t_text.includes("# Document Structure (Read-Only)") ||
      n_text.includes("# Document Structure (Read-Only)")
    ) {
      errors.push(
        `- Edit ${i + 1} Failed: Modification targets the read-only boundary (Structural Appendix). This section cannot be edited.`,
      );
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
    this.timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

    const w16du_ns =
      "http://schemas.microsoft.com/office/word/2023/wordml/word16du";
    for (const part of this.doc.pkg.parts) {
      if (
        part === this.doc.part ||
        (part.contentType.includes("wordprocessingml") &&
          part.contentType.endsWith("+xml"))
      ) {
        if (!part._element.hasAttribute("xmlns:w16du")) {
          part._element.setAttribute("xmlns:w16du", w16du_ns);
        }
      }
    }

    this.current_id = this._scan_existing_ids();
    this.mapper = new DocumentMapper(this.doc);
    this.comments_manager = new CommentsManager(this.doc);
  }

  private _check_punctuation_warning(target_text: string): string | null {
    if (!target_text) return null;
    if (target_text.includes("_") || target_text.includes("-")) {
      return `Warning: target_text '${target_text}' contains tokenization-splitting punctuation ('_' or '-'). This can trigger mid-word splits in the diff engine. Consider using a longer plain-prose anchor.`;
    }
    return null;
  }

  private _build_edit_context_previews(edit: any): [string | null, string | null] {
    if (edit.type !== "modify") return [null, null];
    if (edit._resolved_proxy_edit) {
      edit = edit._resolved_proxy_edit;
    }
    const start_idx = edit._resolved_start_idx;
    if (start_idx === undefined || start_idx === null) return [null, null];
    const target_text = edit.target_text || "";
    const new_text = edit.new_text || "";
    const length = target_text.length;
    const active_mapper = edit._active_mapper_ref || this.mapper;
    const full_text = active_mapper.full_text;
    if (!full_text) return [null, null];

    const before_start = Math.max(0, start_idx - 30);
    const context_before = full_text.substring(before_start, start_idx);
    const context_after = full_text.substring(start_idx + length, start_idx + length + 30);

    const critic_markup = `${context_before}{--${target_text}--}{++${new_text}++}${context_after}`;

    let clean_text = critic_markup;
    clean_text = clean_text.replace(/\{>>.*?<<\}/gs, "");
    clean_text = clean_text.replace(/\{--.*?--\}/gs, "");
    clean_text = clean_text.replace(/\{\+\+(.*?)\+\+\}/gs, "$1");

    return [critic_markup, clean_text];
  }

  private _scan_existing_ids(): number {
    let maxId = 0;
    for (const tag of ["w:ins", "w:del"]) {
      const elements = findAllDescendants(this.doc.element, tag);
      for (const el of elements) {
        const val = parseInt(el.getAttribute("w:id") || "0", 10);
        if (!isNaN(val) && val > maxId) maxId = val;
      }
    }
    return maxId;
  }

  public accept_all_revisions() {
    const parts_to_process: Element[] = [this.doc.element];

    for (const part of this.doc.pkg.parts) {
      if (part === this.doc.part) continue;
      if (
        part.contentType.includes("wordprocessingml") &&
        part.contentType.endsWith("+xml")
      ) {
        parts_to_process.push(part._element);
      }
    }

    for (const root_element of parts_to_process) {
      const insNodes = findAllDescendants(root_element, "w:ins");
      for (const ins of insNodes) {
        this._clean_wrapping_comments(ins);
        const parent = ins.parentNode as Element | null;
        if (!parent) continue;

        if (parent.tagName === "w:trPr") {
          parent.removeChild(ins);
          continue;
        }

        while (ins.firstChild) {
          parent.insertBefore(ins.firstChild, ins);
        }
        parent.removeChild(ins);
      }

      const pNodes = findAllDescendants(root_element, "w:p");
      for (const p of pNodes) {
        const pPr = findChild(p, "w:pPr");
        if (pPr) {
          const rPr = findChild(pPr, "w:rPr");
          const delMark = rPr ? findChild(rPr, "w:del") : null;
          if (rPr && delMark) {
            let has_content = false;
            for (const tag of ["w:t", "w:tab", "w:br"]) {
              for (const child of findAllDescendants(p, tag)) {
                if (tag === "w:t" && !child.textContent) continue;
                
                let is_deleted = false;
                let curr = child.parentNode as Element | null;
                while (curr && curr !== p) {
                  if (curr.tagName === "w:del") {
                    is_deleted = true;
                    break;
                  }
                  curr = curr.parentNode as Element | null;
                }
                if (!is_deleted) {
                  has_content = true;
                  break;
                }
              }
              if (has_content) {
                break;
              }
            }
            if (has_content) {
              rPr.removeChild(delMark);
            } else {
              this._clean_wrapping_comments(p);
              this._delete_comments_in_element(p);
              if (p.parentNode) {
                p.parentNode.removeChild(p);
              }
            }
          }
        }
      }

      const delNodes = findAllDescendants(root_element, "w:del");
      for (const d of delNodes) {
        this._clean_wrapping_comments(d);
        this._delete_comments_in_element(d);
        const parent = d.parentNode as Element | null;
        if (parent) {
          if (parent.tagName === "w:trPr") {
            const row = parent.parentNode as Element | null;
            if (row && row.parentNode) {
              row.parentNode.removeChild(row);
            }
          } else {
            parent.removeChild(d);
          }
        }
      }
    }

    // Final pass: remove any free-standing comments
    const comment_ids = new Set<string>();
    for (const tag of [
      "w:commentRangeStart",
      "w:commentRangeEnd",
      "w:commentReference",
    ]) {
      for (const node of findAllDescendants(this.doc.element, tag)) {
        const cid = node.getAttribute("w:id");
        if (cid) comment_ids.add(cid);
      }
    }

    const comments_part = this.doc.pkg.parts.find(
      (p) =>
        p.contentType ===
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    );
    if (comments_part) {
      for (const c of findAllDescendants(comments_part._element, "w:comment")) {
        const cid = c.getAttribute("w:id");
        if (cid) comment_ids.add(cid);
      }
    }

    for (const cid of comment_ids) {
      this.comments_manager.deleteComment(cid);
    }
  }

  private _getNextId(): string {
    this.current_id++;
    return this.current_id.toString();
  }

  private _create_track_change_tag(
    tagName: string,
    author: string = "",
    reuseId: string | null = null,
  ): Element {
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

  /**
   * Attaches a comment that wraps a contiguous range within a single paragraph.
   * start_element and end_element must both be direct children of parent_element
   * and start_element must come before (or equal) end_element in document order.
   * Ported from Python `RedlineEngine._attach_comment`.
   */
  private _attach_comment(
    parent_element: Element,
    start_element: Element,
    end_element: Element,
    text: string,
  ) {
    if (!text) return;

    const comment_id = this.comments_manager.addComment(this.author, text);
    const xmlDoc = parent_element.ownerDocument!;

    const range_start = xmlDoc.createElement("w:commentRangeStart");
    range_start.setAttribute("w:id", comment_id);

    const range_end = xmlDoc.createElement("w:commentRangeEnd");
    range_end.setAttribute("w:id", comment_id);

    const ref_run = xmlDoc.createElement("w:r");
    const rPr = xmlDoc.createElement("w:rPr");
    const rStyle = xmlDoc.createElement("w:rStyle");
    rStyle.setAttribute("w:val", "CommentReference");
    rPr.appendChild(rStyle);
    ref_run.appendChild(rPr);

    const ref = xmlDoc.createElement("w:commentReference");
    ref.setAttribute("w:id", comment_id);
    ref_run.appendChild(ref);

    // Insert <w:commentRangeStart> immediately before start_element.
    // Insert <w:commentRangeEnd> immediately after end_element.
    // Insert <w:r><w:commentReference/></w:r> immediately after the range end.
    parent_element.insertBefore(range_start, start_element);

    // After insertBefore above, sibling positions shifted. Re-find end_element's next sibling.
    const after_end = end_element.nextSibling;
    if (after_end) {
      parent_element.insertBefore(range_end, after_end);
      parent_element.insertBefore(ref_run, range_end.nextSibling);
    } else {
      parent_element.appendChild(range_end);
      parent_element.appendChild(ref_run);
    }
  }

  /**
   * Attaches a comment that spans across two different paragraphs (or other block
   * containers). start_element lives inside start_p, end_element lives inside end_p,
   * and the comment is open from start_element through end_element.
   * Ported from Python `RedlineEngine._attach_comment_spanning`.
   */
  private _attach_comment_spanning(
    start_p: Element,
    start_el: Element,
    end_p: Element,
    end_el: Element,
    text: string,
  ) {
    if (!text) return;

    const comment_id = this.comments_manager.addComment(this.author, text);
    const xmlDocStart = start_p.ownerDocument!;
    const xmlDocEnd = end_p.ownerDocument!;

    const range_start = xmlDocStart.createElement("w:commentRangeStart");
    range_start.setAttribute("w:id", comment_id);

    const range_end = xmlDocEnd.createElement("w:commentRangeEnd");
    range_end.setAttribute("w:id", comment_id);

    const ref_run = xmlDocEnd.createElement("w:r");
    const rPr = xmlDocEnd.createElement("w:rPr");
    const rStyle = xmlDocEnd.createElement("w:rStyle");
    rStyle.setAttribute("w:val", "CommentReference");
    rPr.appendChild(rStyle);
    ref_run.appendChild(rPr);

    const ref = xmlDocEnd.createElement("w:commentReference");
    ref.setAttribute("w:id", comment_id);
    ref_run.appendChild(ref);

    // Place range start before start_el.
    start_p.insertBefore(range_start, start_el);

    // Place range end + reference run after end_el.
    const after_end = end_el.nextSibling;
    if (after_end) {
      end_p.insertBefore(range_end, after_end);
      end_p.insertBefore(ref_run, range_end.nextSibling);
    } else {
      end_p.appendChild(range_end);
      end_p.appendChild(ref_run);
    }
  }

  /**
   * Inserts `text` as one or more tracked paragraphs anchored relative to
   * either an existing run or a paragraph. Returns:
   *   { first_node, last_p, last_ins, used_block_mode }
   * where:
   *   - first_node: the first <w:ins> (for inline mode) OR the first new <w:p>
   *     (for block mode). The caller uses this for splicing into the DOM and
   *     for anchoring comments.
   *   - last_p: the last new <w:p> created, if any. null when entirely inline.
   *   - last_ins: the last <w:ins> created (inside the last new <w:p>, or the
   *     sole inline ins). Used as the comment's end anchor.
   *   - used_block_mode: true when the first line carried a heading/list style
   *     marker and we created a new paragraph for it (rather than inlining it).
   *
   * Multi-paragraph rules (only when text contains '\n'):
   *   - Each additional line becomes a new <w:p>, inserted after the anchor
   *     paragraph in document order.
   *   - Each new <w:p> gets a copy of the anchor paragraph's <w:pPr> (so list
   *     numbering / indentation are preserved) unless the line itself starts
   *     with a markdown heading or list marker, which overrides the style.
   *   - Each new <w:p> carries a tracked paragraph-break marker
   *     (<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>) so Word natively tracks the
   *     paragraph break.
   *   - Each new <w:p>'s content is wrapped in a <w:ins>, with inline bold/
   *     italic markdown parsed via _parse_inline_markdown.
   *
   * The first line:
   *   - If it carries a heading / list marker AND we have a paragraph anchor,
   *     we drop into "block mode": no inline <w:ins>; the first line itself
   *     becomes the first new <w:p>.
   *   - Otherwise we emit a single inline <w:ins> for the first line (current
   *     behaviour) and treat the remaining lines as block extensions.
   *
   * Does NOT attach comments; callers handle that.
   */
  private _track_insert_multiline(
    text: string,
    anchor_run: Run | null,
    anchor_paragraph: Paragraph | null,
    reuse_id: string,
  ): {
    first_node: Element | null;
    last_p: Element | null;
    last_ins: Element | null;
    used_block_mode: boolean;
  } {
    if (!text) {
      return {
        first_node: null,
        last_p: null,
        last_ins: null,
        used_block_mode: false,
      };
    }

    const xmlDoc = this.doc.part._element.ownerDocument!;
    const lines = text.split(/[\r\n]+/);

    // Resolve the containing <w:p> (current_p) for the anchor.
    let current_p: Element | null = null;
    if (anchor_paragraph !== null) {
      current_p = anchor_paragraph._element;
    } else if (anchor_run !== null) {
      let walker: Element | null = anchor_run._element;
      while (walker && walker.tagName !== "w:p") {
        walker = walker.parentNode as Element | null;
      }
      current_p = walker;
    }

    // Drop trailing empty line. "foo\n\nbar\n\n" splits to
    // ['foo', '', 'bar', '']; that trailing empty is just a terminator, not
    // a real empty paragraph.
    while (lines.length > 1 && lines[lines.length - 1] === "") {
      lines.pop();
    }
    if (lines.length === 0) {
      return {
        first_node: null,
        last_p: null,
        last_ins: null,
        used_block_mode: false,
      };
    }

    // Inspect the first line for heading/list markers.
    const [first_clean, first_style] = this._parse_markdown_style(lines[0]);
    const have_paragraph_context = current_p !== null;
    const block_mode = first_style !== null && have_paragraph_context;

    let first_node: Element | null = null;
    let inline_ins: Element | null = null;

    // ---- INLINE PATH for the first line (when NOT in block mode) ----
    if (!block_mode) {
      inline_ins = this._build_tracked_ins_for_line(
        first_clean === lines[0] ? lines[0] : lines[0],
        anchor_run,
        reuse_id,
        xmlDoc,
      );
      first_node = inline_ins;
      // Caller will attach `inline_ins` to the DOM later — keep it for now.
    }

    // ---- BLOCK PATH for the first line (when in block mode) ----
    // Block-mode first line is just the first extension paragraph below.
    const remaining_lines = block_mode ? lines : lines.slice(1);

    // If there's nothing to do beyond inline, we're done.
    if (remaining_lines.length === 0) {
      return {
        first_node,
        last_p: null,
        last_ins: inline_ins,
        used_block_mode: false,
      };
    }

    if (!current_p) {
      // Multi-paragraph insertion needs a paragraph context. Without one, fall
      // back to the inline result we already built.
      return {
        first_node,
        last_p: null,
        last_ins: inline_ins,
        used_block_mode: false,
      };
    }

    const parent_body = current_p.parentNode as Element | null;
    if (!parent_body) {
      return {
        first_node,
        last_p: null,
        last_ins: inline_ins,
        used_block_mode: false,
      };
    }

    const insertAfterEl = (newNode: Element, ref: Element) => {
      parent_body.insertBefore(newNode, ref.nextSibling);
    };

    let last_p: Element | null = null;
    let last_ins: Element | null = null;
    let after: Element = current_p;

    for (let i = 0; i < remaining_lines.length; i++) {
      const raw_line = remaining_lines[i];
      const [clean_text, style_name] = this._parse_markdown_style(raw_line);

      const new_p = xmlDoc.createElement("w:p");

      if (style_name) {
        // Heading or list style was explicitly authored: replace pPr entirely.
        this._set_paragraph_style(new_p, style_name);
      } else {
        // Inherit pPr from the anchor paragraph (preserves list numbering).
        const existing_pPr = findChild(current_p, "w:pPr");
        if (existing_pPr) {
          new_p.appendChild(existing_pPr.cloneNode(true));
        }
      }

      // Track the paragraph break itself as an insertion.
      let pPr = findChild(new_p, "w:pPr");
      if (!pPr) {
        pPr = xmlDoc.createElement("w:pPr");
        new_p.insertBefore(pPr, new_p.firstChild);
      }
      let rPr = findChild(pPr, "w:rPr");
      if (!rPr) {
        rPr = xmlDoc.createElement("w:rPr");
        pPr.appendChild(rPr);
      }
      const ins_mark = this._create_track_change_tag("w:ins", "", reuse_id);
      rPr.appendChild(ins_mark);

      // Build the content <w:ins>.
      const content_ins = this._build_tracked_ins_for_line(
        clean_text,
        anchor_run,
        reuse_id,
        xmlDoc,
      );
      if (content_ins) {
        new_p.appendChild(content_ins);
      }

      insertAfterEl(new_p, after);
      after = new_p;
      last_p = new_p;
      last_ins = content_ins;

      // In block mode (or if the inline line was completely empty), the first new paragraph IS first_node.
      if (!first_node) {
        first_node = new_p;
      }
    }

    return { first_node, last_p, last_ins, used_block_mode: block_mode };
  }

  /**
   * Builds a single tracked-insert wrapper (<w:ins>) containing one or more
   * <w:r> elements representing the inline markdown segments of `line_text`.
   * Returns null if line_text is empty.
   */
  private _build_tracked_ins_for_line(
    line_text: string,
    anchor_run: Run | null,
    reuse_id: string,
    xmlDoc: Document,
  ): Element | null {
    if (!line_text && line_text !== "") return null;
    const ins = this._create_track_change_tag("w:ins", "", reuse_id);
    const segments = this._parse_inline_markdown(line_text);
    if (segments.length === 0) {
      return null;
    }
    for (const [segText, segProps] of segments) {
      const r = xmlDoc.createElement("w:r");
      // Inherit run formatting (e.g. bold from a heading style) only when we
      // have an anchor run AND we are not overriding via segment props.
      if (anchor_run && anchor_run._element) {
        const anchor_rPr = findChild(anchor_run._element, "w:rPr");
        if (anchor_rPr) {
          const clone = anchor_rPr.cloneNode(true) as Element;
          // Strip vanish / strike to avoid invisible inserts, and emphasis
          // (bold/italic) so inserted replacement text does not silently
          // inherit the anchor run's character formatting (BUG-23-2). Explicit
          // markdown emphasis is re-applied per-segment via _apply_run_props.
          for (const tag of [
            "w:vanish",
            "w:strike",
            "w:dstrike",
            "w:i",
            "w:iCs",
            "w:b",
            "w:bCs",
          ]) {
            const found = findChild(clone, tag);
            if (found) clone.removeChild(found);
          }
          r.appendChild(clone);
        }
      }
      this._apply_run_props(r, segProps, false);
      const t = xmlDoc.createElement("w:t");
      this._set_text_content(t, segText);
      r.appendChild(t);
      ins.appendChild(r);
    }
    return ins;
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

  private _parse_inline_markdown(
    text: string,
    baseStyle: any = {},
  ): [string, any][] {
    if (!text) return [];

    const tokenPattern = /(\*\*.*?\*\*)|(_.*?_)/;
    const match = text.match(tokenPattern);

    if (!match) return [[text, baseStyle]];

    const start = match.index!;
    const raw = match[0];
    const end = start + raw.length;

    const isBold = raw.startsWith("**");
    const innerContent = isBold
      ? raw.substring(2, raw.length - 2)
      : raw.substring(1, raw.length - 1);

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

  private _apply_run_props(
    runElement: Element,
    props: any,
    suppressInherited: boolean = false,
  ) {
    if (!props) {
      if (!suppressInherited) return;
      props = {};
    }

    let rPr = findChild(runElement, "w:rPr");
    if (!rPr && (props.bold || props.italic || suppressInherited)) {
      const doc = runElement.ownerDocument!;
      rPr = doc.createElement("w:rPr");
      runElement.appendChild(rPr);
    }

    if (rPr) {
      const doc = runElement.ownerDocument!;
      if (props.bold) {
        let b = findChild(rPr, "w:b");
        if (!b) {
          b = doc.createElement("w:b");
          rPr.appendChild(b);
        }
        b.setAttribute("w:val", "1");
      } else if (suppressInherited) {
        const b = findChild(rPr, "w:b");
        if (b) rPr.removeChild(b);
      }

      if (props.italic) {
        let i = findChild(rPr, "w:i");
        if (!i) {
          i = doc.createElement("w:i");
          rPr.appendChild(i);
        }
        i.setAttribute("w:val", "1");
      } else if (suppressInherited) {
        const i = findChild(rPr, "w:i");
        if (i) rPr.removeChild(i);
      }
    }
  }

  /**
   * Replaces (or creates) a paragraph's <w:pPr> with a single <w:pStyle> entry
   * pointing at `style_name`. Strips any existing pPr to avoid layering a new
   * heading style on top of a previous list/heading configuration.
   *
   * In Python, the style id is resolved via doc.styles[style_name].style_id and
   * falls back to stripping spaces. Node has no equivalent style cache exposed
   * on `doc`, so we always use the simple "strip spaces" fallback: "Heading 1"
   * becomes the style id "Heading1", "List Number" becomes "ListNumber", etc.
   * This matches python-docx's default style-id convention for the built-in
   * paragraph styles and is what Word writes by default.
   */
  private _set_paragraph_style(p_element: Element, style_name: string) {
    const xmlDoc = p_element.ownerDocument!;

    const existing_pPr = findChild(p_element, "w:pPr");
    if (existing_pPr) {
      p_element.removeChild(existing_pPr);
    }

    const pPr = xmlDoc.createElement("w:pPr");
    const pStyle = xmlDoc.createElement("w:pStyle");
    const style_id = style_name.replace(/\s+/g, "");
    pStyle.setAttribute("w:val", style_id);
    pPr.appendChild(pStyle);

    // pPr is the first child of <w:p> per OOXML schema.
    p_element.insertBefore(pPr, p_element.firstChild);
  }

  private _anchor_reply_comment(parent_id: string, new_id: string) {
    const docEl = this.doc.part._element.ownerDocument!;

    const starts = findAllDescendants(
      this.doc.element,
      "w:commentRangeStart",
    ).filter((n) => n.getAttribute("w:id") === parent_id);
    if (starts.length === 0) return;
    const parent_start = starts[0];

    const new_start = docEl.createElement("w:commentRangeStart");
    new_start.setAttribute("w:id", new_id);
    insertAfter(new_start, parent_start);

    const ends = findAllDescendants(
      this.doc.element,
      "w:commentRangeEnd",
    ).filter((n) => n.getAttribute("w:id") === parent_id);
    if (ends.length === 0) return;
    const parent_end = ends[0];

    const parent_refs = findAllDescendants(
      this.doc.element,
      "w:commentReference",
    ).filter((n) => n.getAttribute("w:id") === parent_id);

    let insertion_point = parent_end;
    if (parent_refs.length > 0) {
      const ref_el = parent_refs[0];
      if (
        ref_el.parentNode &&
        (ref_el.parentNode as Element).tagName === "w:r"
      ) {
        insertion_point = ref_el.parentNode as Element;
      }
    }

    const new_end = docEl.createElement("w:commentRangeEnd");
    new_end.setAttribute("w:id", new_id);
    insertAfter(new_end, insertion_point);

    const ref_run = docEl.createElement("w:r");
    const rPr = docEl.createElement("w:rPr");
    const rStyle = docEl.createElement("w:rStyle");
    rStyle.setAttribute("w:val", "CommentReference");
    rPr.appendChild(rStyle);
    ref_run.appendChild(rPr);

    const ref = docEl.createElement("w:commentReference");
    ref.setAttribute("w:id", new_id);
    ref_run.appendChild(ref);

    insertAfter(ref_run, new_end);
  }

  private _clean_wrapping_comments(element: Element) {
    let first_node: Element = element;
    while (true) {
      const prev = getPreviousElement(first_node);
      if (prev && (prev.tagName === "w:ins" || prev.tagName === "w:del")) {
        first_node = prev;
      } else {
        break;
      }
    }

    let last_node: Element = element;
    while (true) {
      const nxt = getNextElement(last_node);
      if (nxt && (nxt.tagName === "w:ins" || nxt.tagName === "w:del")) {
        last_node = nxt;
      } else {
        break;
      }
    }

    const starts_to_remove: Element[] = [];
    let prev = getPreviousElement(first_node);
    while (prev) {
      if (prev.tagName === "w:commentRangeStart") {
        starts_to_remove.push(prev);
        prev = getPreviousElement(prev);
      } else if (prev.tagName === "w:rPr" || prev.tagName === "w:pPr") {
        prev = getPreviousElement(prev);
      } else {
        break;
      }
    }

    const ends_to_remove: Element[] = [];
    let nxt = getNextElement(last_node);
    while (nxt) {
      if (nxt.tagName === "w:commentRangeEnd") {
        ends_to_remove.push(nxt);
        nxt = getNextElement(nxt);
      } else if (
        nxt.tagName === "w:r" &&
        findAllDescendants(nxt, "w:commentReference").length > 0
      ) {
        ends_to_remove.push(nxt);
        nxt = getNextElement(nxt);
      } else if (nxt.tagName === "w:commentReference") {
        ends_to_remove.push(nxt);
        nxt = getNextElement(nxt);
      } else {
        break;
      }
    }

    const end_ids = new Set<string>();
    for (const e of ends_to_remove) {
      if (e.tagName === "w:commentRangeEnd") {
        const eid = e.getAttribute("w:id");
        if (eid) end_ids.add(eid);
      } else {
        let ref = findAllDescendants(e, "w:commentReference")[0];
        if (!ref && e.tagName === "w:commentReference") ref = e;
        if (ref) {
          const eid = ref.getAttribute("w:id");
          if (eid) end_ids.add(eid);
        }
      }
    }

    for (const s of starts_to_remove) {
      const c_id = s.getAttribute("w:id");
      if (c_id && end_ids.has(c_id)) {
        this.comments_manager.deleteComment(c_id);
        if (s.parentNode) s.parentNode.removeChild(s);
        for (const e of ends_to_remove) {
          let e_id: string | null = null;
          if (e.tagName === "w:commentRangeEnd") {
            e_id = e.getAttribute("w:id");
          } else {
            let ref = findAllDescendants(e, "w:commentReference")[0];
            if (!ref && e.tagName === "w:commentReference") ref = e;
            if (ref) e_id = ref.getAttribute("w:id");
          }
          if (e_id === c_id && e.parentNode) {
            e.parentNode.removeChild(e);
          }
        }
      }
    }
  }

  private _delete_comments_in_element(element: Element) {
    const refs = findAllDescendants(element, "w:commentReference");
    for (const ref of refs) {
      const c_id = ref.getAttribute("w:id");
      if (c_id) {
        this.comments_manager.deleteComment(c_id);
        for (const tag of ["w:commentRangeStart", "w:commentRangeEnd"]) {
          const nodes = findAllDescendants(this.doc.element, tag);
          for (const node of nodes) {
            if (node.getAttribute("w:id") === c_id && node.parentNode) {
              node.parentNode.removeChild(node);
            }
          }
        }
      }
    }
  }

  public validate_edits(edits: any[]): string[] {
    const errors: string[] = [];
    if (!this.mapper.full_text) this.mapper["_build_map"]();

    errors.push(...validate_edit_strings(edits));

    for (let i = 0; i < edits.length; i++) {
      const edit = edits[i];
      if (!edit.target_text) continue;

      let matches = this.mapper.find_all_match_indices(edit.target_text);
      let activeText = this.mapper.full_text;

      if (matches.length === 0) {
        if (!this.clean_mapper)
          this.clean_mapper = new DocumentMapper(this.doc, true);
        matches = this.clean_mapper.find_all_match_indices(edit.target_text);
        if (matches.length > 0) activeText = this.clean_mapper.full_text;
      }

      // BUG-23-5: a copy of the target that lives entirely inside a tracked
      // deletion (<w:del>) is not a live, editable occurrence and must not
      // count toward ambiguity. Drop matches whose overlapping real text is
      // exclusively deleted. Only applies to the raw mapper (the clean mapper
      // already omits deleted text).
      if (activeText === this.mapper.full_text && matches.length > 1) {
        const liveMatches = matches.filter(([start, length]) => {
          const realSpans = this.mapper.spans.filter(
            (s) => s.run !== null && s.end > start && s.start < start + length,
          );
          if (realSpans.length === 0) return true; // virtual-only; keep
          // Keep only if at least one overlapping real span is live (not
          // part of a tracked deletion).
          return realSpans.some((s) => !s.del_id);
        });
        if (liveMatches.length > 0) matches = liveMatches;
      }

      if (matches.length === 0) {
        errors.push(
          `- Edit ${i + 1} Failed: Target text not found in document:\n  "${edit.target_text}"`,
        );
      } else if (matches.length > 1) {
        const positions: [number, number][] = matches.map(([start, length]) => [
          start,
          start + length,
        ]);
        errors.push(
          format_ambiguity_error(
            i + 1,
            edit.target_text,
            activeText,
            positions,
          ),
        );
      }

      // BUG-23-4: when the effective (context-trimmed) target spans a
      // paragraph boundary with real body text on BOTH sides, we must reject
      // the modification to prevent silent corruption of the paragraph structure.
      if (matches.length === 1) {
        const [m_start, m_len] = matches[0];
        const matched = activeText.substring(m_start, m_start + m_len);
        const [pfx, sfx] = trim_common_context(matched, edit.new_text || "");
        const t_end = matched.length - sfx;
        const final_target = matched.substring(pfx, t_end);
        const final_new = (edit.new_text || "").substring(pfx, (edit.new_text || "").length - sfx);
        if (final_target.includes("\n\n")) {
          if (final_new.includes("\n\n")) {
            const parts = matched.split("\n\n");
            if (parts.length >= 2 && parts[0].trim() !== "" && parts[parts.length - 1].trim() !== "") {
              errors.push(
                `- Edit ${i + 1} Failed: target_text spans a paragraph boundary with body text on both sides. The paragraph break is a structural element, not literal text, so it cannot be replaced as a single span without corrupting the document. Split this into one edit per paragraph.`,
              );
            }
          } else {
            const parts = final_target.split("\n\n");
            if (parts.length >= 2 && parts[0].trim() !== "" && parts[parts.length - 1].trim() !== "") {
              errors.push(
                `- Edit ${i + 1} Failed: target_text spans a paragraph boundary with body text on both sides. The paragraph break is a structural element, not literal text, so it cannot be replaced as a single span without corrupting the document. Split this into one edit per paragraph.`,
              );
            }
          }
        }
      }

      for (const [start, length] of matches) {
        const spans = this.mapper.spans.filter(
          (s) => s.end > start && s.start < start + length,
        );
        const nestedAuthors = new Set<string>();
        for (const s of spans) {
          if (s.ins_id) {
            const insNodes = findAllDescendants(
              this.doc.element,
              "w:ins",
            ).filter((n) => n.getAttribute("w:id") === s.ins_id);
            if (insNodes.length > 0) {
              const auth = insNodes[0].getAttribute("w:author");
              if (auth && auth !== this.author) nestedAuthors.add(auth);
            }
          }
        }
        if (nestedAuthors.size > 0) {
          errors.push(
            `- Edit ${i + 1} Failed: Modification targets an active insertion from another author (${Array.from(nestedAuthors).join(", ")}). Accept that change first or scope your edit outside of it.`,
          );
        }
      }
    }
    return errors;
  }

  public validate_review_actions(actions: any[]): string[] {
    const errors: string[] = [];
    for (let i = 0; i < actions.length; i++) {
      const action = actions[i];
      const type = action.type;

      if (type === "reply") {
        const cid = action.target_id.replace("Com:", "");
        let found = false;
        const part = this.doc.pkg.parts.find(
          (p) =>
            p.contentType ===
            "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        );
        if (part) {
          const comments = findAllDescendants(part._element, "w:comment");
          found = comments.some((c) => c.getAttribute("w:id") === cid);
        }
        if (!found) {
          errors.push(
            `- Action ${i + 1} Failed: Target comment ID ${action.target_id} not found.`,
          );
        }
      } else if (type === "accept" || type === "reject") {
        const target_id = action.target_id.replace("Chg:", "");
        const all_ins = findAllDescendants(this.doc.element, "w:ins").filter(
          (n) => n.getAttribute("w:id") === target_id,
        );
        const all_del = findAllDescendants(this.doc.element, "w:del").filter(
          (n) => n.getAttribute("w:id") === target_id,
        );
        if (all_ins.length === 0 && all_del.length === 0) {
          errors.push(
            `- Action ${i + 1} Failed: Target ID ${action.target_id} not found.`,
          );
        }
      }
    }
    return errors;
  }

  public process_batch(changes: DocumentChange[], dry_run: boolean = false): any {
    if (dry_run) {
      const baselines = new Map<any, Element>();
      for (const part of this.doc.pkg.parts) {
        if (part._element) {
          baselines.set(part, part._element.cloneNode(true) as Element);
        }
      }
      try {
        return this._process_batch_internal(changes, true);
      } finally {
        for (const [part, originalEl] of baselines.entries()) {
          const doc = part._element.ownerDocument;
          if (doc && doc.documentElement) {
            doc.replaceChild(originalEl, doc.documentElement);
          }
          part._element = originalEl;
        }
        this.mapper = new DocumentMapper(this.doc);
        this.comments_manager = new CommentsManager(this.doc);
        this.clean_mapper = null;
      }
    } else {
      return this._process_batch_internal(changes, false);
    }
  }

  private _process_batch_internal(changes: DocumentChange[], dry_run_mode: boolean = false): any {
    this.skipped_details = [];
    const actions = changes.filter((c) =>
      ["accept", "reject", "reply"].includes(c.type),
    );
    const edits = changes.filter(
      (c) => !["accept", "reject", "reply"].includes(c.type),
    );

    // BUG-7: Unified single-pass validation in wet-run / standard mode
    if (!dry_run_mode) {
      const all_errors: string[] = [];
      if (actions.length > 0) {
        all_errors.push(...this.validate_review_actions(actions));
      }
      if (edits.length > 0) {
        all_errors.push(...this.validate_edits(edits));
      }
      if (all_errors.length > 0) {
        throw new BatchValidationError(all_errors);
      }
    } else {
      if (actions.length > 0) {
        const action_errors = this.validate_review_actions(actions);
        if (action_errors.length > 0) {
          throw new BatchValidationError(action_errors);
        }
      }
    }

    let applied_actions = 0;
    let skipped_actions = 0;
    if (actions.length > 0) {
      const res = this.apply_review_actions(actions);
      applied_actions = res[0];
      skipped_actions = res[1];
      if (skipped_actions > 0) {
        throw new BatchValidationError(this.skipped_details);
      }
      if (applied_actions > 0) {
        this.mapper["_build_map"]();
        if (this.clean_mapper) this.clean_mapper["_build_map"]();
      }
    }

    const edits_reports: any[] = [];
    let applied_edits = 0;
    let skipped_edits = 0;

    if (edits.length > 0) {
      if (dry_run_mode) {
        for (const edit of edits) {
          const single_errors = this.validate_edits([edit]);
          const warning = this._check_punctuation_warning((edit as any).target_text || "");
          if (single_errors.length > 0) {
            skipped_edits++;
            edits_reports.push({
              status: "failed",
              target_text: (edit as any).target_text || "",
              new_text: (edit as any).new_text || "",
              warning: warning,
              error: single_errors[0],
              critic_markup: null,
              clean_text: null,
            });
            continue;
          }
          const res = this.apply_edits([edit]);
          const applied = res[0];
          if (applied > 0) {
            applied_edits++;
            const previews = this._build_edit_context_previews(edit);
            edits_reports.push({
              status: "applied",
              target_text: (edit as any).target_text || "",
              new_text: (edit as any).new_text || "",
              warning: warning,
              error: null,
              critic_markup: previews[0],
              clean_text: previews[1],
            });
          } else {
            skipped_edits++;
            const error_msg = this.skipped_details.length > 0 ? this.skipped_details[this.skipped_details.length - 1] : "Failed to apply edit";
            edits_reports.push({
              status: "failed",
              target_text: (edit as any).target_text || "",
              new_text: (edit as any).new_text || "",
              warning: warning,
              error: error_msg,
              critic_markup: null,
              clean_text: null,
            });
          }
        }
      } else {
        const errors = this.validate_edits(edits);
        if (errors.length > 0) {
          throw new BatchValidationError(errors);
        }
        const cloned_edits = edits.map(e => JSON.parse(JSON.stringify(e)));
        const res = this.apply_edits(cloned_edits);
        applied_edits = res[0];
        skipped_edits = res[1];

        for (const edit of cloned_edits) {
          const success = (edit as any)._applied_status || false;
          const error_msg = (edit as any)._error_msg || null;
          const warning = this._check_punctuation_warning((edit as any).target_text || "");
          let critic_markup = null;
          let clean_text = null;
          if (success) {
            const previews = this._build_edit_context_previews(edit);
            critic_markup = previews[0];
            clean_text = previews[1];
          }
          edits_reports.push({
            status: success ? "applied" : "failed",
            target_text: (edit as any).target_text || "",
            new_text: (edit as any).new_text || "",
            warning: warning,
            error: error_msg,
            critic_markup: critic_markup,
            clean_text: clean_text,
          });
        }
      }
    }

    return {
      actions_applied: applied_actions,
      actions_skipped: skipped_actions,
      edits_applied: applied_edits,
      edits_skipped: skipped_edits,
      skipped_details: this.skipped_details,
      edits: edits_reports,
      engine: "node",
      version: "1.9.0",
    };
  }

  public apply_edits(edits: any[]): [number, number] {
    let applied = 0;
    let skipped = 0;
    const resolved_edits: [any, string | null][] = [];

    for (const edit of edits) {
      edit._applied_status = false;
      edit._error_msg = null;
    }

    for (const edit of edits) {
      if (
        edit._resolved_start_idx !== undefined &&
        edit._resolved_start_idx !== null
      ) {
        resolved_edits.push([edit, edit.new_text || null]);
      } else if (
        edit._match_start_index !== undefined &&
        edit._match_start_index !== null
      ) {
        edit._resolved_start_idx = edit._match_start_index;
        resolved_edits.push([edit, edit.new_text || null]);
      } else if (edit.type === "insert_row" || edit.type === "delete_row") {
        let matches = this.mapper.find_all_match_indices(edit.target_text);
        if (matches.length === 0) {
          if (!this.clean_mapper) {
            this.clean_mapper = new DocumentMapper(this.doc, true);
          }
          matches = this.clean_mapper.find_all_match_indices(edit.target_text);
        }

        if (matches.length > 0) {
          edit._resolved_start_idx = matches[0][0];
          resolved_edits.push([edit, null]);
        } else {
          skipped++;
          edit._applied_status = false;
          const target_snippet = (edit.target_text || "").trim().substring(0, 40);
          const msg = `- Failed to locate row target: '${target_snippet}...'`;
          this.skipped_details.push(msg);
          edit._error_msg = msg;
        }
      } else {
        const resolved = this._pre_resolve_heuristic_edit(edit);
        if (resolved) {
          if (Array.isArray(resolved)) {
            for (const r of resolved) {
              r._resolved_start_idx = r._match_start_index;
              r._parent_edit_ref = edit;
              if (edit._resolved_start_idx === undefined || edit._resolved_start_idx === null) {
                edit._resolved_start_idx = r._resolved_start_idx;
              }
              if (!edit._resolved_proxy_edit) {
                edit._resolved_proxy_edit = r;
              }
              resolved_edits.push([r, r.new_text]);
            }
          } else {
            resolved._resolved_start_idx = resolved._match_start_index;
            resolved._parent_edit_ref = edit;
            edit._resolved_start_idx = resolved._resolved_start_idx;
            edit._resolved_proxy_edit = resolved;
            resolved_edits.push([resolved, (resolved as any).new_text]);
          }
        } else {
          skipped++;
          edit._applied_status = false;
          const display_text = edit.target_text || "insertion";
          const target_snippet = display_text.trim().substring(0, 40);
          const msg = `- Failed to apply edit targeting: '${target_snippet}...'`;
          this.skipped_details.push(msg);
          edit._error_msg = msg;
        }
      }
    }

    resolved_edits.sort(
      (a, b) => (b[0]._resolved_start_idx || 0) - (a[0]._resolved_start_idx || 0),
    );
    const occupied_ranges: [number, number][] = [];

    for (const [edit, orig_new] of resolved_edits) {
      const start = edit._resolved_start_idx || 0;
      const end = start + (edit.target_text ? edit.target_text.length : 0);

      const overlaps = occupied_ranges.some(
        ([occ_start, occ_end]) => start < occ_end && end > occ_start,
      );
      if (overlaps) {
        skipped++;
        const display_text = edit.target_text || "insertion";
        const target_snippet = display_text.trim().substring(0, 40);
        const msg = `- Skipped overlapping edit targeting: '${target_snippet}...'`;
        this.skipped_details.push(msg);
        edit._applied_status = false;
        edit._error_msg = msg;
        const parent = edit._parent_edit_ref;
        if (parent) {
          parent._applied_status = false;
          parent._error_msg = msg;
        }
        continue;
      }

      let success = false;
      if (edit.type === "modify") {
        success = this._apply_single_edit_indexed(edit, orig_new, false);
      } else if (edit.type === "insert_row" || edit.type === "delete_row") {
        success = this._apply_table_edit(edit, false);
      }

      if (success) {
        applied++;
        occupied_ranges.push([start, end]);
        edit._applied_status = true;
        const parent = edit._parent_edit_ref;
        if (parent) {
          parent._applied_status = true;
        }
      } else {
        skipped++;
        const display_text = edit.target_text || "insertion";
        const target_snippet = display_text.trim().substring(0, 40);
        const msg = `- Failed to apply edit targeting: '${target_snippet}...'`;
        this.skipped_details.push(msg);
        edit._applied_status = false;
        edit._error_msg = msg;
        const parent = edit._parent_edit_ref;
        if (parent) {
          if (!parent._applied_status) {
            parent._applied_status = false;
            parent._error_msg = msg;
          }
        }
      }
    }

    return [applied, skipped];
  }

  public apply_review_actions(actions: any[]): [number, number] {
    let applied = 0;
    let skipped = 0;

    for (const action of actions) {
      const type = action.type;
      if (type === "reply") {
        const cid = action.target_id.replace("Com:", "");
        const new_id = this.comments_manager.addComment(
          this.author,
          action.text,
          cid,
        );
        this._anchor_reply_comment(cid, new_id);
        applied++;
        continue;
      }

      const target_id = action.target_id.replace("Chg:", "");
      const all_ins = findAllDescendants(this.doc.element, "w:ins").filter(
        (n) => n.getAttribute("w:id") === target_id,
      );
      const all_del = findAllDescendants(this.doc.element, "w:del").filter(
        (n) => n.getAttribute("w:id") === target_id,
      );
      const all_nodes = [...all_ins, ...all_del];

      if (all_nodes.length === 0) {
        skipped++;
        this.skipped_details.push(
          `- Failed to apply action: Target ID ${action.target_id} not found.`,
        );
        continue;
      }

      for (const node of all_nodes) {
        const is_ins = node.tagName === "w:ins";
        const parent_tag = node.parentNode
          ? (node.parentNode as Element).tagName
          : "";
        const is_trPr = parent_tag === "w:trPr";

        if (type === "accept") {
          if (is_ins) {
            this._clean_wrapping_comments(node);
            if (is_trPr) node.parentNode?.removeChild(node);
            else {
              while (node.firstChild)
                node.parentNode?.insertBefore(node.firstChild, node);
              node.parentNode?.removeChild(node);
            }
          } else {
            this._clean_wrapping_comments(node);
            this._delete_comments_in_element(node);
            if (is_trPr) {
              const tr = node.parentNode?.parentNode;
              tr?.parentNode?.removeChild(tr);
            } else {
              node.parentNode?.removeChild(node);
            }
          }
        } else if (type === "reject") {
          if (is_ins) {
            this._clean_wrapping_comments(node);
            this._delete_comments_in_element(node);
            if (is_trPr) {
              const tr = node.parentNode?.parentNode;
              tr?.parentNode?.removeChild(tr);
            } else node.parentNode?.removeChild(node);
          } else {
            this._clean_wrapping_comments(node);
            if (is_trPr) node.parentNode?.removeChild(node);
            else {
              const delTexts = Array.from(
                node.getElementsByTagName("w:delText"),
              );
              for (const dt of delTexts) {
                const t = dt.ownerDocument!.createElement("w:t");
                t.textContent = dt.textContent;
                if (dt.hasAttribute("xml:space"))
                  t.setAttribute("xml:space", "preserve");
                dt.parentNode?.replaceChild(t, dt);
              }
              while (node.firstChild)
                node.parentNode?.insertBefore(node.firstChild, node);
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
    const start_idx = edit._resolved_start_idx !== undefined && edit._resolved_start_idx !== null ? edit._resolved_start_idx : (edit._match_start_index || 0);
    const [anchor_run, anchor_para] = this.mapper.get_insertion_anchor(
      start_idx,
      rebuild_map,
    );

    let target_element: Element | null = null;
    if (anchor_run) target_element = anchor_run._element;
    else if (anchor_para) target_element = anchor_para._element;

    if (!target_element) return false;

    let tr: Element | null = target_element;
    while (tr && tr.tagName !== "w:tr") tr = tr.parentNode as Element;
    if (!tr) return false;

    if (edit.type === "delete_row") {
      let trPr = findChild(tr, "w:trPr");
      if (!trPr) {
        trPr = tr.ownerDocument!.createElement("w:trPr");
        tr.insertBefore(trPr, tr.firstChild);
      }
      trPr.appendChild(this._create_track_change_tag("w:del"));
      return true;
    } else if (edit.type === "insert_row") {
      const new_tr = tr.ownerDocument!.createElement("w:tr");
      const trPr = tr.ownerDocument!.createElement("w:trPr");
      new_tr.appendChild(trPr);
      trPr.appendChild(this._create_track_change_tag("w:ins"));
      for (const cellText of edit.cells) {
        const tc = tr.ownerDocument!.createElement("w:tc");
        const p = tr.ownerDocument!.createElement("w:p");
        const r = tr.ownerDocument!.createElement("w:r");
        const t = tr.ownerDocument!.createElement("w:t");
        t.textContent = cellText;
        if (cellText.trim() !== cellText)
          t.setAttribute("xml:space", "preserve");
        r.appendChild(t);
        p.appendChild(r);
        tc.appendChild(p);
        new_tr.appendChild(tc);
      }
      if (edit.position === "above") tr.parentNode?.insertBefore(new_tr, tr);
      else insertAfter(new_tr, tr);
      return true;
    }
    return false;
  }

  /**
   * Returns the first match of `target_text` in the raw mapper that is NOT
   * entirely contained within a tracked deletion (<w:del>). Tracked-deleted
   * copies are not live, editable text, so an edit must resolve to a live
   * occurrence even when a dead copy appears earlier in the document
   * (BUG-23-5). Falls back to the plain first match when no live copy is
   * found (e.g. fuzzy/normalized matches the span filter cannot align).
   */
  private _first_live_match(target_text: string): [number, number] {
    const all = this.mapper.find_all_match_indices(target_text);
    if (all.length <= 1) {
      return this.mapper.find_match_index(target_text);
    }
    for (const [start, length] of all) {
      const realSpans = this.mapper.spans.filter(
        (s) => s.run !== null && s.end > start && s.start < start + length,
      );
      if (realSpans.length === 0) return [start, length];
      if (realSpans.some((s) => !s.del_id)) return [start, length];
    }
    return this.mapper.find_match_index(target_text);
  }

  private _pre_resolve_heuristic_edit(edit: any): any {
    if (!edit.target_text) return null;

    let [start_idx, match_len] = this._first_live_match(edit.target_text);
    let use_clean_map = false;

    if (start_idx === -1) {
      if (!this.clean_mapper)
        this.clean_mapper = new DocumentMapper(this.doc, true);
      [start_idx, match_len] = this.clean_mapper.find_match_index(
        edit.target_text,
      );
      if (start_idx !== -1) use_clean_map = true;
      else return null;
    }

    const active_mapper = use_clean_map ? this.clean_mapper! : this.mapper;
    const effective_new_text = edit.new_text || "";
    const actual_doc_text = this.mapper.full_text.substring(
      start_idx,
      start_idx + match_len,
    );

    if (
      actual_doc_text === effective_new_text ||
      edit.target_text === effective_new_text
    ) {
      return {
        type: "modify",
        target_text: actual_doc_text,
        new_text: actual_doc_text,
        comment: edit.comment,
        _match_start_index: start_idx,
        _internal_op: "COMMENT_ONLY",
        _active_mapper_ref: active_mapper,
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
      const [prefix_len, suffix_len] = trim_common_context(
        actual_doc_text,
        effective_new_text,
      );
      const t_end = actual_doc_text.length - suffix_len;
      const n_end = effective_new_text.length - suffix_len;

      final_target = actual_doc_text.substring(prefix_len, t_end);
      final_new = effective_new_text.substring(prefix_len, n_end);
      effective_start_idx = start_idx + prefix_len;

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
      _active_mapper_ref: active_mapper,
    };
  }

  private _apply_single_edit_indexed(
    edit: any,
    orig_new: string | null,
    rebuild_map: boolean,
  ): boolean {
    let op = edit._internal_op;
    const active_mapper = edit._active_mapper_ref || this.mapper;
    const start_idx = edit._resolved_start_idx !== undefined && edit._resolved_start_idx !== null ? edit._resolved_start_idx : (edit._match_start_index || 0);
    const length = edit.target_text ? edit.target_text.length : 0;

    const del_id = ["DELETION", "MODIFICATION"].includes(op)
      ? this._getNextId()
      : null;
    const ins_id = ["INSERTION", "MODIFICATION"].includes(op)
      ? this._getNextId()
      : null;

    if (op === "COMMENT_ONLY") {
      // Resolve the runs covering [start_idx, start_idx+length) and attach a
      // comment around them. No tracked-change is produced.
      const target_runs = active_mapper.find_target_runs_by_index(
        start_idx,
        length,
        rebuild_map,
      );
      if (target_runs.length === 0) return false;
      if (!edit.comment) return true;

      const first_el = target_runs[0]._element;
      const last_el = target_runs[target_runs.length - 1]._element;

      // Walk up from the first/last run to their containing <w:p>.
      let start_p: Element | null = first_el;
      while (start_p && start_p.tagName !== "w:p")
        start_p = start_p.parentNode as Element;
      let end_p: Element | null = last_el;
      while (end_p && end_p.tagName !== "w:p")
        end_p = end_p.parentNode as Element;
      if (!start_p || !end_p) return false;

      // first_el / last_el may live inside a <w:ins> or <w:del>. We need their
      // top-level child-of-paragraph ancestor so the comment markers become
      // siblings of those wrappers, not children.
      const ascend_to_paragraph_child = (el: Element, p: Element): Element => {
        let cur: Element = el;
        while (cur.parentNode && cur.parentNode !== p) {
          cur = cur.parentNode as Element;
        }
        return cur;
      };
      const first_anchor = ascend_to_paragraph_child(first_el, start_p);
      const last_anchor = ascend_to_paragraph_child(last_el, end_p);

      if (start_p === end_p) {
        this._attach_comment(start_p, first_anchor, last_anchor, edit.comment);
      } else {
        this._attach_comment_spanning(
          start_p,
          first_anchor,
          end_p,
          last_anchor,
          edit.comment,
        );
      }
      return true;
    }
    if (op === "INSERTION") {
      const [anchor_run, anchor_para] = active_mapper.get_insertion_anchor(
        start_idx,
        rebuild_map,
      );
      if (!anchor_run && !anchor_para) return false;

      // BUG-23-3: a prefix insertion whose new_text ends in a paragraph break
      // (e.g. "Summary\n\n" inserted before "Conclusion") must become a NEW
      // paragraph placed BEFORE the anchor paragraph, not inline text merged
      // into a neighbouring paragraph. _track_insert_multiline drops the
      // trailing break and inlines the remainder, which both loses the
      // paragraph boundary and mis-orders the content. Handle this case here.
      const _bug233_new = edit.new_text || "";
      const _bug233_trailing_break = /\n\s*$/.test(_bug233_new);
      let _bug233_target_para: Element | null = null;
      {
        const startingSpans = active_mapper.spans.filter(
          (s: TextSpan) => s.paragraph !== null && s.start === start_idx,
        );
        if (startingSpans.length > 0 && startingSpans[0].paragraph) {
          _bug233_target_para = startingSpans[0].paragraph._element;
        }
      }
      if (
        _bug233_trailing_break &&
        _bug233_target_para &&
        _bug233_target_para.parentNode
      ) {
        const body = _bug233_target_para.parentNode as Element;
        const xmlDoc = this.doc.part._element.ownerDocument!;
        const lines = _bug233_new.split(/[\r\n]+/).filter((l: string) => l !== "");
        let firstNew: Element | null = null;
        let lastNew: Element | null = null;
        let lastIns: Element | null = null;
        for (const raw_line of lines) {
          const [clean_text, style_name] = this._parse_markdown_style(raw_line);
          const new_p = xmlDoc.createElement("w:p");
          if (style_name) {
            this._set_paragraph_style(new_p, style_name);
          } else {
            const existing_pPr = findChild(_bug233_target_para, "w:pPr");
            if (existing_pPr) new_p.appendChild(existing_pPr.cloneNode(true));
          }
          let pPr = findChild(new_p, "w:pPr");
          if (!pPr) {
            pPr = xmlDoc.createElement("w:pPr");
            new_p.insertBefore(pPr, new_p.firstChild);
          }
          let rPr = findChild(pPr, "w:rPr");
          if (!rPr) {
            rPr = xmlDoc.createElement("w:rPr");
            pPr.appendChild(rPr);
          }
          rPr.appendChild(this._create_track_change_tag("w:ins", "", ins_id!));
          const content_ins = this._build_tracked_ins_for_line(
            clean_text,
            anchor_run,
            ins_id!,
            xmlDoc,
          );
          if (content_ins) new_p.appendChild(content_ins);
          body.insertBefore(new_p, _bug233_target_para);
          if (!firstNew) firstNew = new_p;
          lastNew = new_p;
          lastIns = content_ins;
        }
        if (firstNew) {
          if (edit.comment && lastNew && lastIns) {
            const ascend = (el: Element, p: Element): Element => {
              let cur: Element = el;
              while (cur.parentNode && cur.parentNode !== p)
                cur = cur.parentNode as Element;
              return cur;
            };
            const startIns =
              findAllDescendants(firstNew, "w:ins")[0] || firstNew;
            this._attach_comment_spanning(
              firstNew,
              ascend(startIns, firstNew),
              lastNew,
              ascend(lastIns, lastNew),
              edit.comment,
            );
          }
          return true;
        }
      }

      const result = this._track_insert_multiline(
        edit.new_text || "",
        anchor_run,
        anchor_para,
        ins_id!,
      );

      if (!result.first_node) return false;

      // Place the inline <w:ins> (or block-mode first paragraph) into the DOM.
      // Block-mode first_node is already a freshly-inserted <w:p>; only the
      // inline case needs DOM splicing here.
      const is_inline_first = result.first_node.tagName === "w:ins";
      if (is_inline_first) {
        if (anchor_run) {
          insertAfter(result.first_node, anchor_run._element);
        } else if (anchor_para) {
          anchor_para._element.appendChild(result.first_node);
        }
      }

      // Attach the comment if requested. Anchor depends on whether we created
      // additional paragraphs.
      if (edit.comment) {
        const ascend_to_paragraph_child = (
          el: Element,
          p: Element,
        ): Element => {
          let cur: Element = el;
          while (cur.parentNode && cur.parentNode !== p) {
            cur = cur.parentNode as Element;
          }
          return cur;
        };

        if (result.last_p && result.last_ins) {
          // Multi-paragraph: anchor from first_node (in its host paragraph)
          // through last_ins (inside last_p).
          let start_p: Element | null = result.first_node;
          while (start_p && start_p.tagName !== "w:p")
            start_p = start_p.parentNode as Element;
          if (start_p) {
            let first_anchor_target = result.first_node;
            if (result.first_node.tagName === "w:p") {
              first_anchor_target = findAllDescendants(result.first_node, "w:ins")[0] || result.first_node;
            }
            const start_anchor = ascend_to_paragraph_child(
              first_anchor_target,
              start_p,
            );
            const end_anchor = ascend_to_paragraph_child(
              result.last_ins,
              result.last_p,
            );
            this._attach_comment_spanning(
              start_p,
              start_anchor,
              result.last_p,
              end_anchor,
              edit.comment,
            );
            }
          } else {
            // Inline only: anchor around first_node in its host paragraph.
            let host_p: Element | null = result.first_node;
            while (host_p && host_p.tagName !== "w:p")
            host_p = host_p.parentNode as Element;
            if (host_p) {
            let first_anchor_target = result.first_node;
            if (result.first_node.tagName === "w:p") {
              first_anchor_target = findAllDescendants(result.first_node, "w:ins")[0] || result.first_node;
            }
            const anchor = ascend_to_paragraph_child(first_anchor_target, host_p);
            this._attach_comment(host_p, anchor, anchor, edit.comment);
            }
          }
        }
      return true;
    }

    // DELETION / MODIFICATION
    const target_runs = active_mapper.find_target_runs_by_index(
      start_idx,
      length,
      rebuild_map,
    );
    const virtual_spans = active_mapper.get_virtual_spans_in_range(start_idx, length);

    if (target_runs.length === 0 && virtual_spans.length === 0) return false;

    const affected_ps = new Set<Element>();
    for (const run of target_runs) {
      let p: Element | null = run._element.parentNode as Element;
      while (p && p.tagName !== "w:p") p = p.parentNode as Element;
      if (p) affected_ps.add(p);
    }

    let first_del: Element | null = null;
    let last_del: Element | null = null;
    for (const run of target_runs) {
      const del_tag = this._create_track_change_tag("w:del", "", del_id);
      const new_run = run._element.cloneNode(true) as Element;

      const tNodes = Array.from(new_run.getElementsByTagName("w:t"));
      tNodes.forEach((t) => {
        const delText = new_run.ownerDocument!.createElement("w:delText");
        delText.textContent = t.textContent;
        if (t.hasAttribute("xml:space"))
          delText.setAttribute("xml:space", "preserve");
        new_run.replaceChild(delText, t);
      });

      del_tag.appendChild(new_run);
      run._element.parentNode?.replaceChild(del_tag, run._element);
      if (first_del === null) first_del = del_tag;
      last_del = del_tag;
    }

    let ins_elem: Element | null = null;
    let mod_last_p: Element | null = null;
    let mod_last_ins: Element | null = null;

    if (op === "MODIFICATION" && edit.new_text && last_del) {
      // Resolve a paragraph anchor: the <w:p> hosting last_del.
      let mod_anchor_para_el: Element | null = last_del;
      while (mod_anchor_para_el && mod_anchor_para_el.tagName !== "w:p") {
        mod_anchor_para_el = mod_anchor_para_el.parentNode as Element | null;
      }
      const mod_anchor_para: Paragraph | null = mod_anchor_para_el
        ? new Paragraph(mod_anchor_para_el, null)
        : null;

      // The "anchor run" for style inheritance is the run we just deleted; reuse
      // the deleted run's rPr by sourcing the original target run if available.
      const style_source_run: Run | null =
        target_runs.length > 0 ? target_runs[target_runs.length - 1] : null;

      const result = this._track_insert_multiline(
        edit.new_text,
        style_source_run,
        mod_anchor_para,
        ins_id!,
      );

      if (result.first_node) {
        const is_inline_first = result.first_node.tagName === "w:ins";
        if (is_inline_first) {
          // Inline: place the first <w:ins> immediately after last_del.
          insertAfter(result.first_node, last_del);
          ins_elem = result.first_node;
        } else {
          // Block-mode first paragraph was already inserted after the anchor
          // paragraph by the helper. We still need ins_elem for comment fallback.
          ins_elem = result.last_ins;
        }
        mod_last_p = result.last_p;
        mod_last_ins = result.last_ins;
      }
    }

    // PHASE 2: OOXML Paragraph Merge Protocol
    if (op === "DELETION" || op === "MODIFICATION") {
      if (op === "MODIFICATION" && target_runs.length === 0 && virtual_spans.length > 0 && edit.new_text) {
        const first_span = virtual_spans[0];
        if (first_span.paragraph) {
          const p1_el = first_span.paragraph._element;
          const last_runs = findAllDescendants(p1_el, "w:r");
          const anchor = last_runs.length > 0 ? new Run(last_runs[last_runs.length - 1], first_span.paragraph) : null;
          
          const result = this._track_insert_multiline(
            edit.new_text,
            anchor,
            first_span.paragraph,
            ins_id!
          );
          if (result.first_node) {
            p1_el.appendChild(result.first_node);
          }
        }
      }

      for (const span of [...virtual_spans].reverse()) {
        if (span.paragraph) {
          const p1_element = span.paragraph._element;
          let p2_element = getNextElement(p1_element);
          while (p2_element && p2_element.tagName !== "w:p") {
            p2_element = getNextElement(p2_element);
          }

          if (p2_element && p2_element.tagName === "w:p") {
            let pPr = findChild(p1_element, "w:pPr");
            if (!pPr) {
              pPr = p1_element.ownerDocument!.createElement("w:pPr") as Element;
              p1_element.insertBefore(pPr, p1_element.firstChild as Node | null);
            }
            let rPr = findChild(pPr!, "w:rPr");
            if (!rPr) {
              rPr = p1_element.ownerDocument!.createElement("w:rPr") as Element;
              pPr!.appendChild(rPr);
            }
            const del_mark = this._create_track_change_tag("w:del");
            rPr!.appendChild(del_mark);

            const children = Array.from(p2_element.childNodes);
            for (const child of children) {
              if (child.nodeType === 1 && (child as Element).tagName === "w:pPr") {
                continue;
              }
              p1_element.appendChild(child);
            }

            if (p2_element.parentNode) {
              p2_element.parentNode.removeChild(p2_element);
            }
          }
        }
      }
    }

    // Attach comment around the modification or deletion if requested.
    if (edit.comment && first_del !== null) {
      // Resolve the comment END anchor. For multi-paragraph modifications,
      // the end anchor lives in the LAST inserted paragraph (mod_last_p);
      // otherwise it's the inline ins/del in the source paragraph.
      let end_anchor_el: Element;
      let end_p: Element | null;

      if (mod_last_p && mod_last_ins) {
        end_anchor_el = mod_last_ins;
        end_p = mod_last_p;
      } else {
        const final_anchor: Element = ins_elem !== null ? ins_elem : last_del!;
        end_anchor_el = final_anchor;
        end_p = final_anchor;
        while (end_p && end_p.tagName !== "w:p")
          end_p = end_p.parentNode as Element | null;
      }

      let start_p: Element | null = first_del;
      while (start_p && start_p.tagName !== "w:p")
        start_p = start_p.parentNode as Element | null;
      if (!start_p || !end_p) return true;

      const ascend_to_paragraph_child = (el: Element, p: Element): Element => {
        let cur: Element = el;
        while (cur.parentNode && cur.parentNode !== p) {
          cur = cur.parentNode as Element;
        }
        return cur;
      };
      const start_anchor = ascend_to_paragraph_child(first_del, start_p);
      const end_anchor = ascend_to_paragraph_child(end_anchor_el, end_p);

      if (start_p === end_p) {
        this._attach_comment(start_p, start_anchor, end_anchor, edit.comment);
      } else {
        this._attach_comment_spanning(
          start_p,
          start_anchor,
          end_p,
          end_anchor,
          edit.comment,
        );
      }
    }

    // PHASE 2: Check for orphaned paragraphs with zero visible content remaining
    for (const p_elem of affected_ps) {
      let has_visible = false;
      for (const tag of ["w:t", "w:tab", "w:br"]) {
        const nodes = findAllDescendants(p_elem, tag);
        for (const node of nodes) {
          let is_deleted = false;
          let curr = node.parentNode as Element | null;
          while (curr && curr !== p_elem.parentNode) {
            if (curr.tagName === "w:del") {
              is_deleted = true;
              break;
            }
            curr = curr.parentNode as Element | null;
          }
          if (!is_deleted) {
            if (tag === "w:t" && !node.textContent) continue;
            has_visible = true;
            break;
          }
        }
        if (has_visible) break;
      }

      if (!has_visible) {
        let pPr = findChild(p_elem, "w:pPr");
        if (!pPr) {
          pPr = p_elem.ownerDocument!.createElement("w:pPr") as Element;
          p_elem.insertBefore(pPr, p_elem.firstChild as Node | null);
        }
        let rPr = findChild(pPr!, "w:rPr");
        if (!rPr) {
          rPr = p_elem.ownerDocument!.createElement("w:rPr") as Element;
          pPr!.appendChild(rPr);
        }
        if (!findChild(rPr!, "w:del")) {
          const del_mark = this._create_track_change_tag("w:del");
          rPr!.appendChild(del_mark);
        }
      }
    }

    return true;
  }
}
