/**
 * Content-control discovery: the fields ledger and the protection banner.
 *
 * Spec: `specs/content-controls/spec-fields-ledger.md` (frozen v1) plus
 * `spec-projection.md` §7 for the banner. Acceptance: A2, and A1.9 for the
 * banner.
 *
 * Twin of `python/src/adeu/fields.py`. The two MUST render identical text —
 * spec §7 asks callers to parse the line format, so this is an output
 * contract, and `cc_fields_ledger.test.ts` compares both engines against the
 * same frozen golden.
 *
 * The ledger reads the *raw projection*, not the DOM, for every value it
 * shows. A control's rendered value has already survived table flattening (a
 * row-level control's value is the markdown row `A | B`), CriticMarkup and the
 * placeholder-bubble rules; re-deriving it from `w:t` would produce a ledger
 * that quietly disagrees with the document text the agent is editing.
 */
import { findChild, findAllDescendants } from "./docx/dom.js";
import { findDescendantsByLocalName } from "./sanitize/transforms.js";
import { clean_breadcrumb, heading_path_at, offset_to_page } from "./outline.js";
import {
  QN_W_SDT,
  QN_W_SDTCONTENT,
  SdtInfo,
  assignOrdinals,
  partElement,
} from "./utils/content-controls.js";
import { iter_document_parts_with_kind } from "./utils/docx.js";

/**
 * Ledger lines per response (spec §4). FedRAMP rev4 projects 5,007 controls;
 * the cap keeps one response inside the same budget philosophy the changes
 * ledger already applies at 300 entries.
 */
export const FIELDS_PAGE_SIZE = 100;

/** Value/placeholder previews (spec §3 segments 7 and 8). */
export const PREVIEW_CAP = 80;

/** Dropdown/combobox options listed before the overflow marker (spec §3.9). */
export const OPTIONS_SHOWN = 8;

/** `w:documentProtection/@w:edit` -> the banner's word (spec-projection §7). */
const PROTECTION_WORDS: Record<string, string> = {
  readOnly: "read-only",
  forms: "fill-in-forms only",
  comments: "comments only",
  trackedChanges: "tracked-changes only",
};

/**
 * Internal class name -> the ledger's class word (spec §3 segment 2). Only
 * `repeating-item` differs; the rest are already the spec's vocabulary.
 */
const CLASS_WORDS: Record<string, string> = { "repeating-item": "item" };

/**
 * Classes that describe their EXTENT instead of previewing a value. A group's
 * value would be every nested paragraph, which is the document, not a preview.
 */
const CONTAINER_CLASSES: ReadonlySet<string> = new Set([
  "group",
  "repeating",
  "repeating-item",
]);

export interface DocumentProtection {
  mode: string;
  enforced: boolean;
}

export function protectionLabel(p: DocumentProtection): string {
  if (p.mode === "none") return "none";
  return p.enforced ? `${p.mode} (enforced)` : p.mode;
}

export interface FieldEntry {
  ordinal: number;
  cls_word: string;
  alias: string | null;
  tag: string | null;
  page: number;
  heading_path: string;
  container_kind: string | null; // "table cell" | "table row"
  parent_ordinal: number | null;
  states: string[];
  value: string | null;
  checkbox_state: string | null;
  placeholder: string | null;
  options: string[];
  date_format: string | null;
  extent: string | null;
  empty: boolean;
  locked: boolean;
  bound: boolean;
}

// ---------------------------------------------------------------------------
// Protection
// ---------------------------------------------------------------------------

/** Read `w:documentProtection` from `word/settings.xml`. */
export function readDocumentProtection(doc: any): DocumentProtection {
  const none: DocumentProtection = { mode: "none", enforced: false };
  const settingsPart = doc?.pkg?.getPartByPath?.("word/settings.xml");
  if (!settingsPart?._element) return none;

  // Local-name matching, mirroring extract_document_settings_warnings: the
  // settings part comes from many Word versions and the prefix is not
  // guaranteed to be `w`.
  const nodes = findDescendantsByLocalName(
    settingsPart._element,
    "documentProtection",
  );
  const node = nodes.length > 0 ? nodes[0] : null;
  if (!node) return none;

  const edit = node.getAttribute("w:edit");
  // An enforcement flag with no edit mode protects nothing in Word, and
  // reporting "none (enforced)" would be a contradiction the agent has to
  // resolve. Treat it as unprotected.
  if (!edit || !(edit in PROTECTION_WORDS)) return none;
  const enforcement = node.getAttribute("w:enforcement");
  return {
    mode: PROTECTION_WORDS[edit],
    enforced: enforcement === "1" || enforcement === "true",
  };
}

