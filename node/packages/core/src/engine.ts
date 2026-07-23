import { DocumentObject } from "./docx/bridge.js";
import { Paragraph, Table, Run, DocxEvent } from "./docx/primitives.js";
import { DocumentMapper, TextSpan } from "./mapper.js";
import { CommentsManager, extract_comments_data } from "./comments.js";
import {
  ModifyText,
  InsertTableRow,
  DeleteTableRow,
  AcceptChange,
  RejectChange,
  ReplyComment,
  DocumentChange,
} from "./models.js";
import { trim_common_context, generate_edits_from_text } from "./diff.js";
import { findChild, findAllDescendants, serializeXml } from "./docx/dom.js";
import { split_structural_appendix, paginate } from "./pagination.js";
import {
  is_heading_paragraph,
  is_native_heading,
  get_run_style_markers,
  get_run_text,
  apply_formatting_to_segments,
} from "./utils/docx.js";
import { format_ambiguity_error } from "./markup.js";
import {
  PREVIEW_TEXT_CAP,
  REPORT_ECHO_CAP,
  truncate_middle,
} from "./utils/text.js";
import { RegexTimeoutError } from "./utils/safe-regex.js";

// Width of the surrounding-document window shown in redline previews.
const PREVIEW_CONTEXT_CHARS = 30;

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

function safeCloneEdit(val: any, seen: WeakMap<any, any> = new WeakMap()): any {
  if (val === null || typeof val !== "object") {
    return val;
  }
  if (seen.has(val)) {
    return seen.get(val);
  }
  if (val.nodeType !== undefined || typeof val.cloneNode === "function") {
    return val;
  }
  if (Array.isArray(val)) {
    const copy: any[] = [];
    seen.set(val, copy);
    for (let i = 0; i < val.length; i++) {
      copy.push(safeCloneEdit(val[i], seen));
    }
    return copy;
  }
  const copy: any = {};
  seen.set(val, copy);
  for (const key of Object.keys(val)) {
    copy[key] = safeCloneEdit(val[key], seen);
  }
  return copy;
}

function takeSnapshot(doc: any): any {
  const parts = [...doc.pkg.parts];
  const unzipped = { ...doc.pkg.unzipped };
  const rels = new Map<any, Map<string, any>>();
  const elements = new Map<any, Element>();
  for (const part of parts) {
    rels.set(part, new Map(part.rels));
    if (part._element) {
      elements.set(part, part._element.cloneNode(true) as Element);
    }
  }
  return { parts, unzipped, rels, elements };
}

function restoreSnapshot(doc: any, snapshot: any): void {
  doc.pkg.parts = [...snapshot.parts];
  for (const key of Object.keys(doc.pkg.unzipped)) {
    delete doc.pkg.unzipped[key];
  }
  for (const [key, val] of Object.entries(snapshot.unzipped)) {
    doc.pkg.unzipped[key] = val;
  }
  for (const part of snapshot.parts) {
    part.rels = new Map(snapshot.rels.get(part)!);
    const originalEl = snapshot.elements.get(part);
    if (originalEl && part._element) {
      const xmlDoc = part._element.ownerDocument;
      if (xmlDoc && xmlDoc.documentElement) {
        xmlDoc.replaceChild(originalEl, xmlDoc.documentElement);
      }
      part._element = originalEl;
    }
  }
}

