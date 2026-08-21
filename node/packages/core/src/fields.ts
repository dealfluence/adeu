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
import {
  DocumentProtection,
  readDocumentProtection,
} from "./utils/protection.js";
import { clean_breadcrumb, offset_to_page } from "./outline.js";
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

/**
 * `w:documentProtection/@w:edit` -> the BANNER's word (spec-projection §7).
 *
 * Deliberately not `describeProtection()`: that phrasing serves gate errors
 * and A3.4 pins "read-only, enforced" as substrings of one. The banner is a
 * different surface with its own frozen wording, so the parse is shared and
 * only the rendering differs.
 */
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

/** The banner/ledger phrasing for a parsed protection state (spec §7). */
export function protectionLabel(p: DocumentProtection): string {
  if (p.edit === null) return "none";
  const word = PROTECTION_WORDS[p.edit] ?? p.edit;
  return p.enforced ? `${word} (enforced)` : word;
}

/**
 * Re-exported so the ledger's callers reach ONE parser. CC-4 owns the reader
 * (it runs on every engine load for the write gates); CC-2 owns the wording.
 */
export { readDocumentProtection };
export type { DocumentProtection };

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

// ---------------------------------------------------------------------------
// Collection
// ---------------------------------------------------------------------------

const ANCHOR_SCAN_RE = /\{#(\/?)cc:(\d+)(?: [^}]*)?\}/g;

/**
 * `ordinal -> [openStart, openEnd, closeStart]` in ONE pass.
 *
 * Searching per control instead cost 8.8 seconds on FedRAMP rev4 — twenty
 * times the cost of the whole projection — because each of 5,007 controls
 * scanned 600 KB of text. The ledger is a read-path feature; it must not be
 * the slowest thing in the read.
 */
function scanAnchors(rawText: string): Map<number, [number, number, number]> {
  const opens = new Map<number, [number, number]>();
  const closes = new Map<number, number>();
  ANCHOR_SCAN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ANCHOR_SCAN_RE.exec(rawText)) !== null) {
    const ordinal = Number(m[2]);
    if (m[1]) {
      if (!closes.has(ordinal)) closes.set(ordinal, m.index);
    } else if (!opens.has(ordinal)) {
      opens.set(ordinal, [m.index, m.index + m[0].length]);
    }
  }
  const bounds = new Map<number, [number, number, number]>();
  for (const [ordinal, [openStart, openEnd]] of opens) {
    const close = closes.get(ordinal);
    if (close !== undefined && close >= openEnd)
      bounds.set(ordinal, [openStart, openEnd, close]);
  }
  return bounds;
}

/**
 * Answers "which heading path contains this offset?" in O(log H).
 *
 * `heading_path_at` re-splits the whole projection on every call — fine for a
 * handful of search hits, quadratic for a ledger with thousands of rows. This
 * precomputes every heading's full breadcrumb once and binary-searches it; a
 * test pins that the two agree.
 */
export class HeadingIndex {
  private starts: number[] = [];
  private paths: string[] = [];

  constructor(text: string) {
    const stack: Array<{ level: number; path: string[] }> = [];
    let offset = 0;
    for (const line of text.split("\n")) {
      const m = line.match(/^(#{1,6})\s+(.*)/);
      if (m) {
        const level = m[1].length;
        let heading = clean_breadcrumb(m[2]);
        if (heading.length > 80) heading = heading.slice(0, 80) + "...";
        while (stack.length > 0 && stack[stack.length - 1].level >= level)
          stack.pop();
        const path = (stack.length > 0 ? stack[stack.length - 1].path : []).concat([
          heading,
        ]);
        stack.push({ level, path });
        this.starts.push(offset);
        this.paths.push(path.join(" > "));
      }
      offset += line.length + 1;
    }
  }

  pathAt(offset: number): string {
    if (this.starts.length === 0) return "";
    // heading_path_at scans back from the END of the line containing the
    // offset, so a heading ON that line counts as containing it.
    let lo = 0;
    let hi = this.starts.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.starts[mid] <= offset) {
        found = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return found >= 0 ? this.paths[found] : "";
  }
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

function hasText(sdtElement: any): boolean {
  const content = findChild(sdtElement, QN_W_SDTCONTENT);
  if (!content) return false;
  return findAllDescendants(content, "w:t").some(
    (t: any) => (t.textContent ?? "").trim().length > 0,
  );
}

/**
 * `[total, empty, locked, bound]` from the DOM alone.
 *
 * The banner and the appendix summary need only these four numbers, and paying
 * for the full ledger to get them is what made this expensive: on FedRAMP rev4
 * the appendix would have carried 115ms of value previews, breadcrumbs and
 * page lookups that nothing rendered. This walks the ordinal pre-pass and
 * stops.
 *
 * `empty` is derived structurally here (placeholder shown, or no text in the
 * content) rather than from the projection. A test pins that it agrees with
 * the ledger's own count, because a banner that disagrees with the ledger it
 * advertises is worse than no banner.
 */
export function fieldSummary(doc: any): [number, number, number, number] {
  const infos = assignOrdinals(
    Array.from(iter_document_parts_with_kind(doc)).map(([part]) =>
      partElement(part),
    ),
  );
  let total = 0;
  let empty = 0;
  let locked = 0;
  let bound = 0;
  for (const info of infos.values()) {
    total += 1;
    if (CONTAINER_CLASSES.has(info.cls) || info.cls === "checkbox") {
      // containers and checkboxes are never "empty" for the count
    } else if (info.showingPlaceholder || !hasText(info.element)) {
      empty += 1;
    }
    if (info.cls === "group" || info.contentLocked) locked += 1;
    if (info.bound) bound += 1;
  }
  return [total, empty, locked, bound];
}

/** The full-view banner, computed without projecting values (spec §7). */
export function bannerForDocument(doc: any, hint = ""): string | null {
  const counts = fieldSummary(doc);
  const protection = readDocumentProtection(doc);
  if (counts[0] === 0 && protection.edit === null) return null;
  const line = `> **Protection:** ${protectionLabel(protection)} \u00b7 **Fields:** ${summaryText(counts)}`;
  return hint ? `${line}${hint}` : line;
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

  const anchors = scanAnchors(rawText);
  const headings = new HeadingIndex(rawText);

  const entries: FieldEntry[] = [];
  let lastKnownOffset = 0;
  for (const info of ordered) {
    const bounds = anchors.get(info.ordinal);

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
    const crumb = headings.pathAt(offset);

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

function summaryText(counts: readonly [number, number, number, number]): string {
  const [total, empty, locked, bound] = counts;
  if (total === 0) return "no content controls";
  return `${total} content controls \u2014 ${empty} empty \u00b7 ${locked} locked \u00b7 ${bound} bound`;
}

function fieldsSummary(entries: readonly FieldEntry[]): string {
  return summaryText(summaryCounts(entries));
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
  if (entries.length === 0 && protection.edit === null) return null;
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
  counts: readonly [number, number, number, number],
  protection: DocumentProtection,
  hint = "",
): string[] {
  if (counts[0] === 0 && protection.edit === null) return [];
  const lines = [
    "## Content Controls",
    "",
    `Protection: ${protectionLabel(protection)} \u00b7 ${summaryText(counts)}`,
  ];
  if (hint) lines.push(hint);
  return lines;
}