// ---------------------------------------------------------------------------
// Collection
// ---------------------------------------------------------------------------

function anchorBounds(
  rawText: string,
  ordinal: number,
): [number, number, number] | null {
  const open = new RegExp(`\\{#cc:${ordinal}(?: [^}]*)?\\}`).exec(rawText);
  if (!open) return null;
  const close = new RegExp(`\\{#/cc:${ordinal}\\}`).exec(rawText);
  if (!close || close.index < open.index + open[0].length) return null;
  return [open.index, open.index + open[0].length, close.index];
}

/** Whitespace-collapsed, anchor-free, markup-free preview (spec §3.7). */
function preview(text: string, cap: number = PREVIEW_CAP): string {
  // clean_breadcrumb is the projection's existing "render this fragment as
  // plain prose" rule: it unwraps insertions, drops deletions and bubbles,
  // strips emphasis and removes {#…} tokens — including the anchors of any
  // nested control, which a container's span would otherwise carry.
  const collapsed = clean_breadcrumb(text).replace(/\s+/g, " ").trim();
  return collapsed.length > cap ? collapsed.slice(0, cap) + "\u2026" : collapsed;
}

function childElements(parent: any): any[] {
  if (!parent) return [];
  const out: any[] = [];
  for (let n = parent.firstChild; n; n = n.nextSibling) {
    if (n.nodeType === 1) out.push(n);
  }
  return out;
}

function tagOf(el: any): string {
  return el?.tagName ?? el?.nodeName ?? "";
}

function blockChildren(sdtElement: any): any[] {
  const content = findChild(sdtElement, QN_W_SDTCONTENT);
  if (!content) return [];
  return childElements(content).filter((c) => {
    const t = tagOf(c);
    return t === "w:p" || t === "w:tbl";
  });
}

function directChildSdts(sdtElement: any): any[] {
  const content = findChild(sdtElement, QN_W_SDTCONTENT);
  if (!content) return [];
  return childElements(content).filter((c) => tagOf(c) === QN_W_SDT);
}

function nestedSdtCount(sdtElement: any): number {
  const content = findChild(sdtElement, QN_W_SDTCONTENT);
  if (!content) return 0;
  return findAllDescendants(content, QN_W_SDT).length;
}

function plural(count: number, word: string): string {
  return count === 1 ? `${count} ${word}` : `${count} ${word}s`;
}

/** Spec §3 segment 11. */
function extentFor(info: SdtInfo): string | null {
  if (info.cls === "group") {
    return `wraps ${plural(blockChildren(info.element).length, "block")}, ${plural(
      nestedSdtCount(info.element),
      "nested field",
    )}`;
  }
  if (info.cls === "repeating") {
    return plural(directChildSdts(info.element).length, "item");
  }
  if (info.cls === "repeating-item") {
    return `wraps ${plural(blockChildren(info.element).length, "block")}`;
  }
  return null;
}

/**
 * `table row` / `table cell` for row- and cell-level controls.
 *
 * The inverse of `wrappingSdt`: rather than asking a row which control
 * encloses it, ask a control what it encloses.
 */
function containerKind(info: SdtInfo): string | null {
  const content = findChild(info.element, QN_W_SDTCONTENT);
  if (!content) return null;
  const first = childElements(content)[0];
  if (!first) return null;
  const t = tagOf(first);
  if (t === "w:tr") return "table row";
  if (t === "w:tc") return "table cell";
  return null;
}