function stripMatchingHeadingHashes(
  target: string,
  newText: string,
): [string, string] {
  if (!target || !newText) return [target, newText];
  const targetMatch = target.match(/^(#+)\s+/);
  const newMatch = newText.match(/^(#+)\s+/);
  if (targetMatch && newMatch && targetMatch[1] === newMatch[1]) {
    const hashes = targetMatch[1];
    const targetClean = target.substring(hashes.length).trimStart();
    const newClean = newText.substring(hashes.length).trimStart();
    return [targetClean, newClean];
  }
  return [target, newText];
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

// Appended to a validation error when earlier edits in the same batch have
// already applied: the failing target may simply be stale under the
// sequential batch contract. Wording mirrors the Python engine exactly.
function sequential_context_hint(applied_so_far: number): string {
  return (
    `\n  Note: ${applied_so_far} earlier edit(s) in this batch were already ` +
    "applied. Batches apply sequentially — each edit must target the document " +
    "text as it reads AFTER the preceding edits (e.g. target the replacement " +
    "text an earlier edit introduced, not the original wording)."
  );
}

// Report placeholder for edits blocked only by OTHER edits' validation
// failures under the transactional batch contract. Mirrors Python.
const TRANSACTIONAL_NOT_APPLIED_ERROR =
  "Not applied: the batch is transactional and other edits failed " +
  "validation (see their errors). Fix or remove those edits and re-run.";

// Characters XML 1.0 cannot represent: C0 controls except tab/newline/CR.
// Word refuses to open a package carrying them, and @xmldom serializes them
// silently, so they must be rejected before they reach the DOM
// (QA 2026-07-17 F11; mirrors Python's clean per-edit error).
const XML_ILLEGAL_CHARS_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;

export function describe_illegal_control_chars(text: string): string | null {
  if (!text) return null;
  const found = text.match(XML_ILLEGAL_CHARS_RE);
  if (!found) return null;
  const codes = Array.from(new Set(found.map((c) => `0x${c.charCodeAt(0).toString(16).padStart(2, "0")}`))).sort();
  return codes.join(", ");
}

export function validate_edit_strings(
  edits: any[],
  index_offset: number = 0,
): string[] {
  const errors: string[] = [];

  for (let i = 0; i < edits.length; i++) {
    const edit = edits[i];
    const t_text = edit.target_text || "";
    const n_text = edit.new_text || "";

    // VAL-CRIT-8: XML-illegal control characters (QA 2026-07-17 F11).
    const checked_fields: Array<[string, string]> = [
      ["target_text", t_text],
      ["new_text", n_text],
    ];
    if (edit.comment) checked_fields.push(["comment", edit.comment]);
    (edit.cells || []).forEach((cell: string, cell_idx: number) => {
      checked_fields.push([`cells[${cell_idx}]`, cell || ""]);
    });
    for (const [field_name, field_value] of checked_fields) {
      const described = describe_illegal_control_chars(field_value);
      if (described) {
        errors.push(
          `- Edit ${i + 1 + index_offset} Failed: \`${field_name}\` contains control character(s) ` +
            `(${described}) that cannot be stored in a DOCX. Remove them and re-submit.`,
        );
      }
    }

    if (
      n_text.includes("{++") ||
      n_text.includes("{--") ||
      n_text.includes("{>>") ||
      n_text.includes("{==")
    ) {
      errors.push(
        `- Edit ${i + 1 + index_offset} Failed: Do not manually write CriticMarkup tags ({++, {--, {>>, {==) in \`new_text\`. The engine handles redlining automatically. To add a comment, use the \`comment\` parameter.`,
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
            `- Edit ${i + 1 + index_offset} Failed: Cannot insert footnote/endnote markers via text replace. Markers like \`[^fn-N]\` are read-only projections. Use Word's References menu.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Cannot delete footnote/endnote references via text replace. The marker corresponds to a structural XML element.`,
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
            `- Edit ${i + 1 + index_offset} Failed: Cannot insert hyperlinks via text replace. Use a dedicated structural operation.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Cannot delete hyperlinks via text replace. The marker corresponds to a structural XML element.`,
          );
        }
      } else if (
        t_links.length > 1 &&
        JSON.stringify(t_links) !== JSON.stringify(n_links)
      ) {
        errors.push(
          `- Edit ${i + 1 + index_offset} Failed: Can only edit or retarget one hyperlink per text replacement. Please split into multiple edits.`,
        );
      }
    }

    if (t_text.includes("[~") || n_text.includes("[~")) {
      const t_xrefs = t_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || [];
      const n_xrefs = n_text.match(/\[~[^~]+~\]\(#[^\)]+\)/g) || [];
      if (t_xrefs.length !== n_xrefs.length) {
        if (n_xrefs.length > t_xrefs.length) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Cannot insert cross-references via text replace. Markers are read-only projections.`,
          );
        } else {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Cannot delete cross-references via text replace. The marker corresponds to a structural XML element.`,
          );
        }
      } else {
        // Advanced XREF validation simplified for port scope
        if (JSON.stringify(t_xrefs) !== JSON.stringify(n_xrefs)) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Modifying or retargeting cross-reference markers is disallowed to prevent dependency corruption.`,
          );
        }
      }
    }

    // QA 2026-07-18 M5: image markers are read-only projections of
    // w:drawing elements. They cannot be fabricated, duplicated or removed
    // through text replacement.
    if (t_text.includes("docx-image:") || n_text.includes("docx-image:")) {
      const t_imgs = (t_text.match(/!\[[^\]]*\]\(docx-image:[^)]*\)/g) || []).sort();
      const n_imgs = (n_text.match(/!\[[^\]]*\]\(docx-image:[^)]*\)/g) || []).sort();
      if (JSON.stringify(t_imgs) !== JSON.stringify(n_imgs)) {
        errors.push(
          `- Edit ${i + 1 + index_offset} Failed: image markers (![alt](docx-image:N)) are read-only ` +
            "projections of embedded images. They cannot be inserted, altered, or removed " +
            "via text replacement — edit the text around the image instead.",
        );
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
            `- Edit ${i + 1 + index_offset} Failed: Cannot modify or insert internal anchor markers (\`{#...}\`). These represent structural XML bookmarks.`,
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
              `- Edit ${i + 1 + index_offset} Failed: Heading level ${level} is not supported (maximum is 6).`,
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
      n_text.includes("# Document Structure (Read-Only)") ||
      t_text.includes("Document Structure (Read-Only)") ||
      n_text.includes("Document Structure (Read-Only)")
    ) {
      errors.push(
        `- Edit ${i + 1 + index_offset} Failed: Modification targets the read-only boundary (Structural Appendix). This section cannot be edited.`,
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
  public original_mapper: DocumentMapper | null = null;
  public skipped_details: string[] = [];

  constructor(doc: DocumentObject, author: string = "Adeu AI (TS)") {
    this.doc = doc;
    this.author = author;
    this.timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

    const w16du_ns =
      "http://schemas.microsoft.com/office/word/2023/wordml/word16du";
    for (const part of this.doc.pkg.parts) {
      if (part === this.doc.part) {
        if (!part._element.hasAttribute("xmlns:w16du")) {
          part._element.setAttribute("xmlns:w16du", w16du_ns);
        }
      }
    }

    this.current_id = this._scan_existing_ids();
    this.mapper = new DocumentMapper(this.doc);
    this.comments_manager = new CommentsManager(this.doc);
  }

  /**
   * Return a hint when a short, single-token anchor contains punctuation that
   * can split awkwardly, else null.
   *
   * Surface this ONLY for edits that actually failed to match/apply. On a
   * successful edit the batch report already carries the redline preview, so
   * emitting this would be a false positive: the punctuation (dates,
   * `[_name_]` placeholders, `____` blanks) is frequently the literal target
   * and the edit succeeds despite it. Mirrors the Python engine.
   */
  private _check_punctuation_warning(target_text: string): string | null {
    if (!target_text) return null;
    if (target_text.length > 20 || target_text.includes(" ")) return null;
    if (target_text.includes("_") || target_text.includes("-")) {
      return `Warning: target_text '${target_text}' contains tokenization-splitting punctuation ('_' or '-'). This can trigger mid-word splits in the diff engine. Consider using a longer plain-prose anchor.`;
    }
    return null;
  }

  /**
   * Build a single (unfragmented) sub-edit for a commented change.
   *
   * Shared prefix/suffix are still trimmed (word-boundary aware) so the redline
   * stays minimal at the edges, but the changed middle is emitted as ONE tracked
   * change rather than fanned out per word. The comment then anchors around the
   * whole span. See _word_diff_sub_edits for why a commented change must not be
   * split.
   */
  private _single_commented_sub_edit(
    target_str: string,
    new_str: string,
    base_offset: number,
    comment: string,
    is_table: boolean,
    active_mapper: any,
  ): any[] {
    let final_target: string;
    let final_new: string;
    let start: number;
    let op: string;

    if (target_str === new_str) {
      // A pure comment anchor (no textual change) has nothing to trim to;
      // trimming identical strings would collapse the span to zero length and
      // the COMMENT_ONLY apply path would find no runs to attach to. Keep the
      // whole span as the anchor.
      final_target = target_str;
      final_new = new_str;
      start = base_offset;
      op = "COMMENT_ONLY";
    } else {
      const [prefix_len, suffix_len] = trim_common_context(target_str, new_str);
      final_target = target_str.slice(prefix_len, target_str.length - suffix_len);
      final_new = new_str.slice(prefix_len, new_str.length - suffix_len);
      start = base_offset + prefix_len;
      if (!final_target && final_new) {
        op = "INSERTION";
      } else if (final_target && !final_new) {
        op = "DELETION";
      } else {
        op = "MODIFICATION";
      }
    }

    const sub_edit: any = {
      type: "modify",
      target_text: final_target,
      new_text: final_new,
      comment,
    };
    sub_edit._resolved_start_idx = start;
    sub_edit._match_start_index = start;
    sub_edit._active_mapper_ref = active_mapper;
    sub_edit._internal_op = op;
    if (is_table) {
      sub_edit._is_table_edit = true;
    }
    return [sub_edit];
  }

  private _word_diff_sub_edits(
    target_str: string,
    new_str: string,
    base_offset: number,
    parent_comment: string | null = null,
    is_table: boolean = false,
    active_mapper: any = null,
  ): any[] {
    // A modify that carries a comment must stay ONE contiguous tracked change
    // so its comment anchor wraps the whole logical edit. Word-level fan-out
    // would split it into several Chg pairs and attach the comment to only one
    // fragment; rejecting THAT fragment then silently destroys the comment (and
    // any reply thread) while the other fragments — and the batch's "1 applied"
    // report — give no hint the annotation is gone (QA 2026-07-22 bug #1). Emit
    // a single sub-edit over the minimal word-boundary-trimmed changed span so a
    // commented change is atomic: rejecting it reverts the entire edit, with no
    // orphaned "other half".
    if (parent_comment !== null && parent_comment !== undefined) {
      return this._single_commented_sub_edit(
        target_str,
        new_str,
        base_offset,
        parent_comment,
        is_table,
        active_mapper,
      );
    }

    let raw_sub_edits: any[] = [];
    try {
      raw_sub_edits = generate_edits_from_text(target_str, new_str);
    } catch (e) {
      console.error("generate_edits_from_text failed, falling back to wholesale edit", e);
      raw_sub_edits = [];
    }

    // Hunks made purely of style markers are projection artifacts, never
    // user intent: they arise when a PLAIN target fuzzy-matched styled
    // document text ("Net 90 Days" against "**Net 90 Days**"), and the
    // resulting `**`-deletion sub-edits target virtual spans that can never
    // apply — phantom skips while the formatting silently stays (QA
    // 2026-07-19 F-02 sibling). Edits that DO declare markers never reach
    // this word-diff path (they resolve as whole-span markdown proxies).
    const _marker_only = (text: string): boolean => {
      const stripped = text.trim();
      return stripped.length > 0 && /^[*_]+$/.test(stripped);
    };
    raw_sub_edits = raw_sub_edits.filter(
      (e: any) =>
        !(
          (!e.target_text || _marker_only(e.target_text)) &&
          (!e.new_text || _marker_only(e.new_text)) &&
          (e.target_text || e.new_text)
        ),
    );

    if (!raw_sub_edits || raw_sub_edits.length === 0) {
      const fallback_edit: any = {
        type: "modify",
        target_text: target_str,
        new_text: new_str,
        comment: parent_comment,
      };
      fallback_edit._resolved_start_idx = base_offset;
      fallback_edit._match_start_index = base_offset;
      fallback_edit._active_mapper_ref = active_mapper;
      if (is_table) {
        fallback_edit._is_table_edit = true;
      }
      if (target_str === new_str) {
        fallback_edit._internal_op = "COMMENT_ONLY";
      } else if (!target_str && new_str) {
        fallback_edit._internal_op = "INSERTION";
      } else if (target_str && !new_str) {
        fallback_edit._internal_op = "DELETION";
      } else if (target_str && new_str) {
        fallback_edit._internal_op = "MODIFICATION";
      } else {
        fallback_edit._internal_op = "COMMENT_ONLY";
      }
      return [fallback_edit];
    }

    const sub_edits: any[] = [];
    let comment_assigned = false;
    for (const raw_edit of raw_sub_edits) {
      const sub_start = base_offset + (raw_edit._match_start_index || 0);
      const should_attach_comment = (parent_comment !== null) && !comment_assigned;
      if (should_attach_comment) {
        comment_assigned = true;
      }

      const sub_edit: any = {
        type: "modify",
        target_text: raw_edit.target_text,
        new_text: raw_edit.new_text,
        comment: should_attach_comment ? parent_comment : null,
      };
      sub_edit._resolved_start_idx = sub_start;
      sub_edit._match_start_index = sub_start;
      sub_edit._active_mapper_ref = active_mapper;
      if (is_table) {
        sub_edit._is_table_edit = true;
      }

      const t_val = raw_edit.target_text;
      const n_val = raw_edit.new_text;
      if (!t_val && n_val) {
        sub_edit._internal_op = "INSERTION";
      } else if (t_val && !n_val) {
        sub_edit._internal_op = "DELETION";
      } else if (t_val && n_val) {
        sub_edit._internal_op = "MODIFICATION";
      } else {
        sub_edit._internal_op = "COMMENT_ONLY";
      }

      sub_edits.push(sub_edit);
    }

    return sub_edits;
  }
  /**
   * Best-effort "did you mean" hint for a failed target. The common loop trap
   * (observed in the field) is an anchored regex like `^\( x \)$` against a
   * mid-document string: ^/$ bind to the whole full_text, so it never matches
   * even though the literal `( x )` is present. We strip regex anchoring/escapes
   * and probe full_text for a literal occurrence; if found, we tell the model
   * the exact literal that WOULD match so it drops the anchors instead of
   * escalating the regex further.
   */
  private _nearest_match_hint(
    target_text: string | undefined,
    is_regex: boolean,
  ): string {
    if (!target_text) return "";
    let probe = target_text;
    if (is_regex) {
      // Strip leading/trailing anchors and surrounding \s* the model tends to add.
      probe = probe.replace(/^\^/, "").replace(/\$$/, "");
      probe = probe.replace(/^\\s\*/, "").replace(/\\s\*$/, "");
      // Unescape the common literal escapes so "\( x \)" -> "( x )".
      probe = probe.replace(/\\([.^$*+?()[\]{}|\\/])/g, "$1");
    }
    probe = probe.trim();
    if (!probe || probe === target_text) {
      // No anchors to strip, or nothing changed: nothing useful to suggest.
      if (!is_regex) return "";
    }
    const idx = this.mapper.full_text.indexOf(probe);
    if (idx !== -1) {
      const ctx_start = Math.max(0, idx - 15);
      const ctx_end = Math.min(
        this.mapper.full_text.length,
        idx + probe.length + 15,
      );
      const ctx = this.mapper.full_text
        .substring(ctx_start, ctx_end)
        .replace(/\n/g, " ");
      return (
        `\n  Did you mean the literal "${probe}"? It appears in the document` +
        ` (…${ctx}…). If you used a regex, drop the ^/$ anchors — they match` +
        ` the start/end of the entire document, not a line.`
      );
    }
    return "";
  }
  // CriticMarkup wrapper pairs used when tidying preview context windows.
  private static readonly _PREVIEW_WRAPPER_PAIRS: [string, string][] = [
    ["{--", "--}"],
    ["{++", "++}"],
    ["{==", "==}"],
    ["{>>", "<<}"],
  ];

  /**
   * Makes a fixed-width slice of the raw-view projection presentable: drops
   * complete {>>...<<} meta blocks (annotations of pre-existing changes, not
   * part of this edit) and any wrapper fragments the window boundary chopped
   * in half. Without this, previews leak internal scaffolding like
   * "[Chg:5 delete]" (QA H1). Mirrors the Python engine.
   */
  private static _tidy_preview_context(
    snippet: string,
    side: "before" | "after",
  ): string {
    snippet = snippet.replace(/\{>>[\s\S]*?<<\}/g, "");

    for (const [open_tok, close_tok] of RedlineEngine._PREVIEW_WRAPPER_PAIRS) {
      if (side === "before") {
        // Cut through the last closer whose opener lies left of the window.
        let depth = 0;
        let cut = 0;
        let i = 0;
        while (i < snippet.length) {
          if (snippet.startsWith(open_tok, i)) {
            depth += 1;
            i += open_tok.length;
          } else if (snippet.startsWith(close_tok, i)) {
            if (depth === 0) cut = i + close_tok.length;
            else depth -= 1;
            i += close_tok.length;
          } else {
            i += 1;
          }
        }
        snippet = snippet.substring(cut);
      } else {
        // Cut from the first opener whose closer lies right of the window.
        const opens: number[] = [];
        let i = 0;
        while (i < snippet.length) {
          if (snippet.startsWith(open_tok, i)) {
            opens.push(i);
            i += open_tok.length;
          } else if (snippet.startsWith(close_tok, i)) {
            if (opens.length > 0) opens.pop();
            i += close_tok.length;
          } else {
            i += 1;
          }
        }
        if (opens.length > 0) snippet = snippet.substring(0, opens[0]);
      }
    }

    // 1-2 char remnants of a 3-char wrapper token chopped by the window edge.
    if (side === "before") {
      snippet = snippet.replace(/^[-+=<>]{0,2}\}/, "");
    } else {
      snippet = snippet.replace(/\{[-+=<>]{0,2}$/, "");
    }
    return snippet;
  }

  /**
   * Snapshots the document text around a resolved edit BEFORE anything is
   * applied. Previews rendered after the batch mutates the DOM cannot slice
   * full_text at the stored offsets: applied edits shift offsets and inject
   * tracked-change markup, garbling previews with unrelated edits and
   * internal scaffolding (QA H1).
   */
  private _capture_preview_context(edit: any): void {
    if (edit.type !== "modify") return;
    const start_idx = edit._resolved_start_idx;
    if (start_idx === undefined || start_idx === null) return;
    const active_mapper = edit._active_mapper_ref || this.mapper;
    const full_text = active_mapper.full_text;
    if (!full_text) return;
    const length = (edit.target_text || "").length;
    const before = full_text.substring(
      Math.max(0, start_idx - PREVIEW_CONTEXT_CHARS),
      start_idx,
    );
    const after = full_text.substring(
      start_idx + length,
      start_idx + length + PREVIEW_CONTEXT_CHARS,
    );
    edit._preview_context = [
      RedlineEngine._tidy_preview_context(before, "before"),
      RedlineEngine._tidy_preview_context(after, "after"),
    ];
  }

  /**
   * Like _capture_preview_context, but snapshots the context around the
   * ORIGINAL edit's full matched span (stashed by _pre_resolve_heuristic_edit),
   * so the report preview can present the complete logical change of a
   * compound modification instead of its first sub-edit.
   */
  private _capture_parent_preview_context(parent: any): void {
    if (!parent || parent.type !== "modify") return;
    if (parent._preview_context || !parent._preview_span) return;
    const [start_idx, match_len] = parent._preview_span;
    const active_mapper = parent._preview_mapper_ref || this.mapper;
    const full_text = active_mapper.full_text;
    if (!full_text) return;
    const before = full_text.substring(
      Math.max(0, start_idx - PREVIEW_CONTEXT_CHARS),
      start_idx,
    );
    const after = full_text.substring(
      start_idx + match_len,
      start_idx + match_len + PREVIEW_CONTEXT_CHARS,
    );
    parent._preview_context = [
      RedlineEngine._tidy_preview_context(before, "before"),
      RedlineEngine._tidy_preview_context(after, "after"),
    ];
  }

  /**
   * Renders the preview from the edit's full matched span. The common
   * prefix/suffix between matched and replacement text is moved into the
   * surrounding context so the {--...--}{++...++} block shows the minimal
   * complete change.
   */
  private _build_full_match_preview(edit: any): [string | null, string | null] {
    let [context_before, context_after] = edit._preview_context as [
      string,
      string,
    ];
    let matched: string = edit._preview_matched_text || "";
    let new_text: string =
      edit._preview_new_text !== undefined && edit._preview_new_text !== null
        ? edit._preview_new_text
        : edit.new_text || "";

    // Heading markdown prefixes are projection artifacts, not literal
    // document text — keep them out of the {--...--}/{++...++} body.
    const [matched_clean, matched_style] = this._parse_markdown_style(matched);
    const [new_clean, new_style] = this._parse_markdown_style(new_text);
    if (matched_style && matched_style.startsWith("Heading")) {
      context_before = context_before + matched.substring(0, matched.length - matched_clean.length);
      matched = matched_clean;
    }
    if (new_style && new_style.startsWith("Heading")) {
      new_text = new_clean;
    }

    const [prefix_len, suffix_len] = trim_common_context(matched, new_text);
    let display_target = matched.substring(
      prefix_len,
      matched.length - suffix_len,
    );
    let display_new = new_text.substring(
      prefix_len,
      new_text.length - suffix_len,
    );
    context_before = context_before + matched.substring(0, prefix_len);
    if (suffix_len) {
      context_after = matched.substring(matched.length - suffix_len) + context_after;
    }

    display_target = truncate_middle(display_target, PREVIEW_TEXT_CAP);
    display_new = truncate_middle(display_new, PREVIEW_TEXT_CAP);
    let critic_markup: string;
    if (!display_target && !display_new) {
      // Comment-only edit (text unchanged): highlight the anchor instead of
      // rendering an empty change.
      const anchor = truncate_middle(matched, PREVIEW_TEXT_CAP);
      const body = anchor ? `{==${anchor}==}` : "";
      critic_markup = `${context_before.substring(0, context_before.length - matched.length)}${body}${context_after}`;
    } else {
      const deletion = display_target ? `{--${display_target}--}` : "";
      const insertion = display_new ? `{++${display_new}++}` : "";
      critic_markup = `${context_before}${deletion}${insertion}${context_after}`;
    }

    let clean_text = critic_markup;
    clean_text = clean_text.replace(/\{>>[\s\S]*?<<\}/g, "");
    clean_text = clean_text.replace(/\{--[\s\S]*?--\}/g, "");
    clean_text = clean_text.replace(/\{\+\+([\s\S]*?)\+\+\}/g, "$1");
    return [critic_markup, clean_text];
  }

  /**
   * The "new text" a batch report should show for an edit. InsertTableRow has
   * no new_text field — surface its cell contents rather than a misleading
   * empty string (QA M4).
   */
  private static _report_new_text(edit: any): string {
    if (edit && edit.type === "insert_row" && Array.isArray(edit.cells)) {
      return edit.cells.join(" | ");
    }
    return (edit && edit.new_text) || "";
  }

  private _build_edit_context_previews(
    edit: any,
  ): [string | null, string | null] {
    if (edit.type !== "modify") return [null, null];
    if (edit._preview_span && edit._preview_context) {
      return this._build_full_match_preview(edit);
    }
    if (edit._resolved_proxy_edit) {
      edit = edit._resolved_proxy_edit;
    }
    let start_idx = edit._resolved_start_idx;
    if (start_idx === undefined || start_idx === null) return [null, null];
    let target_text = edit.target_text || "";
    let new_text = edit.new_text || "";

    const [clean_target, target_style] =
      this._parse_markdown_style(target_text);
    if (target_style && target_style.startsWith("Heading")) {
      const prefix_len = target_text.length - clean_target.length;
      start_idx += prefix_len;
      target_text = clean_target;
    }

    const [clean_new, new_style] = this._parse_markdown_style(new_text);
    if (new_style && new_style.startsWith("Heading")) {
      new_text = clean_new;
    }

    const length = target_text.length;
    let context_before: string;
    let context_after: string;
    if (edit._preview_context) {
      [context_before, context_after] = edit._preview_context;
    } else {
      // Fallback for callers that never went through apply_edits. Only safe
      // while the mapper still reflects the pre-apply document.
      const active_mapper = edit._active_mapper_ref || this.mapper;
      const full_text = active_mapper.full_text;
      if (!full_text) return [null, null];
      context_before = RedlineEngine._tidy_preview_context(
        full_text.substring(
          Math.max(0, start_idx - PREVIEW_CONTEXT_CHARS),
          start_idx,
        ),
        "before",
      );
      context_after = RedlineEngine._tidy_preview_context(
        full_text.substring(
          start_idx + length,
          start_idx + length + PREVIEW_CONTEXT_CHARS,
        ),
        "after",
      );
    }

    // Bound the echoed edit values: previews flow into LLM context windows
    // and must not multiply an oversized new_text/target_text (QA C2).
    const display_target = truncate_middle(target_text, PREVIEW_TEXT_CAP);
    const display_new = truncate_middle(new_text, PREVIEW_TEXT_CAP);
    const insertion = display_new ? `{++${display_new}++}` : "";
    const critic_markup = `${context_before}{--${display_target}--}${insertion}${context_after}`;

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

  private _get_heading_path_and_page(
    start_idx: number,
    text: string,
    page_offsets: number[],
  ): [string, number] {
    let page = 1;
    for (let i = 0; i < page_offsets.length; i++) {
      if (start_idx >= page_offsets[i]) {
        page = i + 1;
      } else {
        break;
      }
    }

    const textBefore = text.substring(0, start_idx);
    const lines = textBefore.split("\n");
    const path: string[] = [];
    let current_level = 999;

    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      const m = line.match(/^(#{1,6})\s+(.*)/);
      if (m) {
        const level = m[1].length;
        if (level < current_level) {
          let cleanHeading = m[2]
            .replace(/\*\*|__|[*_]/g, "")
            .replace(/\{#[^}]+\}/g, "")
            .trim();
          if (cleanHeading.length > 80) {
            cleanHeading = cleanHeading.substring(0, 80) + "...";
          }
          path.unshift(cleanHeading);
          current_level = level;
          if (level === 1) break;
        }
      }
    }
    return [path.join(" > "), page];
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

    // Pre-count revisions before mutating. Unit is REVISION ELEMENTS, matching
    // the Python engine and sanitize's count_tracked_changes so no two surfaces
    // report different totals for one document.
    let accepted_insertions = 0;
    let accepted_deletions = 0;
    let accepted_formatting = 0;
    for (const root_element of parts_to_process) {
      accepted_insertions += findAllDescendants(root_element, "w:ins").length;
      accepted_deletions += findAllDescendants(root_element, "w:del").length;
      for (const tag of ["w:rPrChange", "w:pPrChange", "w:sectPrChange"]) {
        accepted_formatting += findAllDescendants(root_element, tag).length;
      }
    }

    // Counted as it happens below, not pre-read from the comments part: this
    // method only deletes the bodies of OUR OWN comments wrapping a resolved
    // revision (foreign ones keep their body by design), so the document's
    // comment total would claim removals that never happened.
    let removed_comments = 0;

    for (const root_element of parts_to_process) {
      const insNodes = findAllDescendants(root_element, "w:ins");
      for (const ins of insNodes) {
        removed_comments += this._clean_wrapping_comments(ins);
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
              removed_comments += this._clean_wrapping_comments(p);
              removed_comments += this._delete_comments_in_element(p);
              if (p.parentNode) {
                p.parentNode.removeChild(p);
              }
            }
          }
        }
      }

      const delNodes = findAllDescendants(root_element, "w:del");
      for (const d of delNodes) {
        removed_comments += this._clean_wrapping_comments(d);
        removed_comments += this._delete_comments_in_element(d);
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

    // Final pass: completely eject all comments, anchors, and parts
    for (const root_element of parts_to_process) {
      for (const tag of ["w:commentRangeStart", "w:commentRangeEnd"]) {
        for (const el of findAllDescendants(root_element, tag)) {
          el.parentNode?.removeChild(el);
        }
      }

      const refs = findAllDescendants(root_element, "w:commentReference");
      for (const ref of refs) {
        const parent = ref.parentNode as Element | null;
        if (parent) {
          if (parent.tagName === "w:r" || parent.tagName.endsWith(":r")) {
            const nonRprChildren = Array.from(parent.childNodes).filter(
              (c) =>
                c.nodeType === 1 &&
                (c as Element).tagName !== "w:rPr" &&
                (c as Element).tagName !== "rPr",
            );
            if (nonRprChildren.length <= 1) {
              parent.parentNode?.removeChild(parent);
            } else {
              parent.removeChild(ref);
            }
          } else {
            parent.removeChild(ref);
          }
        }
      }
    }

    const pkg = this.doc.pkg;
    const comment_partnames = new Set<string>();
    for (const part of pkg.parts) {
      if (part.partname.toLowerCase().includes("comments")) {
        comment_partnames.add(part.partname);
        const withSlash = part.partname.startsWith("/")
          ? part.partname
          : "/" + part.partname;
        const withoutSlash = part.partname.startsWith("/")
          ? part.partname.substring(1)
          : part.partname;
        comment_partnames.add(withSlash);
        comment_partnames.add(withoutSlash);
      }
    }

    if (comment_partnames.size > 0) {
      // Sever relationships referencing comments
      for (const part of pkg.parts) {
        if (part.partname.endsWith(".rels")) {
          const rels = findAllDescendants(part._element, "Relationship");
          const toRemove: Element[] = [];
          for (const rel of rels) {
            const target = rel.getAttribute("Target") || "";
            if (target.toLowerCase().includes("comments")) {
              toRemove.push(rel);

              const sourcePath = part.partname
                .replace("/_rels/", "/")
                .replace(".rels", "");
              const sourcePart = pkg.getPartByPath(sourcePath);
              if (sourcePart) {
                const relId = rel.getAttribute("Id");
                if (relId) sourcePart.rels.delete(relId);
              }
            }
          }
          for (const relEl of toRemove) {
            relEl.parentNode?.removeChild(relEl);
          }
        }
      }

      // Remove overrides from [Content_Types].xml
      const ctPart = pkg.getPartByPath("[Content_Types].xml");
      if (ctPart) {
        const overrides = findAllDescendants(ctPart._element, "Override");
        const toRemove: Element[] = [];
        for (const override of overrides) {
          const partName = override.getAttribute("PartName") || "";
          if (
            comment_partnames.has(partName) ||
            partName.toLowerCase().includes("comments")
          ) {
            toRemove.push(override);
          }
        }
        for (const overrideEl of toRemove) {
          overrideEl.parentNode?.removeChild(overrideEl);
        }
      }

      // Remove comment parts from pkg.parts
      pkg.parts = pkg.parts.filter(
        (p) => !p.partname.toLowerCase().includes("comments"),
      );

      // Remove comment files from pkg.unzipped
      for (const key of Object.keys(pkg.unzipped)) {
        if (key.toLowerCase().includes("comments")) {
          delete pkg.unzipped[key];
        }
      }
    }

    return {
      accepted_insertions,
      accepted_deletions,
      accepted_formatting,
      removed_comments,
    };
  }

  /**
   * Revert every tracked change, returning the document to the state it had
   * before any revision was proposed. The exact inverse of
   * accept_all_revisions:
   *
   *   - <w:ins>  -> removed together with all of its content (the proposed
   *                 insertion never existed); an inserted row (<w:ins> in
   *                 <w:trPr>) drops the whole row.
   *   - <w:del>  -> unwrapped, restoring the original text (<w:delText> becomes
   *                 <w:t> again); a row-deletion mark in <w:trPr> is removed so
   *                 the row survives.
   *   - paragraph-mark <w:del> in pPr/rPr -> removed, undoing a proposed merge.
   *
   * Comments are annotations, not revisions, so standalone comments are left in
   * place; only anchors stranded inside a rejected insertion are cleaned up.
   *
   * Insertions are reverted before deletions are restored so a deletion nested
   * inside a foreign author's insertion is removed wholesale with the insertion
   * — the contingent text disappears rather than being promoted to committed
   * body text.
   *
   * Known limitation: tracked paragraph STRUCTURE changes (a split recorded as a
   * pilcrow <w:ins>, or a merge recorded as a pilcrow <w:del>) are reverted only
   * to the extent of dropping/keeping the mark; the original paragraph boundary
   * is not reconstructed, because the merge protocol coalesces paragraphs
   * destructively at edit time. Reverting run-level insertions/deletions (the
   * common case) is exact. This limitation is shared with the Python engine.
   */
  public reject_all_revisions() {
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
      // 1. Reject insertions: drop the <w:ins> and everything inside it.
      //    Document order means an outer <w:ins> is handled before a nested
      //    one; removing the outer detaches the inner (guarded below).
      const insNodes = findAllDescendants(root_element, "w:ins");
      for (const ins of insNodes) {
        const parent = ins.parentNode as Element | null;
        if (!parent) continue;
        this._clean_wrapping_comments(ins);
        this._delete_comments_in_element(ins);
        if (parent.tagName === "w:trPr") {
          const row = parent.parentNode as Element | null;
          if (row && row.parentNode) {
            row.parentNode.removeChild(row);
          }
        } else {
          parent.removeChild(ins);
        }
      }

      // 2. Reject paragraph-mark deletions: keep the paragraph break.
      const pNodes = findAllDescendants(root_element, "w:p");
      for (const p of pNodes) {
        const pPr = findChild(p, "w:pPr");
        if (pPr) {
          const rPr = findChild(pPr, "w:rPr");
          const delMark = rPr ? findChild(rPr, "w:del") : null;
          if (rPr && delMark) {
            rPr.removeChild(delMark);
          }
        }
      }

      // 3. Reject deletions: restore the original text.
      const delNodes = findAllDescendants(root_element, "w:del");
      for (const d of delNodes) {
        const parent = d.parentNode as Element | null;
        if (!parent) continue;
        this._clean_wrapping_comments(d);
        if (parent.tagName === "w:trPr") {
          parent.removeChild(d);
          continue;
        }
        const delTexts = Array.from(d.getElementsByTagName("w:delText"));
        for (const dt of delTexts) {
          const t = d.ownerDocument!.createElement("w:t");
          t.textContent = dt.textContent;
          if (dt.hasAttribute("xml:space"))
            t.setAttribute("xml:space", "preserve");
          dt.parentNode?.replaceChild(t, dt);
        }
        while (d.firstChild) {
          parent.insertBefore(d.firstChild, d);
        }
        parent.removeChild(d);
      }
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
   * Walks `element` to its XML root element. Word (and LibreOffice, which
   * refuses to LOAD such files) only supports comment ranges in the main
   * document story ("w:document") — never in headers, footers, footnotes or
   * endnotes (QA 2026-07-18 H4/C1).
   */
  private _comment_anchor_in_main_story(element: Element): boolean {
    let root: Element = element;
    while (root.parentNode && root.parentNode.nodeType === 1) {
      root = root.parentNode as Element;
    }
    return root.tagName === "w:document";
  }

  /**
   * When the anchor lives outside the main document story, records a
   * user-visible warning and returns true (caller must skip the comment).
   * The tracked change itself still applies — only the bubble is dropped.
   */
  private _skip_comment_outside_main_story(
    element: Element,
    text: string,
  ): boolean {
    if (this._comment_anchor_in_main_story(element)) return false;
    let root: Element = element;
    while (root.parentNode && root.parentNode.nodeType === 1) {
      root = root.parentNode as Element;
    }
    const story =
      (
        {
          "w:ftr": "footer",
          "w:hdr": "header",
          "w:footnotes": "footnote",
          "w:endnotes": "endnote",
        } as Record<string, string>
      )[root.tagName] || "non-body";
    const msg =
      `- Warning: the comment "${text.substring(0, 60)}" was NOT attached: Word does not support ` +
      `comments inside a ${story} part, and writing one produces a document other ` +
      "applications cannot open. The tracked change itself was applied.";
    this.skipped_details.push(msg);
    console.error(
      `Comment anchor outside main story; comment dropped (story=${story})`,
    );
    return true;
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
    if (!parent_element || !start_element || !end_element) return;
    if (this._skip_comment_outside_main_story(parent_element, text)) return;

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
    if (!start_p || !end_p) return;
    if (
      this._skip_comment_outside_main_story(start_p, text) ||
      this._skip_comment_outside_main_story(end_p, text)
    )
      return;

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
  private _clone_pPr_scrubbing_headings(existing_pPr: Element): Element {
    const pPr_clone = existing_pPr.cloneNode(true) as Element;
    const pStyle_el = findChild(pPr_clone, "w:pStyle");
    if (pStyle_el) {
      const style_val = pStyle_el.getAttribute("w:val");
      if (style_val) {
        const is_heading =
          style_val.startsWith("Heading") ||
          style_val === "Title" ||
          style_val.replace(/\s+/g, "").startsWith("Heading");
        if (is_heading) {
          pPr_clone.removeChild(pStyle_el);
        }
      }
    }
    const outlineLvl_el = findChild(pPr_clone, "w:outlineLvl");
    if (outlineLvl_el) {
      pPr_clone.removeChild(outlineLvl_el);
    }
    return pPr_clone;
  }

  private _track_insert_multiline(
    text: string,
    anchor_run: Run | null,
    anchor_paragraph: Paragraph | null,
    reuse_id: string,
    // The attached DOM element the insertion physically follows. anchor_run
    // supplies STYLING and may already be detached (the deletion step clones
    // runs into <w:del> and replaces the originals); suffix relocation for
    // paragraph-splitting insertions keys on this element instead.
    positional_anchor_el: Element | null = null,
    // When the edit declares explicit emphasis markers, the markers are
    // authoritative: strip inherited bold/italic from the anchor style
    // (QA 2026-07-19 F-02).
    suppress_emphasis: boolean = false,
    // True when the caller will attach the insertion BEFORE the anchor
    // (paragraph-start insertions): the anchor itself then belongs to the
    // relocating suffix (hunt-profile counterexample, 2026-07-19 —
    // "00." + insert "0.\n\n0 " must read "0.\n\n0 00.", never
    // "0.00.\n\n0 "). Mirrors the Python engine.
    insert_before: boolean = false,
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

    // Suffix nodes: content that follows the anchor inside current_p. When
    // the inserted text carries paragraph breaks, this content belongs in
    // the LAST new paragraph. The positional anchor is attached to the
    // DOM, and the insertion lands immediately after it, so its following
    // child-of-paragraph siblings are exactly the suffix.
    const suffix_nodes: Element[] = [];
    const relocatable = new Set(["w:r", "w:ins", "w:del"]);
    // insert_before with a RUN anchor: the insertion precedes the anchor, so
    // the anchor run itself is part of the suffix. (The explicit
    // positional_anchor_el is only passed by flows that insert AFTER it.)
    const pos_from_positional =
      positional_anchor_el && positional_anchor_el.parentNode
        ? positional_anchor_el
        : null;
    const pos_from_anchor_run =
      anchor_run !== null && anchor_run._element.parentNode
        ? anchor_run._element
        : null;
    const pos_source = pos_from_positional ?? pos_from_anchor_run;
    const suffix_includes_anchor =
      insert_before && pos_from_positional === null && pos_from_anchor_run !== null;
    if (current_p !== null && pos_source !== null) {
      let pos_anchor: Element | null = pos_source;
      while (pos_anchor && pos_anchor.parentNode !== current_p) {
        pos_anchor = pos_anchor.parentNode as Element | null;
        if (pos_anchor === current_p) {
          pos_anchor = null;
          break;
        }
      }
      if (pos_anchor) {
        let nxt: Node | null = suffix_includes_anchor
          ? pos_anchor
          : pos_anchor.nextSibling;
        while (nxt) {
          if (nxt.nodeType === 1 && relocatable.has((nxt as Element).tagName)) {
            suffix_nodes.push(nxt as Element);
          }
          nxt = nxt.nextSibling;
        }
      }
    } else if (current_p !== null && insert_before) {
      // No attached anchor run at all (paragraph-anchored insertion at
      // paragraph START): everything in the host paragraph follows the
      // insertion point, so it all relocates (mirrors the Python engine).
      let child = current_p.firstChild;
      while (child) {
        if (
          child.nodeType === 1 &&
          relocatable.has((child as Element).tagName)
        ) {
          suffix_nodes.push(child as Element);
        }
        child = child.nextSibling;
      }
    }

    // Drop the trailing empty line ONLY when there is no suffix to relocate.
    // "foo\n\nbar\n\n" splits to ['foo', '', 'bar', '']; without a suffix
    // the trailing empty is just a terminator, but with one it is the fresh
    // destination paragraph the suffix moves into.
    while (
      lines.length > 1 &&
      lines[lines.length - 1] === "" &&
      suffix_nodes.length === 0
    ) {
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
        suppress_emphasis,
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
          new_p.appendChild(this._clone_pPr_scrubbing_headings(existing_pPr));
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
        suppress_emphasis,
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

    // Relocate the suffix into the last new paragraph: the paragraph break
    // the insertion introduced splits current_p at the anchor, so everything
    // after the anchor continues in the final inserted paragraph.
    if (!block_mode && last_p && suffix_nodes.length > 0) {
      for (const node of suffix_nodes) {
        node.parentNode?.removeChild(node);
        last_p.appendChild(node);
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
    suppress_emphasis: boolean = false,
  ): Element | null {
    if (!line_text && line_text !== "") return null;
    const ins = this._create_track_change_tag("w:ins", "", reuse_id);
    const segments = this._parse_inline_markdown(line_text);
    if (segments.length === 0) {
      return null;
    }
    for (const [segText, segProps] of segments) {
      const r = xmlDoc.createElement("w:r");
      // Inherit run formatting from the anchor so partial replacements inside
      // a styled span keep the style (matching Word's type-into-selection
      // behavior and the Python engine — the old blanket strip made
      // "Important" -> "Critical" inside a bold span come out unstyled).
      if (anchor_run && anchor_run._element) {
        const anchor_rPr = findChild(anchor_run._element, "w:rPr");
        if (anchor_rPr) {
          const clone = anchor_rPr.cloneNode(true) as Element;
          // Always strip vanish / strike (invisible inserts) and italic
          // (BUG-23-2: an inserted replacement must not silently inherit the
          // surrounding italic styling). Bold is preserved — it usually
          // carries structural meaning (headings, defined terms) — UNLESS
          // the edit's own markers are authoritative (QA 2026-07-19 F-02):
          // `**X**` -> `_X_` must yield italic-only, `**X**` -> `X` plain.
          // Mirrors the Python engine's _track_insert_inline exactly.
          const strip_tags = ["w:vanish", "w:strike", "w:dstrike", "w:i", "w:iCs"];
          if (suppress_emphasis) {
            strip_tags.push("w:b", "w:bCs");
          }
          for (const tag of strip_tags) {
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

    // Numbered lists: the projection emits ordered items with a CONSTANT
    // "1. " marker (Markdown renumbers), so only that exact shape converts
    // back into a list style. Any other leading number ("2024. Year in
    // review", "3. Clause text") is literal document text. Continuation
    // items inside an existing list anchor keep full "\d+." handling via
    // the list-anchored insertion path.
    const match = stripped_text.match(/^1\.\s+/);
    if (match) {
      return [stripped_text.substring(match[0].length).trim(), "List Number"];
    }

    return [text, null];
  }

  /**
   * True when this edit's target or replacement text carries explicit
   * bold/italic markers, making the markers AUTHORITATIVE for the inserted
   * runs' formatting. Replacing `**X**` with `_X_` must yield italic-only
   * text, and replacing `**X**` with `X` must yield plain text — inheriting
   * the replaced span's run properties on top of (or instead of) the
   * requested markers silently produces the wrong document while the report
   * claims success (QA 2026-07-19 F-02). Plain-text edits (no markers on
   * either side) keep inheriting the context style so partial replacements
   * inside a styled span never lose formatting.
   */
  private _edit_declares_emphasis(edit: any): boolean {
    for (const text of [edit?.target_text, edit?.new_text]) {
      if (!text || (!text.includes("**") && !text.includes("_"))) continue;
      const segments = this._parse_inline_markdown(text);
      if (segments.some(([, props]: [string, any]) => props && Object.keys(props).length > 0)) {
        return true;
      }
    }
    return false;
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
  /**
   * Read a comment's author directly from the comments part. Used by
   * _clean_wrapping_comments to decide whether a wrapping comment belongs to
   * another author (and must be preserved when its anchored change is
   * accepted/rejected) or to us (safe to delete). Reads the part rather than
   * the mapper because the mapper's comments_map is not rebuilt between review
   * actions, so it can be stale mid-batch. Returns null if not found.
   */
  private _get_comment_author(c_id: string): string | null {
    const part = this.doc.pkg.parts.find(
      (p) =>
        p.contentType ===
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    );
    if (!part) return null;
    const comments = findAllDescendants(part._element, "w:comment");
    for (const c of comments) {
      if (c.getAttribute("w:id") === c_id) {
        return c.getAttribute("w:author");
      }
    }
    return null;
  }
  /** Returns how many comment BODIES were actually deleted (see below: only
   *  our own are; foreign ones keep their body and lose only the anchor). */
  private _clean_wrapping_comments(element: Element): number {
    let deleted = 0;
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
        // Author-aware preservation. A comment that merely WRAPS the change
        // being accepted/rejected is a separate annotation — often the
        // counterparty's note explaining the very edit we are resolving.
        // Deleting it silently destroys their provenance (and, e.g., fails a
        // "keep the counterparty's comment" review requirement). So: always
        // detach the range markers (leaving no orphaned anchor, which lets the
        // accept/reject proceed cleanly), but only delete the comment BODY when
        // it is ours. Foreign-authored comment bodies are kept in the comments
        // part. When the author can't be read we default to preserving, since
        // deletion is the irreversible, higher-cost mistake.
        const author = this._get_comment_author(c_id);
        const is_own = author !== null && author === this.author;
        if (is_own) {
          this.comments_manager.deleteComment(c_id);
          deleted++;
        }
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
    return deleted;
  }

  /** Returns how many comment bodies were deleted. */
  private _delete_comments_in_element(element: Element): number {
    let deleted = 0;
    const refs = findAllDescendants(element, "w:commentReference");
    for (const ref of refs) {
      const c_id = ref.getAttribute("w:id");
      if (c_id) {
        this.comments_manager.deleteComment(c_id);
        deleted++;
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
    return deleted;
  }

  public validate_edits(edits: any[], index_offset: number = 0): string[] {
    const errors: string[] = [];
    if (!this.mapper.full_text) this.mapper["_build_map"]();

    errors.push(...validate_edit_strings(edits, index_offset));

    for (let i = 0; i < edits.length; i++) {
      const edit = edits[i];
      if (typeof edit !== "object" || edit === null) {
        errors.push(
          `- Edit ${i + 1 + index_offset} Failed: Invalid change format. Expected a JSON object, but received a primitive ${typeof edit}. Do not pass raw strings.`,
        );
        continue;
      }
      if (!edit.target_text) continue;
      // Caller-pinned indexes (e.g. generate_edits_from_text output) resolve
      // by position, not content: ambiguity / not-found checks are meaningless
      // for them and false-positive whenever the target coincidentally matches
      // unrelated text (a comment timestamp, an earlier redline). The
      // string-shape checks above still apply.
      if (
        (edit._match_start_index !== undefined &&
          edit._match_start_index !== null) ||
        (edit._resolved_start_idx !== undefined &&
          edit._resolved_start_idx !== null)
      )
        continue;

      const is_regex = (edit as any).regex || false;
      const match_mode = (edit as any).match_mode || "strict";

      if (is_regex) {
        // An unparsable pattern must be diagnosed as a regex problem. Without
        // this check it falls through the matcher's silent guard and surfaces
        // as "target text not found", sending the user hunting for a typo in
        // the document instead of in the pattern (QA 2026-07-19 F-13).
        try {
          new RegExp(edit.target_text);
        } catch (regex_err: any) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: target_text is not a valid regular expression ` +
              `(${regex_err?.message ?? regex_err}). Fix the pattern, or set "regex": false to ` +
              "match the text literally.",
          );
          continue;
        }
      }

      // Matches covering ONLY virtual projection text (meta bubbles,
      // timestamps, style markers) are phantoms: they can neither be edited
      // nor legitimately ambiguate a real match — a target of "4" was
      // rejected as "appears 8 times" because comment-bubble timestamps
      // matched (QA 2026-07-19 ADEU-QA-002 C).
      let matches = this.mapper.drop_virtual_only_matches(
        this.mapper.find_all_match_indices(edit.target_text, is_regex),
      );
      let activeText = this.mapper.full_text;
      let target_mapper = this.mapper;

      if (matches.length === 0) {
        if (!this.clean_mapper)
          this.clean_mapper = new DocumentMapper(this.doc, true);
        matches = this.clean_mapper.drop_virtual_only_matches(
          this.clean_mapper.find_all_match_indices(edit.target_text, is_regex),
        );
        if (matches.length > 0) {
          activeText = this.clean_mapper.full_text;
          target_mapper = this.clean_mapper;
        }
      }

      // BUG-23-5: a copy of the target that lives entirely inside a tracked
      // deletion (<w:del>) is not a live, editable occurrence and must not
      // count toward ambiguity. Drop matches whose overlapping real text is
      // exclusively deleted. Only applies to the raw mapper (the clean mapper
      // already omits deleted text).
      if (activeText === this.mapper.full_text && matches.length > 0) {
        const liveMatches = matches.filter(([start, length]) => {
          const realSpans = this.mapper.spans.filter(
            (s) => s.run !== null && s.end > start && s.start < start + length,
          );
          // Virtual-only matches were already dropped above; here we only
          // skip matches buried entirely inside tracked deletions.
          if (realSpans.length === 0) return true;
          return realSpans.some((s) => !s.del_id);
        });
        matches = liveMatches;
      }

      let is_deleted_text = false;
      const deleted_authors = new Set<string>();

      if (matches.length === 0) {
        if (!this.original_mapper) {
          this.original_mapper = new DocumentMapper(this.doc, false, true);
        }
        const orig_matches = this.original_mapper.drop_virtual_only_matches(
          this.original_mapper.find_all_match_indices(
            edit.target_text,
            is_regex,
          ),
        );
        if (orig_matches.length > 0) {
          is_deleted_text = true;
          for (const [start, length] of orig_matches) {
            const spans = this.original_mapper.spans.filter(
              (s) => s.end > start && s.start < start + length,
            );
            for (const s of spans) {
              if (s.run !== null) {
                let parent = s.run._element as Node | null;
                while (parent) {
                  if (
                    parent.nodeType === 1 &&
                    (parent as Element).tagName === "w:del"
                  ) {
                    const auth = (parent as Element).getAttribute("w:author");
                    if (auth) {
                      deleted_authors.add(auth);
                    }
                    break;
                  }
                  parent = parent.parentNode;
                }
              }
            }
          }
        }
      }

      if (matches.length === 0) {
        if (is_deleted_text) {
          const author_phrase =
            deleted_authors.size > 0
              ? `by ${Array.from(deleted_authors).sort().join(", ")}`
              : "by an existing revision";
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Target text matches text inside a tracked deletion ${author_phrase}. Reject/accept that change first or target the active replacement text instead.`,
          );
        } else {
          const hint = this._nearest_match_hint(edit.target_text, is_regex);
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: Target text not found in document:\n  "${truncate_middle(edit.target_text, REPORT_ECHO_CAP)}"${hint}`,
          );
        }
      } else if (matches.length > 1 && match_mode === "strict") {
        if (edit.target_text.includes("|")) {
          matches = matches.slice(0, 1);
        } else {
          const positions: [number, number][] = matches.map(
            ([start, length]) => [start, start + length],
          );
          errors.push(
            format_ambiguity_error(
              i + 1 + index_offset,
              edit.target_text,
              activeText,
              positions,
            ),
          );
        }
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
        const final_new = (edit.new_text || "").substring(
          pfx,
          (edit.new_text || "").length - sfx,
        );

        // QA 2026-07-18 C1: the projection flattens headers, body, footers
        // and notes into one string, but a text edit whose matched span
        // covers real text from two different OPC parts cannot be applied
        // without putting content in the wrong part — including the
        // insertion shape, whose anchor point at the part gap is inherently
        // ambiguous. Refuse the RAW match range outright and ask for a
        // single-part anchor. (Single-part documents skip the scan.)
        const multi_part_doc =
          target_mapper.part_ranges.filter((r) => r[1] > r[0]).length > 1;
        const raw_span_parts = multi_part_doc
          ? Array.from(
              new Set(
                target_mapper.spans
                  .filter(
                    (s) =>
                      s.run !== null &&
                      s.end > m_start &&
                      s.start < m_start + m_len,
                  )
                  .map((s) => s.part_index),
              ),
            ).sort((a, b) => a - b)
          : [];
        if (raw_span_parts.length > 1) {
          const kinds = raw_span_parts
            .map((pi) => target_mapper.part_kind_of(pi) || "?")
            .join(" → ");
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: target_text spans a structural document-part ` +
              `boundary (${kinds}). Headers, body, footers and footnotes are separate ` +
              "Word parts — an edit cannot cross between them. Anchor the edit on text " +
              "within a single part (split it into one edit per part if both sides " +
              "must change).",
          );
        }

        // QA 2026-07-18 M5: image markers are read-only projections. Only
        // the CHANGED span matters — markers sitting untouched in the
        // shared context are fine.
        const eff_start = m_start + pfx;
        const eff_end = m_start + m_len - sfx;
        if (eff_end > eff_start) {
          const overlapping = target_mapper.spans.filter(
            (s) =>
              s.end > eff_start &&
              s.start < eff_end &&
              (s.run !== null || s.text.trim() !== ""),
          );
          if (overlapping.some((s) => (s as any).is_image_marker)) {
            errors.push(
              `- Edit ${i + 1 + index_offset} Failed: the target overlaps a read-only image marker ` +
                "(![alt](docx-image:N)). Images cannot be edited or removed via text " +
                "replacement — target the text around the image instead.",
            );
          }
        }

        // QA 2026-07-18 H4: comments can only be anchored in the main
        // document story. A comment-only edit (target == new) whose match
        // lives in a header/footer/footnote has no effect Word or
        // LibreOffice could render — refuse it clearly.
        if (edit.comment && (edit.new_text || "") === (edit.target_text || "")) {
          const kind_here = target_mapper.part_kind_at(m_start);
          if (kind_here !== null && kind_here !== "body") {
            errors.push(
              `- Edit ${i + 1 + index_offset} Failed: comments cannot be anchored inside a ${kind_here} ` +
                "part — Word only supports comments in the main document body. Comment on " +
                "the related body text instead.",
            );
          }
        }

        // QA 2026-07-18 C2: a replacement may not smuggle new pipe-delimited
        // row lines into a table cell. Rows are structural; adding one
        // requires the insert_row operation.
        if (
          RedlineEngine._introduces_table_row_text(
            target_mapper,
            m_start,
            m_len,
            final_target,
            final_new,
          )
        ) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: new_text introduces a pipe-delimited row line inside ` +
              "a table. Text replacement cannot create table rows — use the structured " +
              `'insert_row' operation instead (e.g. {"type": "insert_row", ` +
              `"target_text": "<anchor row text>", "cells": ["...", "..."]}).`,
          );
        }

        if (final_target.includes("\n\n")) {
          // A *balanced* multi-paragraph modification (target and replacement
          // carry the same number of paragraph breaks) is safe: it is split
          // into one sub-edit per paragraph segment and applied, leaving the
          // structural \n\n breaks untouched. Only reject when the paragraph
          // structure would actually change (a merge or split), which cannot be
          // expressed as a per-paragraph text replacement. See
          // _pre_resolve_heuristic_edit.
          const balanced =
            matched.split("\n\n").length ===
            (edit.new_text || "").split("\n\n").length;
          if (!balanced) {
            if (final_new.includes("\n\n")) {
              const parts = matched.split("\n\n");
              if (
                parts.length >= 2 &&
                parts[0].trim() !== "" &&
                parts[parts.length - 1].trim() !== ""
              ) {
                errors.push(
                  `- Edit ${i + 1 + index_offset} Failed: target_text spans a paragraph boundary with body text on both sides. The paragraph break is a structural element, not literal text, so it cannot be replaced as a single span without corrupting the document. Split this into one edit per paragraph.`,
                );
              }
            } else {
              const parts = final_target.split("\n\n");
              if (
                parts.length >= 2 &&
                parts[0].trim() !== "" &&
                parts[parts.length - 1].trim() !== ""
              ) {
                errors.push(
                  `- Edit ${i + 1 + index_offset} Failed: target_text spans a paragraph boundary with body text on both sides. The paragraph break is a structural element, not literal text, so it cannot be replaced as a single span without corrupting the document. Split this into one edit per paragraph.`,
                );
              }
            }
          }
        }
      }

      for (const [start, length] of matches) {
        // Filter spans from the SAME mapper the match indices came from
        // (target_mapper may be the clean mapper); using this.mapper.spans here
        // would read a different coordinate space and miss the foreign <w:ins>
        // overlap for clean-mapper-resolved targets — silently letting a
        // partial straddle through. (Python filters target_mapper.spans too.)
        const spans = target_mapper.spans.filter(
          (s) => s.end > start && s.start < start + length,
        );
        const insAuthors = new Set<string>();
        const commentAuthors = new Set<string>();
        // Does any real (run-backed) text in the target lie OUTSIDE a foreign
        // insertion? If so the target only partially overlaps the insertion and
        // replacing it as one span would straddle the <w:ins> boundary — that
        // case must still be refused.
        let hasNonForeignRealText = false;
        for (const s of spans) {
          if (s.run === null) continue;
          let isForeignIns = false;
          if (s.ins_id) {
            const insNodes = findAllDescendants(
              this.doc.element,
              "w:ins",
            ).filter((n) => n.getAttribute("w:id") === s.ins_id);
            if (insNodes.length > 0) {
              const auth = insNodes[0].getAttribute("w:author");
              if (auth && auth !== this.author) {
                insAuthors.add(auth);
                isForeignIns = true;
              }
            }
          }
          if (!isForeignIns) hasNonForeignRealText = true;
        }
        for (const s of spans) {
          if (s.comment_ids) {
            for (const cid of s.comment_ids) {
              const c_data = this.mapper.comments_map[cid];
              if (c_data && c_data.author && c_data.author !== this.author) {
                commentAuthors.add(c_data.author);
              }
            }
          }
        }
        if (insAuthors.size > 0) {
          // A single (strict/first) modification whose target lies ENTIRELY
          // inside foreign-authored insertion(s) is allowed: track_delete_run
          // splits the enclosing <w:ins> and nests the change, producing valid
          // tracked-change XML. Refuse the remaining cases — match_mode "all"
          // fan-outs and partial overlaps that straddle the insertion
          // boundary.
          const fullyWithinForeignIns = !hasNonForeignRealText;
          if (
            !(
              (match_mode === "strict" || match_mode === "first") &&
              fullyWithinForeignIns
            )
          ) {
            errors.push(
              `- Edit ${i + 1 + index_offset} Failed: Modification targets an active insertion from another author (${Array.from(insAuthors).join(", ")}). Accept that change first or scope your edit outside of it.`,
            );
            continue;
          }
        }
        // Foreign comment ranges do NOT block deliberate single-occurrence
        // edits: amending body text under a colleague's comment is a normal
        // review workflow, and the comment anchor survives the tracked change.
        // Only blind match_mode="all" fan-outs are refused, so a bulk
        // replacement cannot silently sweep through another author's
        // annotations (transactional rollback).
        if (commentAuthors.size > 0 && match_mode === "all") {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: match_mode="all" would sweep through a comment range from another author (${Array.from(commentAuthors).join(", ")}). Target the commented text deliberately with match_mode "strict" or "first", or scope your edit outside of it.`,
          );
        }
      }

      // Structural table edits: verify the anchor really is a table row, and
      // that insert_row does not provide more cells than the row has columns —
      // extra cells must never produce a structurally inconsistent row (QA M3).
      if (
        (edit.type === "insert_row" || edit.type === "delete_row") &&
        matches.length > 0
      ) {
        const [start, length] = matches[0];
        const n_cols = RedlineEngine._column_count_at(
          target_mapper,
          start,
          length,
        );
        if (n_cols === null) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: ${edit.type} target text was found, but it is not inside a table row. Anchor the operation on text that appears within the table.`,
          );
        } else if (
          edit.type === "insert_row" &&
          Array.isArray(edit.cells) &&
          edit.cells.length > n_cols
        ) {
          errors.push(
            `- Edit ${i + 1 + index_offset} Failed: insert_row provides ${edit.cells.length} cells but the target table has ${n_cols} column(s). The extra cell(s) would be dropped. Provide at most ${n_cols} cells — rows given fewer cells are padded with empty ones.`,
          );
        }
      }
    }
    return errors;
  }

  /**
   * Number of columns (w:tc elements) in the table row containing the text at
   * [start, start+length) in `mapper`, or null if that text is not inside a
   * table row.
   */
  /**
   * True when a replacement anchored in a table would ADD line-separated
   * pipe-delimited content — the text shape of a table row. Writing that
   * into a cell renders a fake row inside one cell while the real grid
   * stays unchanged (QA 2026-07-18 C2); such edits must use insert_row.
   */
  private static _introduces_table_row_text(
    mapper: DocumentMapper,
    start: number,
    length: number,
    final_target: string,
    final_new: string,
  ): boolean {
    if (!final_new.includes("\n") || !final_new.includes(" | ")) return false;
    const new_pipe_lines = final_new
      .split("\n")
      .filter((line) => line.includes(" | ")).length;
    const old_pipe_lines = final_target
      .split("\n")
      .filter((line) => line.includes(" | ")).length;
    if (new_pipe_lines <= old_pipe_lines) return false;
    return (
      RedlineEngine._column_count_at(mapper, start, Math.max(length, 1)) !==
      null
    );
  }

  private static _column_count_at(
    mapper: DocumentMapper,
    start: number,
    length: number,
  ): number | null {
    for (const s of mapper.spans) {
      if (s.end <= start || s.start >= start + length) {
        continue;
      }
      let curr: Node | null = null;
      if (s.run !== null) {
        curr = s.run._element;
      } else if (s.paragraph !== null) {
        curr = s.paragraph._element;
      }
      while (curr) {
        if (curr.nodeType === 1 && (curr as Element).tagName === "w:tr") {
          return findAllDescendants(curr as Element, "w:tc").filter(
            (tc) => tc.parentNode === curr,
          ).length;
        }
        curr = curr.parentNode;
      }
    }
    return null;
  }

  public validate_review_actions(actions: any[]): string[] {
    const errors: string[] = [];

    // Document-context-free shape checks (QA 2026-07-19 v8 F-07), mirroring
    // Python's validate_review_action_batch: blank replies render as empty
    // Word comment bubbles; a duplicated or conflicting accept/reject on one
    // target_id either double-counts as "applied" or contradicts itself.
    // Distinct IDs one action resolves as a group (a modification's del+ins
    // pair) stay legitimate, as do DIFFERENT replies to the same comment.
    const seen_resolutions = new Map<string, [number, string]>();
    const seen_replies = new Set<string>();
    for (let i = 0; i < actions.length; i++) {
      const action = actions[i];
      const type = action.type;
      const target_id = action.target_id ?? "";
      if (type === "reply") {
        if (!String(action.text ?? "").trim()) {
          errors.push(
            `- Action ${i + 1} Failed: reply text for ${target_id} is empty or ` +
              `whitespace-only. Word would show a blank comment bubble — provide the ` +
              `reply content in 'text'.`,
          );
          continue;
        }
        const reply_key = `${target_id} ${String(action.text).trim()}`;
        if (seen_replies.has(reply_key)) {
          errors.push(
            `- Action ${i + 1} Failed: duplicate reply — this batch already replies to ` +
              `${target_id} with the same text. Remove the duplicate action.`,
          );
        }
        seen_replies.add(reply_key);
      } else if (type === "accept" || type === "reject") {
        const prior = seen_resolutions.get(target_id);
        if (prior !== undefined) {
          const [first_idx, first_type] = prior;
          if (first_type === type) {
            errors.push(
              `- Action ${i + 1} Failed: duplicate action — Action ${first_idx + 1} in this ` +
                `batch already applies '${type}' to ${target_id}. A change can only be ` +
                `resolved once; remove the duplicate action.`,
            );
          } else {
            errors.push(
              `- Action ${i + 1} Failed: conflicting actions — Action ${first_idx + 1} in ` +
                `this batch applies '${first_type}' to ${target_id}, but this action applies ` +
                `'${type}'. Decide the outcome and keep exactly one of them.`,
            );
          }
        } else {
          seen_resolutions.set(target_id, [i, type]);
        }
      }
    }
    if (errors.length > 0) return errors;

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
            this._action_not_found_error(action.target_id, "reply", `- Action ${i + 1} Failed:`),
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
            this._action_not_found_error(action.target_id, type, `- Action ${i + 1} Failed:`),
          );
        }
      }
    }
    return errors;
  }

  public process_batch(
    changes: DocumentChange[],
    dry_run: boolean = false,
  ): any {
    // Defensive sanitization: some LLM clients "double-serialize" nested
    // arrays, delivering each element of `changes` as a JSON string instead of
    // a parsed object. Downstream code mutates state trackers (e.g.
    // `edit._applied_status`) and reads `change.type` on these elements, which
    // throws a TypeError on string primitives. Parse stringified elements back
    // into objects here, leaving genuine objects (and unparseable strings)
    // untouched so validation can surface a clear error rather than crashing.
    if (Array.isArray(changes)) {
      changes = changes.map((item: any) => {
        if (typeof item === "string") {
          try {
            const parsed = JSON.parse(item);
            // Only swap in the parsed value if it is an object; a string that
            // parses to a scalar (e.g. "42") is not a valid change.
            if (parsed !== null && typeof parsed === "object") {
              return parsed;
            }
            return item;
          } catch {
            // Leave malformed strings as-is; the validation pass downstream
            // will report them rather than crashing on a raw TypeError.
            return item;
          }
        }
        return item;
      }) as DocumentChange[];
    }

    if (dry_run) {
      const snapshot = takeSnapshot(this.doc);
      const originalCurrentId = this.current_id;
      try {
        return this._process_batch_internal(changes, true);
      } finally {
        restoreSnapshot(this.doc, snapshot);
        this.current_id = originalCurrentId;
        this.mapper = new DocumentMapper(this.doc);
        this.comments_manager = new CommentsManager(this.doc);
        this.clean_mapper = null;
      }
    } else {
      return this._process_batch_internal(changes, false);
    }
  }

  private _process_batch_internal(
    changes: DocumentChange[],
    dry_run_mode: boolean = false,
  ): any {
    // Pre-process edits: strip identical leading heading hashes from target_text and new_text
    for (const c of changes) {
      if (
        c &&
        typeof c === "object" &&
        (c as any).type === "modify" &&
        (c as any).target_text &&
        (c as any).new_text
      ) {
        const [strippedTarget, strippedNew] = stripMatchingHeadingHashes(
          (c as any).target_text,
          (c as any).new_text,
        );
        (c as any).target_text = strippedTarget;
        (c as any).new_text = strippedNew;
      }
    }

    this.skipped_details = [];

    const actions = changes.filter(
      (c) =>
        c !== null &&
        typeof c === "object" &&
        ["accept", "reject", "reply"].includes(c.type),
    );
    const edits = changes.filter(
      (c) =>
        c === null ||
        typeof c !== "object" ||
        !["accept", "reject", "reply"].includes(c.type),
    );

    // Never pre-unwrap a foreign author's <w:ins> to make a partially
    // straddling edit fit: that turns their tracked-inserted text into
    // untracked committed body text before the edit applies, destroying
    // their provenance. A partial straddle surfaces the standard validation
    // error ("Modification targets an active insertion from another author
    // …") via validate_edits, matching the Python engine. An edit fully
    // CONTAINED inside a foreign <w:ins> is allowed and handled by nesting
    // the <w:del> inside that <w:ins> (see _apply_single_edit_indexed /
    // _insert_and_split_ins).

    // BUG-7: Unified single-pass validation in wet-run / standard mode.
    // The document-aware pairing check runs BEFORE any action mutates the
    // DOM: accept + reject across one replacement's del+ins pair is a
    // contradiction, not two independent operations (ADEU-QA-004).
    if (!dry_run_mode) {
      let action_errors =
        actions.length > 0 ? this.validate_review_actions(actions) : [];
      if (actions.length > 0 && action_errors.length === 0) {
        action_errors = this.validate_action_pairing(actions);
      }
      const validate_edits_now = edits.length > 0 && action_errors.length > 0;
      const edit_errors = validate_edits_now ? this.validate_edits(edits) : [];
      const all_errors = [...action_errors, ...edit_errors];
      if (all_errors.length > 0) {
        throw new BatchValidationError(all_errors);
      }
    } else {
      if (actions.length > 0) {
        let action_errors = this.validate_review_actions(actions);
        if (action_errors.length === 0) {
          action_errors = this.validate_action_pairing(actions);
        }
        if (action_errors.length > 0) {
          throw new BatchValidationError(action_errors);
        }
      }
    }

    let applied_actions = 0;
    let skipped_actions = 0;
    let already_resolved_actions = 0;
    if (actions.length > 0) {
      const res = this.apply_review_actions(actions);
      applied_actions = res[0];
      skipped_actions = res[1];
      already_resolved_actions = res[2];
      if (skipped_actions > 0) {
        throw new BatchValidationError(this.skipped_details);
      }
      if (applied_actions > 0) {
        this.mapper["_build_map"]();
        if (this.clean_mapper) this.clean_mapper["_build_map"]();
      }
    }

    const [body_text] = split_structural_appendix(this.mapper.full_text);
    const pag_res = paginate(body_text, "");
    const page_offsets = pag_res.body_page_offsets;

    const edits_reports: any[] = [];
    let applied_edits = 0;
    let skipped_edits = 0;

    if (edits.length > 0) {
      // Sequential application rebuilds the mapper after every applied edit,
      // shifting every position at/after that edit. Caller-pinned indexes
      // (_match_start_index / _resolved_start_idx, e.g. generate_edits_from_text
      // output) are coordinates in the INITIAL document state, so apply indexed
      // edits bottom-up first — positions below an applied edit never move (the
      // same invariant apply_edits' reverse sweep relies on) — then let
      // text-anchored edits re-resolve against the mutated text as before.
      // Reports are keyed by `i` so they stay in batch order.
      const pinned_idx = (e: any): number | null => {
        if (
          e._resolved_start_idx !== undefined &&
          e._resolved_start_idx !== null
        )
          return e._resolved_start_idx;
        if (
          e._match_start_index !== undefined &&
          e._match_start_index !== null
        )
          return e._match_start_index;
        return null;
      };
      const ordered_edits = edits
        .map((edit, i) => ({ edit: edit as any, i }))
        .sort((a, b) => {
          const ka = pinned_idx(a.edit);
          const kb = pinned_idx(b.edit);
          if (ka === null && kb === null) return a.i - b.i;
          if (ka === null) return 1;
          if (kb === null) return -1;
          return kb - ka || a.i - b.i;
        });

      if (dry_run_mode) {
        const reports_by_input: any[] = new Array(edits.length);
        // Indexes that failed VALIDATION (not runtime skips): if any exist,
        // the real run rejects the whole batch, so the dry-run report must
        // not claim any edit "applied" (transactional parity with Python).
        const validation_failed_idx = new Set<number>();
        for (const { edit, i } of ordered_edits) {
          let single_errors: string[];
          try {
            single_errors = this.validate_edits([edit], i);
          } catch (e) {
            // A pathological user pattern must fail as a clean per-edit
            // validation error, never a hang or crash (QA 2026-07-17 F5).
            if (!(e instanceof RegexTimeoutError)) throw e;
            single_errors = [`- Edit ${i + 1} Failed: ${e.message}`];
          }
          if (single_errors.length > 0) {
            if (applied_edits > 0) {
              const hint = sequential_context_hint(applied_edits);
              single_errors = single_errors.map((err) => err + hint);
            }
            validation_failed_idx.add(i);
            skipped_edits++;
            // Only surface the punctuation-anchor warning when the edit actually
            // failed. A clean apply already returns the redline preview, so the
            // warning is pure noise on success — and it misleads agents into
            // hunting for a "cleaner" anchor that was never needed (e.g. on
            // placeholders/dates where the punctuation IS the literal target).
            const warning = this._check_punctuation_warning(
              (edit as any).target_text || "",
            );
            reports_by_input[i] = {
              status: "failed",
              type: (edit as any).type || "modify",
              target_text: truncate_middle((edit as any).target_text || "", REPORT_ECHO_CAP),
              new_text: truncate_middle(RedlineEngine._report_new_text(edit), REPORT_ECHO_CAP),
              warning: warning,
              error: single_errors.join("\n"),
              critic_markup: null,
              clean_text: null,
            };
            continue;
          }
          const res = this.apply_edits([edit], page_offsets);
          if ((edit as any)._applied_status) {
            applied_edits++;
            const previews = this._build_edit_context_previews(edit);
            reports_by_input[i] = {
              status: "applied",
              type: (edit as any).type || "modify",
              target_text: truncate_middle((edit as any).target_text || "", REPORT_ECHO_CAP),
              new_text: truncate_middle(RedlineEngine._report_new_text(edit), REPORT_ECHO_CAP),
              warning: null,
              error: null,
              critic_markup: previews[0],
              clean_text: previews[1],
              pages: (edit as any)._pages || [],
              heading_path: (edit as any)._heading_path || "",
              occurrences_modified: (edit as any)._occurrences_modified || 0,
              match_mode: (edit as any).match_mode || "strict",
            };
            this.mapper = new DocumentMapper(this.doc);
            this.clean_mapper = null;
          } else {
            skipped_edits++;
            const error_msg =
              this.skipped_details.length > 0
                ? this.skipped_details[this.skipped_details.length - 1]
                : "Failed to apply edit";
            const warning = this._check_punctuation_warning(
              (edit as any).target_text || "",
            );
            reports_by_input[i] = {
              status: "failed",
              type: (edit as any).type || "modify",
              target_text: truncate_middle((edit as any).target_text || "", REPORT_ECHO_CAP),
              new_text: truncate_middle(RedlineEngine._report_new_text(edit), REPORT_ECHO_CAP),
              warning: warning,
              error: error_msg,
              critic_markup: null,
              clean_text: null,
            };
          }
        }
        if (validation_failed_idx.size > 0) {
          // Dry-run mirrors the real run's transactional rejection: no edit
          // will be applied by the real run, so none may be reported as
          // applied here. Edits that only failed at runtime keep their own
          // error; edits that would have applied get the transactional note.
          applied_edits = 0;
          skipped_edits = edits.length;
          for (let i = 0; i < reports_by_input.length; i++) {
            const report = reports_by_input[i];
            if (!report || validation_failed_idx.has(i)) continue;
            if (report.status === "applied") {
              reports_by_input[i] = {
                status: "failed",
                type: report.type || "modify",
                target_text: report.target_text,
                new_text: report.new_text,
                warning: null,
                error: TRANSACTIONAL_NOT_APPLIED_ERROR,
                critic_markup: null,
                clean_text: null,
              };
            }
          }
        }
        edits_reports.push(...reports_by_input);
      } else {
        // Simulated dry-run sequentially for wet-run validation parity
        const snapshot = takeSnapshot(this.doc);
        const originalCurrentId = this.current_id;
        try {
          const sequential_errors: string[] = [];
          let applied_so_far = 0;
          for (const { edit, i } of ordered_edits) {
            let single_errors: string[];
            try {
              single_errors = this.validate_edits([edit], i);
            } catch (e) {
              // Clean per-edit failure for time-budget violations (QA F5).
              if (!(e instanceof RegexTimeoutError)) throw e;
              single_errors = [`- Edit ${i + 1} Failed: ${e.message}`];
            }
            if (single_errors.length > 0) {
              if (applied_so_far > 0) {
                const hint = sequential_context_hint(applied_so_far);
                single_errors = single_errors.map((err) => err + hint);
              }
              sequential_errors.push(...single_errors);
            } else {
              this.apply_edits([edit], page_offsets);
              if ((edit as any)._applied_status) {
                applied_so_far++;
              }
              this.mapper = new DocumentMapper(this.doc);
              this.clean_mapper = null;
            }
          }
          if (sequential_errors.length > 0) {
            throw new BatchValidationError(sequential_errors);
          }
        } catch (err) {
          restoreSnapshot(this.doc, snapshot);
          this.current_id = originalCurrentId;
          this.mapper = new DocumentMapper(this.doc);
          this.comments_manager = new CommentsManager(this.doc);
          this.clean_mapper = null;
          throw err;
        }

        applied_edits = edits.filter((e) => (e as any)._applied_status).length;
        skipped_edits = edits.length - applied_edits;

        for (const edit of edits) {
          const success = (edit as any)._applied_status || false;
          const error_msg = (edit as any)._error_msg || null;
          let critic_markup = null;
          let clean_text = null;
          // Punctuation-anchor warning is failure-context only: on success the
          // redline preview below already reports the change cleanly.
          let warning: string | null = null;
          if (success) {
            const previews = this._build_edit_context_previews(edit);
            critic_markup = previews[0];
            clean_text = previews[1];
          } else {
            warning = this._check_punctuation_warning(
              (edit as any).target_text || "",
            );
          }
          edits_reports.push({
            status: success ? "applied" : "failed",
            type: (edit as any).type || "modify",
            target_text: truncate_middle((edit as any).target_text || "", REPORT_ECHO_CAP),
            new_text: truncate_middle(RedlineEngine._report_new_text(edit), REPORT_ECHO_CAP),
            warning: warning,
            error: error_msg,
            critic_markup: critic_markup,
            clean_text: clean_text,
            pages: (edit as any)._pages || [],
            heading_path: (edit as any)._heading_path || "",
            occurrences_modified: (edit as any)._occurrences_modified || 0,
            match_mode: (edit as any).match_mode || "strict",
          });
        }
      }
    }
    return {
      actions_applied: applied_actions,
      actions_skipped: skipped_actions,
      // Actions whose target was already resolved by an earlier action of
      // this batch (via its replacement pair): consistent no-ops, never
      // counted as applied — every reported "applied" action causes an
      // observable state transition (ADEU-QA-004).
      actions_already_resolved: already_resolved_actions,
      edits_applied: applied_edits,
      edits_skipped: skipped_edits,
      // edits_applied counts change OBJECTS; this is the total number of
      // document occurrences they modified (match_mode="all" fan-out), so
      // automation never has to guess which of the two a count means
      // (QA 2026-07-19 F-21).
      occurrences_modified: edits_reports.reduce(
        (sum: number, r: any) => sum + (r.occurrences_modified || 0),
        0,
      ),
      skipped_details: this.skipped_details,
      edits: edits_reports,
      engine: "node",
      version: "1.18.2",
    };
  }

  public apply_edits(
    edits: any[],
    page_offsets: number[] = [],
  ): [number, number] {
    let applied = 0;
    let skipped = 0;

    if (!page_offsets || page_offsets.length === 0) {
      const [body_text] = split_structural_appendix(this.mapper.full_text);
      page_offsets = paginate(body_text, "").body_page_offsets;
    }
    const resolved_edits: [any, string | null][] = [];

    for (const edit of edits) {
      if (typeof edit !== "object" || edit === null) {
        skipped++;
        continue;
      }
      edit._applied_status = false;
      edit._error_msg = null;
      edit._any_sub_failure = false;
    }

    for (const edit of edits) {
      if (typeof edit !== "object" || edit === null) continue;

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
        let matches = this.mapper.drop_virtual_only_matches(
          this.mapper.find_all_match_indices(edit.target_text),
        );
        let resolved_mapper = this.mapper;
        if (matches.length === 0) {
          if (!this.clean_mapper) {
            this.clean_mapper = new DocumentMapper(this.doc, true);
          }
          matches = this.clean_mapper.drop_virtual_only_matches(
            this.clean_mapper.find_all_match_indices(edit.target_text),
          );
          resolved_mapper = this.clean_mapper;
        }

        if (matches.length > 0) {
          const match_mode = edit.match_mode || "strict";

          // We need to resolve matches to unique w:tr elements to deduplicate them.
          const unique_matches: [number, number][] = [];
          const seen_trs = new Set<any>();

          for (const match of matches) {
            const start_idx = match[0];
            const [anchor_run, anchor_para] = resolved_mapper.get_insertion_anchor(start_idx, false);
            let target_element: Element | null = null;
            if (anchor_run) target_element = anchor_run._element;
            else if (anchor_para) target_element = anchor_para._element;

            let tr: Element | null = target_element;
            while (tr && tr.tagName !== "w:tr") tr = tr.parentNode as Element;

            if (tr) {
              if (!seen_trs.has(tr)) {
                seen_trs.add(tr);
                unique_matches.push(match);
              }
            }
          }

          if (unique_matches.length > 0) {
            let matches_to_apply = unique_matches;
            if (match_mode === "strict" || match_mode === "first") {
              matches_to_apply = unique_matches.slice(0, 1);
            }

            if (match_mode === "all" || matches_to_apply.length > 1) {
              // Create sub-edits for each match so that they are processed as independent operations,
              // and the occurrences_modified and applied_status are tracked correctly on the parent.
              for (const m of matches_to_apply) {
                const sub_edit = {
                  ...edit,
                  _resolved_start_idx: m[0],
                  _active_mapper_ref: resolved_mapper,
                  _parent_edit_ref: edit,
                };
                resolved_edits.push([sub_edit, null]);
              }
            } else {
              // Single match case for non-"all" modes
              edit._resolved_start_idx = matches_to_apply[0][0];
              edit._active_mapper_ref = resolved_mapper;
              resolved_edits.push([edit, null]);
            }
          } else {
            skipped++;
            edit._applied_status = false;
            const target_snippet = (edit.target_text || "")
              .trim()
              .substring(0, 40);
            const msg = `- Failed to locate row target: '${target_snippet}...'`;
            this.skipped_details.push(msg);
            edit._error_msg = msg;
          }
        } else {
          skipped++;
          edit._applied_status = false;
          const target_snippet = (edit.target_text || "")
            .trim()
            .substring(0, 40);
          const msg = `- Failed to locate row target: '${target_snippet}...'`;
          this.skipped_details.push(msg);
          edit._error_msg = msg;
        }
      } else {
        let resolved: any;
        try {
          resolved = this._pre_resolve_heuristic_edit(edit);
        } catch (e) {
          // Direct apply_edits callers bypass validate_edits; the time
          // budget must still fail cleanly here (QA F5).
          if (!(e instanceof RegexTimeoutError)) throw e;
          skipped++;
          edit._applied_status = false;
          const msg = `- Failed to apply edit targeting: '${(edit.target_text || "").substring(0, 40)}...' (${e.message})`;
          this.skipped_details.push(msg);
          edit._error_msg = msg;
          continue;
        }
        if (resolved) {
          if (Array.isArray(resolved)) {
            for (const r of resolved) {
              r._resolved_start_idx = r._match_start_index;
              r._parent_edit_ref = edit;
              if (
                edit._resolved_start_idx === undefined ||
                edit._resolved_start_idx === null
              ) {
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
      (a, b) =>
        (b[0]._resolved_start_idx || 0) - (a[0]._resolved_start_idx || 0),
    );

    // Snapshot preview context now, while every resolved offset still refers
    // to the untouched document. The sweep below mutates the DOM and rebuilds
    // the map, shifting offsets and injecting tracked-change markup —
    // slicing full_text at report time garbles previews (QA H1).
    for (const [res_edit] of resolved_edits) {
      this._capture_preview_context(res_edit);
      if (res_edit._parent_edit_ref) {
        this._capture_parent_preview_context(res_edit._parent_edit_ref);
      }
    }

    const occupied_ranges: [number, number][] = [];
    // Sub-edits split from one balanced multi-paragraph modification share a
    // _split_group_id; count the group as a single applied edit (and a single
    // occurrence), even though it touches several paragraphs.
    const counted_split_groups = new Set<number>();

    for (const [edit, orig_new] of resolved_edits) {
      const start = edit._resolved_start_idx || 0;
      // An insert_row does not consume its anchor text — it adds an adjacent
      // row. Give it a zero-width range so several inserts sharing one
      // anchor (consecutive new rows) never flag each other as overlapping.
      const end =
        edit.type === "insert_row"
          ? start
          : start + (edit.target_text ? edit.target_text.length : 0);

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
        edit._any_sub_failure = true;
        const parent = edit._parent_edit_ref;
        if (parent) {
          parent._applied_status = false;
          parent._error_msg = msg;
          parent._any_sub_failure = true;
        }
        continue;
      }

      let success = false;
      if (edit.type === "modify") {
        // Never rebuild the map inside the sweep: sub-edits apply in strictly
        // descending offset order, and every DOM mutation (run splits, w:del
        // wraps, w:ins insertions, bottom-up paragraph merges) happens at or
        // above the current offset, so spans below it stay valid in the stale
        // map. Rebuilding here made regex + match_mode="all"
        // O(occurrences × document) (QA 2026-07-19 F-06).
        success = this._apply_single_edit_indexed(edit, orig_new, false);
      } else if (edit.type === "insert_row" || edit.type === "delete_row") {
        success = this._apply_table_edit(edit, false);
      }

      if (success) {
        // A balanced multi-paragraph split fans one logical edit into several
        // paragraph sub-edits sharing a _split_group_id; count it once. Edits
        // with no group id (the common case) always count.
        const group_id = edit._split_group_id;
        const first_in_group =
          group_id === undefined ||
          group_id === null ||
          !counted_split_groups.has(group_id);
        if (first_in_group && group_id !== undefined && group_id !== null) {
          counted_split_groups.add(group_id);
        }
        if (first_in_group) applied++;
        occupied_ranges.push([start, end]);
        edit._applied_status = true;
        const parent = edit._parent_edit_ref;
        if (parent) {
          parent._applied_status = true;
          if (first_in_group) {
            parent._occurrences_modified =
              (parent._occurrences_modified || 0) + 1;
          }
          const [path, page] = this._get_heading_path_and_page(
            start,
            this.mapper.full_text,
            page_offsets,
          );
          const pages: number[] = parent._pages || [];
          if (!pages.includes(page)) pages.unshift(page);
          parent._pages = pages;
          parent._heading_path = path;
        } else {
          if (first_in_group) {
            edit._occurrences_modified = (edit._occurrences_modified || 0) + 1;
          }
          const [path, page] = this._get_heading_path_and_page(
            start,
            this.mapper.full_text,
            page_offsets,
          );
          const pages: number[] = edit._pages || [];
          if (!pages.includes(page)) pages.unshift(page);
          edit._pages = pages;
          edit._heading_path = path;
        }
      } else {
        skipped++;
        const display_text = edit.target_text || "insertion";
        const target_snippet = display_text.trim().substring(0, 40);
        const msg = `- Failed to apply edit targeting: '${target_snippet}...'`;
        this.skipped_details.push(msg);
        edit._applied_status = false;
        edit._error_msg = msg;
        edit._any_sub_failure = true;
        const parent = edit._parent_edit_ref;
        if (parent) {
          parent._any_sub_failure = true;
          if (!parent._applied_status) {
            parent._applied_status = false;
            parent._error_msg = msg;
          }
        }
      }
    }

    // Return LOGICAL edit counts over the caller's input list: one
    // match_mode="all" edit over N occurrences is one applied edit (its
    // occurrence count lives in _occurrences_modified / the report), never N
    // (QA 2026-07-19 F-21). An edit with any failed or skipped sub-edit
    // counts as skipped so the all-or-nothing batch contract is unchanged.
    let applied_logical = 0;
    let skipped_logical = 0;
    for (const input_edit of edits) {
      if (typeof input_edit !== "object" || input_edit === null) {
        skipped_logical++;
        continue;
      }
      if (input_edit._applied_status && !input_edit._any_sub_failure) {
        applied_logical++;
      } else {
        skipped_logical++;
      }
    }
    return [applied_logical, skipped_logical];
  }

  /**
   * True when the paragraph still carries visible content (w:t text, w:tab,
   * w:br) that is NOT wrapped in a tracked deletion — i.e. the paragraph
   * would render non-empty in the accepted document.
   */
  private _paragraph_has_visible_content(p_elem: Element): boolean {
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
          return true;
        }
      }
    }
    return false;
  }

  /**
   * All contiguous same-author w:ins/w:del siblings that form one logical
   * modification block with `node` (a replacement's del+ins pair). Mirrors
   * the Python engine's _get_paired_nodes: comment range markers and
   * rPr/pPr are transparent; a different author or any other element breaks
   * the group.
   */
  private _get_paired_nodes(node: Element): Element[] {
    const pairs: Element[] = [];
    const author = node.getAttribute("w:author");
    const transparent = new Set([
      "w:commentRangeStart",
      "w:commentRangeEnd",
      "w:commentReference",
      "w:rPr",
      "w:pPr",
    ]);

    const walk = (start: Element, dir: "next" | "prev") => {
      let cur: Node | null =
        dir === "next" ? start.nextSibling : start.previousSibling;
      while (cur) {
        if (cur.nodeType !== 1) {
          cur = dir === "next" ? cur.nextSibling : cur.previousSibling;
          continue;
        }
        const el = cur as Element;
        if (transparent.has(el.tagName)) {
          cur = dir === "next" ? cur.nextSibling : cur.previousSibling;
          continue;
        }
        if (
          (el.tagName === "w:ins" || el.tagName === "w:del") &&
          el.getAttribute("w:author") === author
        ) {
          pairs.push(el);
          cur = dir === "next" ? cur.nextSibling : cur.previousSibling;
          continue;
        }
        break;
      }
    };

    walk(node, "next");
    walk(node, "prev");
    return pairs;
  }

  /**
   * All revision ids that resolve as ONE unit with `target_id`: the ids of
   * every contiguous same-author w:ins/w:del sibling of its elements (a
   * replacement's del+ins pair), plus the id itself.
   */
  private _resolution_group_ids(target_id: string): Set<string> {
    const nodes = [
      ...findAllDescendants(this.doc.element, "w:ins"),
      ...findAllDescendants(this.doc.element, "w:del"),
    ].filter((n) => n.getAttribute("w:id") === target_id);
    const group = new Set<string>();
    if (nodes.length === 0) return group;
    group.add(target_id);
    for (const node of nodes) {
      for (const paired of this._get_paired_nodes(node)) {
        const pid = paired.getAttribute("w:id");
        if (pid) group.add(pid);
      }
    }
    return group;
  }

  /**
   * Document-aware validation (QA 2026-07-19 ADEU-QA-004): a replacement's
   * del+ins pair carries two distinct ids but resolves as one unit, so a
   * batch that accepts one side and rejects the other is contradictory.
   * Rejecting it up front — before any action mutates the document — keeps
   * the batch transactional.
   */
  public validate_action_pairing(actions: any[]): string[] {
    const errors: string[] = [];
    const group_first = new Map<string, [number, string, string]>();
    for (let pos = 0; pos < actions.length; pos++) {
      const act = actions[pos];
      if (act.type !== "accept" && act.type !== "reject") continue;
      const raw_id = String(act.target_id ?? "");
      if (raw_id.startsWith("Com:")) continue;
      const target_id = raw_id.startsWith("Chg:") ? raw_id.slice(4) : raw_id;
      const group = this._resolution_group_ids(target_id);
      if (group.size === 0) continue; // unknown ids fail with their own not-found error
      let conflict: [number, string, string] | null = null;
      for (const gid of group) {
        const prior = group_first.get(gid);
        if (prior !== undefined && prior[1] !== act.type) {
          conflict = prior;
          break;
        }
      }
      if (conflict !== null) {
        const [first_pos, first_type, first_id] = conflict;
        errors.push(
          `- Action ${pos + 1} Failed: conflicting actions on one replacement — Action ` +
            `${first_pos + 1} applies '${first_type}' to Chg:${first_id}, and Chg:${target_id} is ` +
            `part of the same change (a replacement's contiguous del+ins pair resolves as one ` +
            `unit, so '${first_type}' already decides both sides). Accepting one side and ` +
            `rejecting the other is contradictory — decide the outcome and submit exactly one ` +
            `action for the pair.`,
        );
        continue;
      }
      for (const gid of group) {
        if (!group_first.has(gid)) {
          group_first.set(gid, [pos, act.type, target_id]);
        }
      }
    }
    return errors;
  }

  /**
   * Returns [applied, skipped, already_resolved]. `applied` counts actions
   * that caused an observable state transition; an action naming an id an
   * earlier action of this batch already resolved (via its replacement pair)
   * is counted in `already_resolved` instead — never as applied
   * (QA 2026-07-19 ADEU-QA-004).
   */
  /** Distinct tracked-change ids (w:id on w:ins/w:del) in the main story. */
  private _existing_change_ids(): string[] {
    const ids = new Set<string>();
    for (const tag of ["w:ins", "w:del"]) {
      for (const n of findAllDescendants(this.doc.element, tag)) {
        const id = n.getAttribute("w:id");
        if (id) ids.add(id);
      }
    }
    return Array.from(ids).sort((a, b) => {
      const na = /^\d+$/.test(a) ? parseInt(a, 10) : 0;
      const nb = /^\d+$/.test(b) ? parseInt(b, 10) : 0;
      return na - nb || a.localeCompare(b);
    });
  }

  /** Comment ids present in the document, sorted for display. */
  private _existing_comment_ids(): string[] {
    let ids: string[] = [];
    try {
      ids = Object.keys(extract_comments_data(this.doc.pkg));
    } catch {
      ids = [];
    }
    return ids.sort((a, b) => {
      const na = /^\d+$/.test(a) ? parseInt(a, 10) : 0;
      const nb = /^\d+$/.test(b) ? parseInt(b, 10) : 0;
      return na - nb || a.localeCompare(b);
    });
  }

  private static _format_id_list(ids: string[], prefix: string, limit = 20): string {
    const shown = ids.slice(0, limit);
    let rendered = shown.map((i) => `${prefix}${i}`).join(", ");
    if (ids.length > shown.length) {
      rendered += `, … (+${ids.length - shown.length} more)`;
    }
    return rendered;
  }

  /**
   * Self-service diagnostic for accept/reject/reply on an id that resolved
   * nothing. The other errors in this engine explain WHY and HOW to recover;
   * this path used to emit only "Target ID X not found" with no way to find a
   * valid id (QA 2026-07-22 bug #3). Names the expected id kind, lists the ids
   * that actually exist, flags the common change/comment id mix-up, and points
   * at the command that prints current ids. `lead` is the full sentence
   * prefix (e.g. "- Action 3 Failed:") so callers can match the surrounding
   * error style.
   */
  private _action_not_found_error(
    raw_id: string,
    type: string,
    lead = "- Failed to apply action:",
  ): string {
    const change_ids = this._existing_change_ids();
    const comment_ids = this._existing_comment_ids();
    const has_prefix = raw_id.startsWith("Chg:") || raw_id.startsWith("Com:");
    // Bare numeric id, regardless of which prefix (or none) the caller used.
    const bare = raw_id.replace(/^(Chg:|Com:)/, "");
    const find_hint =
      "Run `adeu markup <file> -i` or `adeu extract <file>` to list the current " +
      "change (Chg:) and comment (Com:) ids.";

    if (type === "reply") {
      const echo = has_prefix ? raw_id : `Com:${bare}`;
      if (change_ids.includes(bare)) {
        return (
          `${lead} reply on ${echo} — Chg:${bare} is a tracked-change id, not a comment. ` +
          "`reply` adds to an existing comment thread (Com:…); to comment on a change instead, " +
          `apply a modify with a \`comment\`. ${find_hint}`
        );
      }
      const avail =
        comment_ids.length > 0
          ? `Comment ids in this document: ${RedlineEngine._format_id_list(comment_ids, "Com:")}. `
          : "This document has no comments to reply to. ";
      return `${lead} reply on ${echo} — no comment with that id exists. ${avail}${find_hint}`;
    }

    const echo = has_prefix ? raw_id : `Chg:${bare}`;
    if (comment_ids.includes(bare)) {
      return (
        `${lead} ${type} on ${echo} — Com:${bare} is a comment id, ` +
        `not a tracked change. accept/reject act on tracked changes (Chg:…); to respond to a ` +
        `comment use \`reply\`. ${find_hint}`
      );
    }
    const avail =
      change_ids.length > 0
        ? `Tracked-change ids in this document: ${RedlineEngine._format_id_list(change_ids, "Chg:")}. `
        : "This document has no tracked changes. ";
    return (
      `${lead} ${type} on ${echo} — no tracked change with that id exists ` +
      `(it may already have been accepted or rejected, or the id is stale). ${avail}${find_hint}`
    );
  }

  public apply_review_actions(actions: any[]): [number, number, number] {
    let applied = 0;
    let skipped = 0;
    let already_resolved = 0;
    const resolved_history = new Map<string, string>(); // id -> resolving action type

    // Sort actions internally: non-destructive metadata operations (ReplyComment) first,
    // followed by destructive structural operations (AcceptChange, RejectChange).
    // Stable sort preserves the original relative ordering, and we preserve `pos`
    // so diagnostic messages refer to the original array indexes.
    const sortedActions = actions
      .map((action, pos) => ({ action, pos }))
      .sort((a, b) => {
        const aPri = a.action.type === "reply" ? 0 : 1;
        const bPri = b.action.type === "reply" ? 0 : 1;
        if (aPri !== bPri) {
          return aPri - bPri;
        }
        return a.pos - b.pos;
      });

    for (const { action, pos } of sortedActions) {
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

      const prior_type = resolved_history.get(target_id);
      if (prior_type !== undefined) {
        if (prior_type === type) {
          // Consistent follow-up on the pair: legitimate agent workflow
          // ("accept both ids of the replacement"), but no state transition
          // happens — report it accurately (ADEU-QA-004).
          already_resolved++;
          this.skipped_details.push(
            `- Note: Action ${pos + 1} ('${type}' on ${action.target_id}) had no additional effect — ` +
              `the change was already resolved together with its replacement pair by an earlier ` +
              `action in this batch. Counted as already_resolved, not applied.`,
          );
          continue;
        }
        // Contradiction. validate_action_pairing rejects this shape before
        // anything mutates; this guard covers direct callers.
        this.skipped_details.push(
          `- Action ${pos + 1} Failed: contradictory action — '${type}' on ${action.target_id}, but ` +
            `the change was already resolved as '${prior_type}' together with its replacement ` +
            `pair by an earlier action in this batch.`,
        );
        skipped++;
        continue;
      }

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
          this._action_not_found_error(action.target_id, type),
        );
        continue;
      }

      // Refuse accept/reject on a w:id shared by revisions from DIFFERENT
      // authors. Uniqueness of w:id is assumed but not guaranteed for
      // externally produced documents (merges, cross-document copy-paste),
      // where one action would silently resolve several unrelated changes
      // (QA 2026-07-17 F9). Same-author reuse is legitimate — this engine
      // itself mints one id across every element of a single logical edit —
      // so authorship is the discriminator.
      const dup_authors = Array.from(
        new Set(all_nodes.map((n) => n.getAttribute("w:author") || "Unknown")),
      ).sort();
      if (dup_authors.length > 1) {
        skipped++;
        this.skipped_details.push(
          `- Failed to apply action: ${type} on Chg:${target_id} is ambiguous. The document ` +
            `contains ${all_nodes.length} tracked-change elements sharing w:id=${target_id} from ` +
            `different authors (${dup_authors.join(", ")}) — duplicate revision IDs produced ` +
            `outside this engine (e.g. by a document merge or copy-paste). Acting on this ID ` +
            `would resolve all of them at once. Resolve these changes individually in Word, or ` +
            `apply the intended outcome as an explicit text edit instead.`,
        );
        continue;
      }

      // A modification is one logical unit stored as a contiguous
      // same-author del+ins pair: resolving either side resolves BOTH —
      // Word's atomic replacement handling, and the Python engine's
      // long-standing behavior. Without this, accepting the deletion side
      // left the paired insertion pending (engine divergence,
      // QA 2026-07-19 ADEU-QA-004).
      const group_nodes = new Set<Element>(all_nodes);
      for (const node of all_nodes) {
        for (const paired of this._get_paired_nodes(node)) {
          group_nodes.add(paired);
        }
      }
      const resolved_now = new Set<string>();
      for (const node of group_nodes) {
        const rid = node.getAttribute("w:id");
        if (rid) resolved_now.add(rid);
      }

      // Accept/reject can delete a comment as a side effect when the comment's
      // anchor falls inside the resolved change. Snapshot the comment ids first
      // so a removal is reported explicitly instead of happening silently under
      // "1 applied" (QA 2026-07-22 bug #1).
      const comments_before = new Set(this._existing_comment_ids());

      for (const node of group_nodes) {
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
      for (const rid of resolved_now) {
        resolved_history.set(rid, type);
      }
      applied++;

      if (comments_before.size > 0) {
        const after = new Set(this._existing_comment_ids());
        const removed = Array.from(comments_before).filter((c) => !after.has(c));
        if (removed.length > 0) {
          const removed_list = removed
            .sort((a, b) => {
              const na = /^\d+$/.test(a) ? parseInt(a, 10) : 0;
              const nb = /^\d+$/.test(b) ? parseInt(b, 10) : 0;
              return na - nb || a.localeCompare(b);
            })
            .map((c) => `Com:${c}`)
            .join(", ");
          this.skipped_details.push(
            `- Note: ${type} on ${action.target_id} also removed comment ${removed_list} ` +
              `(including any reply thread) because its anchor was inside the resolved change. ` +
              `This note is informational — the action itself succeeded.`,
          );
        }
      }
    }
    return [applied, skipped, already_resolved];
  }

  private _apply_table_edit(edit: any, rebuild_map: boolean): boolean {
    const start_idx =
      edit._resolved_start_idx !== undefined &&
      edit._resolved_start_idx !== null
        ? edit._resolved_start_idx
        : edit._match_start_index || 0;
    // The offset must be looked up in the coordinate space it was resolved
    // in: a clean-view offset applied to the raw
    // mapper points at earlier text once tracked changes exist.
    const active_mapper: DocumentMapper = edit._active_mapper_ref || this.mapper;
    const [anchor_run, anchor_para] = active_mapper.get_insertion_anchor(
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
      // The new row must carry exactly as many cells as the anchor row has
      // columns: pad missing cells with empty strings and drop extras
      // (validation already rejects overfilled batches upfront, QA M3) so a
      // mismatched `cells` list can never produce a structurally
      // inconsistent table row.
      const anchor_cols = findAllDescendants(tr, "w:tc").filter(
        (tc) => tc.parentNode === tr,
      ).length;
      let cell_values: string[] = Array.isArray(edit.cells)
        ? [...edit.cells]
        : [];
      if (anchor_cols > 0) {
        while (cell_values.length < anchor_cols) cell_values.push("");
        cell_values = cell_values.slice(0, anchor_cols);
      }
      for (const cellText of cell_values) {
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

  private _pre_resolve_heuristic_edit(edit: any): any {
    if (!edit.target_text) return null;

    const is_regex = edit.regex || false;
    const match_mode = edit.match_mode || "strict";

    let matches = this.mapper.drop_virtual_only_matches(
      this.mapper.find_all_match_indices(edit.target_text, is_regex),
    );
    let use_clean_map = false;

    if (matches.length === 0) {
      if (!this.clean_mapper)
        this.clean_mapper = new DocumentMapper(this.doc, true);
      matches = this.clean_mapper.drop_virtual_only_matches(
        this.clean_mapper.find_all_match_indices(edit.target_text, is_regex),
      );
      if (matches.length > 0) use_clean_map = true;
      else return null;
    }

    const active_mapper = use_clean_map ? this.clean_mapper! : this.mapper;

    let live_matches: [number, number][] = [];
    for (const [s, match_len] of matches) {
      const realSpans = active_mapper.spans.filter(
        (span) =>
          span.run !== null && span.end > s && span.start < s + match_len,
      );
      // Virtual-only matches were already dropped above; here we only skip
      // matches buried entirely inside tracked deletions.
      if (realSpans.length === 0 || realSpans.some((span) => !span.del_id)) {
        live_matches.push([s, match_len]);
      }
    }

    if (live_matches.length === 0) return null;

    if (match_mode === "strict" || match_mode === "first") {
      live_matches = live_matches.slice(0, 1);
    }

    const all_sub_edits: any[] = [];

    for (const [start_idx, match_len] of live_matches) {
      const actual_doc_text = active_mapper.full_text.substring(
        start_idx,
        start_idx + match_len,
      );
      let current_effective_new_text = edit.new_text || "";

      // Cell anchors ({#cell:<paraId>}) are pure position markers with no real
      // content — they let the model address an empty (or any) table cell that
      // has no run to diff against. Treat such a target as a clean INSERTION at
      // the anchor's paragraph: never delete the marker, never run trim_common_context
      // (which refuses to split inside {#...} markup and yields a no-op MODIFICATION).
      // Strip any echoed anchor from new_text so the model can send either
      // "June 22, 2026" or "June 22, 2026{#cell:...}" and get the same result.
      if (/^\{#cell:[^}]+\}$/.test(actual_doc_text.trim())) {
        let ins_text = current_effective_new_text;
        // Drop a leading/trailing copy of the same anchor token if echoed.
        ins_text = ins_text.split(actual_doc_text.trim()).join("");
        if (ins_text) {
          all_sub_edits.push({
            type: "modify",
            target_text: "",
            new_text: ins_text,
            comment: edit.comment,
            // Insert at the anchor token's start so the new run lands inside
            // the cell paragraph that get_insertion_anchor resolves there.
            _match_start_index: start_idx,
            _internal_op: "INSERTION",
            _active_mapper_ref: active_mapper,
          });
        } else if (edit.comment) {
          // Anchor target with empty effective new_text but a comment: attach
          // the comment to the cell paragraph.
          all_sub_edits.push({
            type: "modify",
            target_text: "",
            new_text: "",
            comment: edit.comment,
            _match_start_index: start_idx,
            _internal_op: "COMMENT_ONLY",
            _active_mapper_ref: active_mapper,
          });
        }
        continue;
      }

      if (is_regex && current_effective_new_text) {
        try {
          current_effective_new_text = actual_doc_text.replace(
            new RegExp(edit.target_text),
            current_effective_new_text,
          );
        } catch (e) {}
      }

      // Stash the first occurrence's full match for the report preview, so it
      // can show the complete logical change rather than only the first
      // word-diff sub-edit (e.g. "{--two--}{++five++} (2) years" for a
      // "two (2) years" -> "five (5) years" edit). Mirrors Python (QA H1).
      if (!edit._preview_span) {
        edit._preview_span = [start_idx, match_len];
        edit._preview_matched_text = actual_doc_text;
        edit._preview_new_text = current_effective_new_text;
        edit._preview_mapper_ref = active_mapper;
      }

      const [edit_target_clean, edit_target_style] = this._parse_markdown_style(
        edit.target_text,
      );
      const [edit_new_clean, edit_new_style] = this._parse_markdown_style(
        current_effective_new_text,
      );

      if (edit_target_style !== edit_new_style) {
        const [actual_clean] = this._parse_markdown_style(actual_doc_text);
        const final_target = actual_clean;
        const final_new = edit_new_clean;
        const style_op =
          final_target === final_new ? "STYLE_ONLY" : "STYLE_AND_TEXT";
        const prefix_offset = actual_doc_text.indexOf(actual_clean);
        const effective_start_idx =
          start_idx + (prefix_offset !== -1 ? prefix_offset : 0);
        const resolved_style =
          edit_new_style !== null ? edit_new_style : "Normal";

        all_sub_edits.push({
          type: "modify",
          target_text: final_target,
          new_text: final_new,
          comment: edit.comment,
          _match_start_index: effective_start_idx,
          _internal_op: style_op,
          _new_style: resolved_style,
          _active_mapper_ref: active_mapper,
        });
        continue;
      }

      if (
        actual_doc_text === current_effective_new_text ||
        edit.target_text === current_effective_new_text
      ) {
        all_sub_edits.push({
          type: "modify",
          target_text: actual_doc_text,
          new_text: actual_doc_text,
          comment: edit.comment,
          _match_start_index: start_idx,
          _internal_op: "COMMENT_ONLY",
          _active_mapper_ref: active_mapper,
        });
        continue;
      }

      let overlaps_virtual_pipe = false;
      if (active_mapper) {
        overlaps_virtual_pipe = active_mapper.spans.some(
          (s: any) =>
            s.text === " | " &&
            (s.run === null || s.run === undefined) &&
            s.start < start_idx + match_len &&
            s.end > start_idx,
        );
      }

      if (overlaps_virtual_pipe) {
        const actual_cells = actual_doc_text.split("|");
        const new_cells = current_effective_new_text.split("|");

        if (actual_cells.length !== new_cells.length) {
          throw new BatchValidationError([
            `Target text spans ${actual_cells.length} table cells, but replacement provides ${new_cells.length}. To modify text without altering table structure (rows or columns), ensure the replacement contains the exact same number of '|' separators (e.g., replace with 'CellC | ' to empty the second cell).`
          ]);
        }

        if (actual_cells.length > 1) {
          const sub_edits: any[] = [];

          // actual_doc_text IS the document slice at
          // [start_idx, start_idx + len): per-cell offsets are exact
          // arithmetic over that slice — never a search of mapper.full_text,
          // which cannot distinguish repeated cell text and lands in the
          // wrong cell when the matched range starts inside a " | "
          // separator.
          let cell_start_in_target = 0;

          // Determine which cell receives the comment
          let target_comment_idx = 0;
          for (let idx = 0; idx < actual_cells.length; idx++) {
            if (actual_cells[idx].trim() !== new_cells[idx].trim()) {
              target_comment_idx = idx;
              break;
            }
          }

          for (let cell_idx = 0; cell_idx < actual_cells.length; cell_idx++) {
            const a_cell = actual_cells[cell_idx];
            const n_cell = new_cells[cell_idx];
            const a_clean = a_cell.trim();
            const n_clean = n_cell.trim();
            const actual_start =
              start_idx +
              cell_start_in_target +
              (a_clean ? a_cell.indexOf(a_clean) : 0);

            const should_attach_comment = (edit.comment !== null && edit.comment !== undefined) && (cell_idx === target_comment_idx);

            if (a_clean !== n_clean || should_attach_comment) {
              const cell_sub_edits = this._word_diff_sub_edits(
                a_clean,
                n_clean,
                actual_start,
                should_attach_comment ? edit.comment : null,
                true,
                active_mapper,
              );
              for (const se of cell_sub_edits) {
                se._original_target_text = edit.target_text;
                se._split_group_id = start_idx;
                sub_edits.push(se);
              }
            }

            cell_start_in_target += a_cell.length + 1; // +1 for the '|'
          }

          for (const sub of sub_edits) {
            all_sub_edits.push(sub);
          }
          continue;
        }
        // Exactly one "cell": the target merely brushes a separator (its
        // match range starts or ends inside " | ") without crossing into
        // another cell's text. That is an ordinary in-cell edit — fall
        // through to the standard resolution.
      }

      let has_markdown = false;
      if (edit.target_text && (edit.target_text.includes("**") || edit.target_text.includes("_"))) {
        has_markdown = true;
      }
      if (current_effective_new_text && (current_effective_new_text.includes("**") || current_effective_new_text.includes("_"))) {
        has_markdown = true;
      }

      let effective_op = "";
      let final_target = "";
      let final_new = "";
      let effective_start_idx = start_idx;

      if (current_effective_new_text.startsWith(actual_doc_text)) {
        effective_op = "INSERTION";
        final_new = current_effective_new_text.substring(actual_doc_text.length);
        effective_start_idx = start_idx + match_len;
      } else {
        const [prefix_len, suffix_len] = trim_common_context(
          actual_doc_text,
          current_effective_new_text,
        );
        const t_end = actual_doc_text.length - suffix_len;
        const n_end = current_effective_new_text.length - suffix_len;

        final_target = actual_doc_text.substring(prefix_len, t_end);
        final_new = current_effective_new_text.substring(prefix_len, n_end);
        effective_start_idx = start_idx + prefix_len;
      }

      if (has_markdown) {
        if (!final_target && final_new) {
          effective_op = "INSERTION";
        } else if (final_target && !final_new) {
          effective_op = "DELETION";
        } else if (final_target && final_new) {
          effective_op = "MODIFICATION";
        } else {
          all_sub_edits.push({
            type: "modify",
            target_text: final_target,
            new_text: final_new,
            comment: edit.comment,
            _match_start_index: effective_start_idx,
            _internal_op: "COMMENT_ONLY",
            _active_mapper_ref: active_mapper,
          });
          continue;
        }

        all_sub_edits.push({
          type: "modify",
          target_text: final_target,
          new_text: final_new,
          comment: edit.comment,
          _resolved_start_idx: effective_start_idx,
          _match_start_index: effective_start_idx,
          _internal_op: effective_op,
          _active_mapper_ref: active_mapper,
        });
        continue;
      }

      // Balanced multi-paragraph modification: the matched span crosses one or
      // more paragraph breaks and the replacement preserves the same number of
      // breaks. Apply it as one independent sub-edit per paragraph segment so
      // the structural \n\n breaks are left intact. Each sub-edit shares a
      // _split_group_id (the occurrence's start index) so the batch report
      // counts it as a single applied edit. Unbalanced cases (a genuine
      // paragraph merge or split) fall through to the single-span path and are
      // rejected by validate_edits.
      const target_segs = actual_doc_text.split("\n\n");
      const new_segs = current_effective_new_text.split("\n\n");
      if (
        actual_doc_text.includes("\n\n") &&
        target_segs.length === new_segs.length
      ) {
        const split_sub_edits: any[] = [];
        let seg_offset = start_idx;
        let comment_assigned = false;
        for (let k = 0; k < target_segs.length; k++) {
          const t_seg = target_segs[k];
          const n_seg = new_segs[k];
          if (t_seg !== n_seg) {
            const seg_comment =
              edit.comment && !comment_assigned ? edit.comment : null;
            const seg_sub_edits = this._word_diff_sub_edits(
              t_seg,
              n_seg,
              seg_offset,
              seg_comment,
              false,
              active_mapper,
            );
            if (seg_sub_edits.some((se) => se.comment !== null && se.comment !== undefined)) {
              comment_assigned = true;
            }
            for (const se of seg_sub_edits) {
              se._split_group_id = start_idx;
              split_sub_edits.push(se);
            }
          }
          // Advance past this segment plus its "\n\n" separator span.
          seg_offset += t_seg.length + 2;
        }
        if (split_sub_edits.length > 0) {
          for (const sub of split_sub_edits) all_sub_edits.push(sub);
          continue;
        }
      }

      // After trimming shared context, an edit whose target remainder is
      // EMPTY is a pure insertion with exactly one hunk. Resolve it
      // directly at the effective offset instead of word-diffing the full
      // strings: dmp's alignment can cross-match punctuation between the
      // shared context and the inserted text (pairing the period of "two."
      // with "marker.") and split the insertion apart.
      if (!final_target && final_new) {
        all_sub_edits.push({
          type: "modify",
          target_text: "",
          new_text: final_new,
          comment: edit.comment,
          _resolved_start_idx: effective_start_idx,
          _match_start_index: effective_start_idx,
          _internal_op: "INSERTION",
          _active_mapper_ref: active_mapper,
        });
        continue;
      }

      const sub_edits = this._word_diff_sub_edits(
        actual_doc_text,
        current_effective_new_text,
        start_idx,
        edit.comment,
        false,
        active_mapper,
      );
      for (const se of sub_edits) {
        se._split_group_id = start_idx;
        all_sub_edits.push(se);
      }
    }

    if (all_sub_edits.length === 0) return null;
    if (match_mode === "all" || all_sub_edits.length > 1) return all_sub_edits;
    return all_sub_edits[0];
  }

  /**
   * Split a <w:ins> so that everything up to and INCLUDING split_after stays in
   * a left <w:ins>, new_elem is placed between, and the remainder moves to a
   * right <w:ins> — all at the grandparent level. Used when revising another
   * author's pending insertion: the <w:del> stays nested in their <w:ins> while
   * our replacement <w:ins> lands as a sibling, so we never nest <w:ins> in
   * <w:ins>.
   */
  private _insert_and_split_ins(
    parent_ins: Element,
    anchor: Element,
    new_elem: Element,
    split_before: boolean = false,
  ) {
    const grandparent = parent_ins.parentNode as Element | null;
    if (!grandparent) return;
    // cloneNode(false) copies the attributes (author/id/date) onto both halves.
    // The split lands after `anchor` by default; with split_before the anchor
    // itself goes to the right half so new_elem ends up in front of it.
    const left = parent_ins.cloneNode(false) as Element;
    const right = parent_ins.cloneNode(false) as Element;
    let toRight = false;
    for (const kid of Array.from(parent_ins.childNodes)) {
      parent_ins.removeChild(kid);
      if (split_before && kid === anchor) toRight = true;
      if (!toRight) {
        left.appendChild(kid);
        if (kid === anchor) toRight = true;
      } else {
        right.appendChild(kid);
      }
    }
    if (left.childNodes.length > 0) grandparent.insertBefore(left, parent_ins);
    grandparent.insertBefore(new_elem, parent_ins);
    if (right.childNodes.length > 0)
      grandparent.insertBefore(right, parent_ins);
    grandparent.removeChild(parent_ins);
  }

  private _apply_single_edit_indexed(
    edit: any,
    orig_new: string | null,
    rebuild_map: boolean,
  ): boolean {
    let op = edit._internal_op;
    const active_mapper = edit._active_mapper_ref || this.mapper;
    const start_idx =
      edit._resolved_start_idx !== undefined &&
      edit._resolved_start_idx !== null
        ? edit._resolved_start_idx
        : edit._match_start_index || 0;
    const length = edit.target_text ? edit.target_text.length : 0;

    // Indexed edits (caller-supplied _match_start_index, e.g. straight from
    // generate_edits_from_text) bypass _pre_resolve_heuristic_edit — the only
    // place _internal_op is normally assigned. Without this fallback the
    // deletion sweep below still runs but no insertion branch does: a
    // replacement silently degrades to a pure tracked deletion and a pure
    // insertion fails. Mirrors the Python engine.
    if (op === undefined || op === null) {
      if (!edit.target_text && edit.new_text) {
        op = "INSERTION";
      } else if (edit.target_text && !edit.new_text) {
        op = "DELETION";
      } else {
        op = "MODIFICATION";
      }
    }

    // Explicit bold/italic markers in the edit make the markers
    // authoritative: inserted runs must not additionally inherit the replaced
    // span's emphasis (QA 2026-07-19 F-02). Keys on THIS resolved edit's
    // post-trim fields: identical markers on both sides were absorbed into
    // context (formatting unchanged — keep inheriting), and plain edits
    // fuzzy-matched onto styled text never receive marker hunks at all.
    const suppress_emphasis = this._edit_declares_emphasis(edit);

    if (op === "STYLE_ONLY" || op === "STYLE_AND_TEXT") {
      const [anchor_run, anchor_para] = active_mapper.get_insertion_anchor(
        start_idx,
        rebuild_map,
      );
      let target_para_el: Element | null = null;
      if (anchor_para) {
        target_para_el = anchor_para._element;
      } else if (anchor_run) {
        let walker: Element | null = anchor_run._element;
        while (walker && walker.tagName !== "w:p") {
          walker = walker.parentNode as Element | null;
        }
        target_para_el = walker;
      }

      if (target_para_el && edit._new_style) {
        this._set_paragraph_style(target_para_el, edit._new_style);
      }

      if (op === "STYLE_ONLY") {
        if (edit.comment) {
          const target_runs = active_mapper.find_target_runs_by_index(
            start_idx,
            length,
            rebuild_map,
          );
          if (target_runs.length > 0) {
            const first_el = target_runs[0]._element;
            const last_el = target_runs[target_runs.length - 1]._element;
            let start_p: Element | null = first_el;
            while (start_p && start_p.tagName !== "w:p")
              start_p = start_p.parentNode as Element;
            let end_p: Element | null = last_el;
            while (end_p && end_p.tagName !== "w:p")
              end_p = end_p.parentNode as Element;
            if (start_p && end_p) {
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
              const first_anchor = ascend_to_paragraph_child(first_el, start_p);
              const last_anchor = ascend_to_paragraph_child(last_el, end_p);
              if (start_p === end_p) {
                this._attach_comment(
                  start_p,
                  first_anchor,
                  last_anchor,
                  edit.comment,
                );
              } else {
                this._attach_comment_spanning(
                  start_p,
                  first_anchor,
                  end_p,
                  last_anchor,
                  edit.comment,
                );
              }
            }
          }
        }
        return true;
      }

      if (edit.target_text && edit.new_text) {
        op = "MODIFICATION";
      } else if (!edit.target_text && edit.new_text) {
        op = "INSERTION";
      } else if (edit.target_text && !edit.new_text) {
        op = "DELETION";
      } else {
        op = "COMMENT_ONLY";
      }
    }

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
      let final_new_text = edit.new_text || "";

      // A MACHINE-PINNED pure insertion (diff/text round-trip output:
      // authored with an empty target and no parent edit) positioned in the
      // separator gap between the body and a following part anchors to the
      // end of the BODY with forced new-paragraph semantics — anchoring on
      // the next part's first paragraph writes the new final body paragraph
      // into word/footer1.xml. Insertions DERIVED from a target-anchored
      // edit (parent ref set — e.g. prepending "DRAFT " to "FOOTER MARKER")
      // keep the user's chosen anchor: their context names the part they
      // meant.
      let boundary_anchor: TextSpan | null = null;
      const boundary =
        typeof (active_mapper as any).part_boundary_at === "function"
          ? active_mapper.part_boundary_at(start_idx)
          : null;
      const is_machine_pure_insertion =
        !edit.target_text &&
        (edit._parent_edit_ref === undefined || edit._parent_edit_ref === null);
      if (boundary !== null && is_machine_pure_insertion) {
        const [prev_i, next_i] = boundary;
        const prev_kind = active_mapper.part_kind_of(prev_i);
        const next_kind = active_mapper.part_kind_of(next_i);
        if (prev_kind === "body" && next_kind !== "body") {
          const real_before = active_mapper.spans.filter(
            (s: TextSpan) => s.run !== null && s.part_index === prev_i,
          );
          if (real_before.length > 0) {
            boundary_anchor = real_before[real_before.length - 1];
          }
        }
      }

      let anchor_run: Run | null;
      let anchor_para: Paragraph | null;
      if (boundary_anchor !== null) {
        anchor_run = boundary_anchor.run;
        anchor_para = boundary_anchor.paragraph;
        if (!final_new_text.startsWith("\n")) {
          final_new_text = "\n\n" + final_new_text;
        }
      } else {
        [anchor_run, anchor_para] = active_mapper.get_insertion_anchor(
          start_idx,
          rebuild_map,
        );
      }
      if (!anchor_run && !anchor_para) return false;

      // QA 2026-07-18 C2 (apply-level backstop, pinned edits bypass
      // validate_edits): refuse insertions that would write row-shaped pipe
      // text into a table cell.
      if (
        RedlineEngine._introduces_table_row_text(
          active_mapper,
          start_idx,
          1,
          "",
          final_new_text,
        )
      ) {
        return false;
      }

      // BUG-23-3: a prefix insertion whose new_text ends in a paragraph break
      // (e.g. "Summary\n\n" inserted before "Conclusion") must become a NEW
      // paragraph placed BEFORE the anchor paragraph, not inline text merged
      // into a neighbouring paragraph. _track_insert_multiline drops the
      // trailing break and inlines the remainder, which both loses the
      // paragraph boundary and mis-orders the content. Handle this case here.
      // (Skipped when the C1 boundary re-anchor above took over the anchor.)
      const _bug233_new = final_new_text;
      const _bug233_trailing_break =
        boundary_anchor === null && /\n\s*$/.test(_bug233_new);
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
        const lines = _bug233_new
          .split(/[\r\n]+/)
          .filter((l: string) => l !== "");
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
            if (existing_pPr) {
              new_p.appendChild(this._clone_pPr_scrubbing_headings(existing_pPr));
            }
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
            suppress_emphasis,
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
        final_new_text,
        anchor_run,
        anchor_para,
        ins_id!,
        null,
        suppress_emphasis,
        // Paragraph-start insertions attach BEFORE the anchor (see
        // before_anchor below): the suffix relocation must know.
        start_idx === 0,
      );

      if (!result.first_node) return false;

      // Place the inline <w:ins> (or block-mode first paragraph) into the DOM.
      // Block-mode first_node is already a freshly-inserted <w:p>; only the
      // inline case needs DOM splicing here.
      const is_inline_first = result.first_node.tagName === "w:ins";
      if (is_inline_first) {
        if (anchor_run) {
          let anchor_el: Element = anchor_run._element;
          let anchor_parent = anchor_el.parentNode as Element | null;
          // A tracked-deleted anchor (run inside <w:del>) cannot host the
          // new <w:ins> as a child — an insertion nested inside a deletion
          // is invalid revision XML. Lift the anchor to the <w:del> wrapper
          // so the insert lands beside the whole block (mirrors the Python
          // engine).
          if (anchor_parent && anchor_parent.tagName === "w:del") {
            anchor_el = anchor_parent;
            anchor_parent = anchor_el.parentNode as Element | null;
          }
          // get_insertion_anchor(0) resolves to the document's FIRST run: the
          // insertion point precedes it, so the new <w:ins> must land before
          // the anchor, not after (mirrors the Python engine's insert_before
          // path).
          const before_anchor = start_idx === 0;
          if (anchor_parent && anchor_parent.tagName === "w:ins") {
            // Inserting inside another author's pending <w:ins>: split it so our
            // new <w:ins> lands as a sibling right next to the anchor run, never
            // <w:ins> nested in <w:ins> (mirrors the MODIFICATION path and the
            // Python engine).
            this._insert_and_split_ins(
              anchor_parent,
              anchor_el,
              result.first_node,
              before_anchor,
            );
          } else if (before_anchor && anchor_parent) {
            anchor_parent.insertBefore(result.first_node, anchor_el);
          } else {
            insertAfter(result.first_node, anchor_el);
          }
        } else if (anchor_para) {
          // Paragraph-anchored insertion: the anchor resolves to a paragraph
          // (not a run) for zero-width paragraph-start spans — e.g. index 0 of
          // the document. The insertion point is the START of the paragraph
          // content, so land right after pPr, mirroring the Python engine;
          // appendChild would drop the text at the paragraph's END.
          const para_el = anchor_para._element;
          let ref: Node | null = para_el.firstChild;
          while (ref && (ref as Element).tagName === "w:pPr") {
            ref = ref.nextSibling;
          }
          para_el.insertBefore(result.first_node, ref);
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
              first_anchor_target =
                findAllDescendants(result.first_node, "w:ins")[0] ||
                result.first_node;
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
              first_anchor_target =
                findAllDescendants(result.first_node, "w:ins")[0] ||
                result.first_node;
            }
            const anchor = ascend_to_paragraph_child(
              first_anchor_target,
              host_p,
            );
            this._attach_comment(host_p, anchor, anchor, edit.comment);
          }
        }
      }
      return true;
    }

    // QA 2026-07-18 C1 (apply-level backstop, pinned edits bypass
    // validate_edits): a modification/deletion may never mutate real text
    // from two different OPC parts in one span. Single-part documents skip
    // the scan.
    if (
      (op === "DELETION" || op === "MODIFICATION") &&
      length &&
      active_mapper.part_ranges.filter((r: [number, number, string]) => r[1] > r[0]).length > 1
    ) {
      const crossed_parts = new Set<number>();
      for (const s of active_mapper.spans) {
        if (s.run !== null && s.end > start_idx && s.start < start_idx + length) {
          crossed_parts.add(s.part_index);
        }
      }
      if (crossed_parts.size > 1) {
        console.error(
          `Refusing edit that spans OPC part boundary (start=${start_idx}, parts=${Array.from(crossed_parts).sort().join(",")})`,
        );
        return false;
      }
    }

    // DELETION / MODIFICATION
    const target_runs = active_mapper.find_target_runs_by_index(
      start_idx,
      length,
      rebuild_map,
    );
    const virtual_spans = active_mapper.get_virtual_spans_in_range(
      start_idx,
      length,
    );

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
        // The insertion physically follows the deletion block; the style
        // run was detached when the deletion cloned it into <w:del>.
        last_del,
        suppress_emphasis,
      );

      if (result.first_node) {
        const is_inline_first = result.first_node.tagName === "w:ins";
        if (is_inline_first) {
          const del_parent = last_del!.parentNode as Element | null;
          if (del_parent && del_parent.tagName === "w:ins") {
            // Revising another author's pending insertion: keep the <w:del>
            // nested in their <w:ins> and splice our new <w:ins> in right after
            // it by splitting their <w:ins>, so we never nest <w:ins> in
            // <w:ins>.
            this._insert_and_split_ins(
              del_parent,
              last_del!,
              result.first_node,
            );
          } else {
            // Inline: place the first <w:ins> immediately after last_del.
            insertAfter(result.first_node, last_del!);
          }
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
      if (
        op === "MODIFICATION" &&
        target_runs.length === 0 &&
        virtual_spans.length > 0 &&
        edit.new_text
      ) {
        const first_span = virtual_spans[0];
        if (first_span.paragraph) {
          const p1_el = first_span.paragraph._element;
          const last_runs = findAllDescendants(p1_el, "w:r");
          const anchor =
            last_runs.length > 0
              ? new Run(last_runs[last_runs.length - 1], first_span.paragraph)
              : null;

          const result = this._track_insert_multiline(
            edit.new_text,
            anchor,
            first_span.paragraph,
            ins_id!,
            null,
            suppress_emphasis,
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
            // Decide the merged container's properties BEFORE p2's children
            // move in: when p1 keeps no visible content (a FULL paragraph
            // deletion), the only surviving text is p2's — the merged
            // paragraph must carry p2's properties (style, numbering).
            // Keeping p1's restyled the following paragraph: deleting a
            // heading turned the next body paragraph into a heading,
            // deleting a plain paragraph before a list item stripped the
            // item's numbering (QA 2026-07-19 ADEU-QA-002 B).
            const p1_fully_deleted =
              !this._paragraph_has_visible_content(p1_element);

            let pPr = findChild(p1_element, "w:pPr");
            if (p1_fully_deleted) {
              const p2_pPr = findChild(p2_element, "w:pPr");
              const adopted = (
                p2_pPr
                  ? p2_pPr.cloneNode(true)
                  : p1_element.ownerDocument!.createElement("w:pPr")
              ) as Element;
              // Section properties belong to p1's position in the document
              // flow, never to p2's styling — carry them over so a section
              // boundary is not destroyed.
              if (pPr) {
                const sect = findChild(pPr, "w:sectPr");
                if (sect && !findChild(adopted, "w:sectPr")) {
                  adopted.appendChild(sect.cloneNode(true));
                }
                p1_element.removeChild(pPr);
              }
              p1_element.insertBefore(
                adopted,
                p1_element.firstChild as Node | null,
              );
              pPr = adopted;
            }
            if (!pPr) {
              pPr = p1_element.ownerDocument!.createElement("w:pPr") as Element;
              p1_element.insertBefore(
                pPr,
                p1_element.firstChild as Node | null,
              );
            }
            let rPr = findChild(pPr!, "w:rPr");
            if (!rPr) {
              rPr = p1_element.ownerDocument!.createElement("w:rPr") as Element;
              pPr!.appendChild(rPr);
            }
            if (!findChild(rPr!, "w:del")) {
              const del_mark = this._create_track_change_tag("w:del");
              rPr!.appendChild(del_mark);
            }

            const children = Array.from(p2_element.childNodes);
            for (const child of children) {
              if (
                child.nodeType === 1 &&
                (child as Element).tagName === "w:pPr"
              ) {
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
      const has_visible = this._paragraph_has_visible_content(p_elem);

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