/** Spec §3 segment 6 — upper-case state tokens, in the spec's order. */
function statesFor(info: SdtInfo, empty: boolean): string[] {
  const states: string[] = [];
  if (empty) states.push("EMPTY");
  // Order is the spec's: contents, then group, then no-delete. The fixture
  // pins the precedence — its group carries a bare `sdtLocked`, and the golden
  // calls it LOCKED (group), not LOCKED (no-delete).
  if (info.contentLocked) states.push("LOCKED (contents)");
  else if (info.cls === "group") states.push("LOCKED (group)");
  else if (info.deleteLocked) states.push("LOCKED (no-delete)");
  if (info.bound) states.push(`BOUND \u2192 ${info.bindingXpath ?? ""}`.trimEnd());
  if (info.temporary) states.push("TEMPORARY");
  return states;
}

/** Build every ledger row for `doc`, in ordinal order. */
export function collectFields(
  doc: any,
  rawText: string,
  pageOffsets?: number[] | null,
): FieldEntry[] {
  const infos = assignOrdinals(
    Array.from(iter_document_parts_with_kind(doc)).map(([part]) =>
      partElement(part),
    ),
  );
  const ordered = Array.from(infos.values()).sort((a, b) => a.ordinal - b.ordinal);

  // Nearest enclosing control, for the `in CC:<M>` segment. Walking up from
  // each control and looking the ancestor up in the SAME ordinal map keeps the
  // relation consistent with the numbering by construction.
  const parentOrdinal = (info: SdtInfo): number | null => {
    let node = info.element?.parentNode ?? null;
    while (node) {
      if (tagOf(node) === QN_W_SDT) {
        const parent = infos.get(node);
        if (parent) return parent.ordinal;
      }
      node = node.parentNode ?? null;
    }
    return null;
  };

  const entries: FieldEntry[] = [];
  let lastKnownOffset = 0;
  for (const info of ordered) {
    const bounds = anchorBounds(rawText, info.ordinal);

    // Location. An anchored control reports its own offset exactly. An
    // un-anchored one (checkbox, picture, building block, repeating section
    // and its items — spec §1) has no token to find, so it inherits the last
    // offset established in document order. Ordinals ARE document order, so
    // this is monotone and never reports a control before its predecessor; it
    // is an approximation only in that an un-anchored control sitting exactly
    // on a page boundary can be attributed to the page its predecessor ended
    // on.
    if (bounds) lastKnownOffset = bounds[0];
    const offset = lastKnownOffset;

    const page = pageOffsets ? offset_to_page(offset, pageOffsets) : 1;
    const crumb = rawText ? heading_path_at(offset, rawText) : "";

    let value: string | null = null;
    let checkboxState: string | null = null;
    let placeholder: string | null = null;
    let empty = info.showingPlaceholder;

    if (info.cls === "checkbox") {
      // Spec §3.7: checkboxes render their state where a value would go.
      checkboxState = info.checked ? "checked" : "unchecked";
    } else if (CONTAINER_CLASSES.has(info.cls)) {
      // extent instead of a value
    } else if (bounds) {
      const p = preview(rawText.slice(bounds[1], bounds[2]));
      if (p) value = p;
      else empty = true;
    }

    if (empty && info.placeholderText) placeholder = preview(info.placeholderText);

    entries.push({
      ordinal: info.ordinal,
      cls_word: CLASS_WORDS[info.cls] ?? info.cls,
      alias: info.alias,
      tag: info.tag,
      page,
      heading_path: crumb,
      container_kind: containerKind(info),
      parent_ordinal: parentOrdinal(info),
      states: statesFor(info, empty),
      value,
      checkbox_state: checkboxState,
      placeholder,
      options: info.options.map(([display]) => display),
      date_format: info.dateFormat,
      extent: extentFor(info),
      empty,
      locked: info.cls === "group" || info.contentLocked,
      bound: info.bound,
    });
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/**
 * `[total, empty, locked, bound]` — the banner/header counts (spec §7).
 *
 * `locked` is content-locked leaves plus group containers; a bare `sdtLocked`
 * forbids deleting the control but leaves its contents editable, so it is a
 * ledger detail and not a lock for counting purposes.
 */
export function summaryCounts(
  entries: readonly FieldEntry[],
): [number, number, number, number] {
  return [
    entries.length,
    entries.filter((e) => e.empty).length,
    entries.filter((e) => e.locked).length,
    entries.filter((e) => e.bound).length,
  ];
}

function fieldsSummary(entries: readonly FieldEntry[]): string {
  const [total, empty, locked, bound] = summaryCounts(entries);
  if (total === 0) return "no content controls";
  return `${total} content controls \u2014 ${empty} empty \u00b7 ${locked} locked \u00b7 ${bound} bound`;
}

/**
 * The full-view banner line (spec-projection §7), or null when unwarranted.
 *
 * A plain document — no controls, no protection — gains zero noise. That is
 * the rule that keeps this from taxing every ordinary read.
 */
export function renderBanner(
  entries: readonly FieldEntry[],
  protection: DocumentProtection,
  hint = "",
): string | null {
  if (entries.length === 0 && protection.mode === "none") return null;
  const line = `> **Protection:** ${protectionLabel(protection)} \u00b7 **Fields:** ${fieldsSummary(entries)}`;
  return hint ? `${line}${hint}` : line;
}

/** The `mode="fields"` body (spec §2-§4). */
export function renderLedger(
  basename: string,
  entries: readonly FieldEntry[],
  protection: DocumentProtection,
  offset = 0,
  pageSize: number = FIELDS_PAGE_SIZE,
): string {
  const header = [
    `# Fields: ${basename}`,
    `Protection: ${protectionLabel(protection)} \u00b7 ${fieldsSummary(entries)}`,
  ];
  if (entries.length === 0) {
    return [...header, "", "No content controls."].join("\n");
  }

  const total = entries.length;
  const start = Math.max(0, Math.min(offset, total));
  const window = entries.slice(start, start + pageSize);
  const width = Math.max(...entries.map((e) => `CC:${e.ordinal}`.length));
  const lines = window.map((e) => renderLine(e, width));

  const remaining = total - (start + window.length);
  if (remaining > 0) {
    const nextOffset = start + window.length;
    lines.push(
      `\u2026 ${remaining} more \u2014 pass fields_offset=${nextOffset} to continue.`,
    );
  }
  return [...header, "", ...lines].join("\n");
}

/** One ledger line. The format is an output contract — see spec §3. */
export function renderLine(entry: FieldEntry, width: number): string {
  let head = `CC:${entry.ordinal}`.padEnd(width) + "  " + entry.cls_word;

  const nameParts: string[] = [];
  if (entry.alias) nameParts.push(`"${entry.alias}"`);
  if (entry.tag) nameParts.push(`(tag: ${entry.tag})`);
  // Two spaces between the class word and the name group; an anonymous control
  // shows neither empty quotes nor an empty tag (A2.5).
  if (nameParts.length > 0) head += "  " + nameParts.join(" ");

  const segments: string[] = [
    `p${entry.page}` + (entry.heading_path ? ` \u00b7 ${entry.heading_path}` : ""),
  ];
  if (entry.container_kind) segments.push(entry.container_kind);
  if (entry.parent_ordinal !== null) segments.push(`in CC:${entry.parent_ordinal}`);
  segments.push(...entry.states);
  if (entry.checkbox_state) segments.push(entry.checkbox_state);
  else if (entry.value !== null) segments.push(`value: "${entry.value}"`);
  if (entry.placeholder) segments.push(`placeholder: "${entry.placeholder}"`);
  if (entry.options.length > 0) {
    const shown = entry.options.slice(0, OPTIONS_SHOWN);
    let rendered = shown.join(" | ");
    const extra = entry.options.length - shown.length;
    if (extra > 0) rendered += ` | \u2026 (+${extra} more)`;
    segments.push(`options: ${rendered}`);
  }
  if (entry.date_format) segments.push(`format: ${entry.date_format}`);
  if (entry.extent) segments.push(entry.extent);

  return head + segments.map((s) => ` \u2014 ${s}`).join("");
}

/**
 * The appendix's `## Content Controls` block (spec §5).
 *
 * Header lines only: the full ledger never renders here, because the appendix
 * is bounded and a 5,007-line ledger would swallow it.
 */
export function renderAppendixSection(
  entries: readonly FieldEntry[],
  protection: DocumentProtection,
  hint = "",
): string[] {
  if (entries.length === 0 && protection.mode === "none") return [];
  const lines = [
    "## Content Controls",
    "",
    `Protection: ${protectionLabel(protection)} \u00b7 ${fieldsSummary(entries)}`,
  ];
  if (hint) lines.push(hint);
  return lines;
}
