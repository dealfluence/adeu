/**
 * Content-control (`w:sdt`) classification and ordinal assignment.
 *
 * Twin of `python/src/adeu/utils/content_controls.py` — every rule here must
 * hold identically in both engines (Virtual Text contract).
 *
 * Spec: `specs/content-controls/spec-projection.md` §1-§5.
 *
 * This lives in its own module rather than in `utils/docx.ts` for two reasons:
 * `utils/docx.ts` is the most contended file in the tree (both agents touch
 * it), and keeping the classification rules in one small pair of files makes
 * the python/node diff reviewable by eye — which is how the twins are kept
 * honest.
 *
 * Note the namespace handling difference from the python twin: this engine's
 * DOM uses PREFIXED tag names (`docx/dom.ts` defines `qn` as identity), not
 * Clark notation, so probes are spelled `"w14:checkbox"` directly. The python
 * side has to build `{uri}local` strings because lxml resolves prefixes.
 */

import { findChild, findAllDescendants } from "../docx/dom.js";

export const QN_W_SDT = "w:sdt";
export const QN_W_SDTPR = "w:sdtPr";
export const QN_W_SDTCONTENT = "w:sdtContent";

export type SdtClass =
  | "checkbox"
  | "dropdown"
  | "combobox"
  | "date"
  | "picture"
  | "building-block"
  | "group"
  | "repeating"
  | "repeating-item"
  | "text"
  | "richtext";

/**
 * Classification probes, in the order spec-projection.md §1 lists them.
 * FIRST MATCH WINS — the order is normative, not incidental: a checkbox also
 * carries no `w:text`, and a repeating-section item nested in a group would
 * otherwise classify as its container.
 */
const CLASS_PROBES: ReadonlyArray<readonly [SdtClass, string]> = [
  ["checkbox", "w14:checkbox"],
  ["dropdown", "w:dropDownList"],
  ["combobox", "w:comboBox"],
  ["date", "w:date"],
  ["picture", "w:picture"],
  ["building-block", "w:docPartObj"],
  ["building-block", "w:docPartList"],
  ["group", "w:group"],
  ["repeating", "w15:repeatingSection"],
  ["repeating-item", "w15:repeatingSectionItem"],
  ["text", "w:text"],
];

/**
 * Classes that never carry inline `{#cc:N}` anchors (spec §1). They still
 * consume an ordinal (A1.3) and still appear in the ledger.
 */
const UNANCHORED_CLASSES: ReadonlySet<string> = new Set([
  "checkbox",
  "picture",
  "building-block",
  "repeating",
  "repeating-item",
]);

/**
 * Content-lock values that make a control's CONTENTS read-only. `sdtLocked` is
 * deliberately absent: it forbids deleting the control but leaves the contents
 * editable, so it is a ledger detail and never an inline flag (spec §2).
 */
const CONTENT_LOCK_VALUES: ReadonlySet<string> = new Set([
  "sdtContentLocked",
  "contentLocked",
]);

export interface SdtInfo {
  element: any;
  cls: SdtClass;
  alias: string | null;
  tag: string | null;
  sdtId: string | null;
  contentLocked: boolean;
  deleteLocked: boolean;
  bound: boolean;
  bindingXpath: string | null;
  /**
   * `w:dataBinding/@w:storeItemID` - which CustomXML store the xpath is
   * relative to. Needed to write the store back (spec-set-field §6).
   */
  storeItemId: string | null;
  showingPlaceholder: boolean;
  placeholderText: string | null;
  temporary: boolean;
  options: ReadonlyArray<readonly [string, string]>;
  checked: boolean | null;
  dateFormat: string | null;
  hasNestedSdt: boolean;
  ordinal: number;
  flags: ReadonlyArray<string>;
}

/**
 * Read `w:val`, falling back to `w14:val`.
 *
 * The w14 elements (`w14:checked`, `w14:checkedState`) carry their value in
 * the w14 namespace, not w. Reading only `w:val` silently reports every
 * checkbox as unchecked — worse than failing, because the projection would
 * render a confident `[ ]` over a ticked box.
 */
function val(element: any): string | null {
  if (!element) return null;
  return element.getAttribute("w:val") ?? element.getAttribute("w14:val");
}

/**
 * True when this control projects `{#cc:N}` / `{#/cc:N}`.
 *
 * A rich-text control containing another control is NOT anchored: its contents
 * project normally and it is ledger-only (spec §1), because anchoring it would
 * nest anchor pairs and make the empty-pair edit surface ambiguous.
 */
export function isAnchored(info: SdtInfo): boolean {
  if (UNANCHORED_CLASSES.has(info.cls)) return false;
  if (info.cls === "richtext" && info.hasNestedSdt) return false;
  return true;
}

export function openToken(info: SdtInfo): string {
  const flags = info.flags.map((f) => ` ${f}`).join("");
  return `{#cc:${info.ordinal}${flags}}`;
}

export function closeToken(info: SdtInfo): string {
  return `{#/cc:${info.ordinal}}`;
}

/**
 * The ballot glyphs Word writes as a checkbox's visible content.
 *
 * Substituted ONLY inside a checkbox control: the corpus has bare `U+2610`
 * runs sitting in ordinary prose outside any control (`odot_uic_drywell` has
 * two), and rewriting those would invent checkboxes in a document that has 19
 * real ones for them to hide among.
 *
 * Word writes the glyph as literal `w:t` text, not `w:sym` — verified against
 * Word 16.0 — which is what lets a one-character run back a one-character span.
 */
export const BALLOT_GLYPHS: ReadonlySet<string> = new Set([
  "\u2610",
  "\u2611",
  "\u2612",
]);

/** Bracket halves of the checkbox token. Virtual: they map to no run. */
export const CHECKBOX_OPEN = "[";
export const CHECKBOX_CLOSE = "]";

/**
 * The middle character of the `[x]` / `[ ]` token (spec-projection.md §4).
 *
 * Read from `w14:checked`, NOT from the glyph, and the COM battery is why:
 * Word restores `w14:checked` when a toggle is rejected, so the attribute is
 * the value that survives the review, while the glyph run can lag it inside a
 * pending revision.
 */
export function checkboxMark(info: SdtInfo): string {
  return info.checked ? "x" : " ";
}

/** Classify one `w:sdt` from its `w:sdtPr`. Never mutates the element. */
export function classifySdt(sdtElement: any, ordinal = 0): SdtInfo {
  const sdtPr = findChild(sdtElement, QN_W_SDTPR);

  let cls: SdtClass = "richtext";
  if (sdtPr) {
    for (const [name, probe] of CLASS_PROBES) {
      if (findChild(sdtPr, probe)) {
        cls = name;
        break;
      }
    }
  }

  const alias = val(sdtPr ? findChild(sdtPr, "w:alias") : null);
  const tag = val(sdtPr ? findChild(sdtPr, "w:tag") : null);
  const sdtId = val(sdtPr ? findChild(sdtPr, "w:id") : null);

  const lockVal = val(sdtPr ? findChild(sdtPr, "w:lock") : null);
  const contentLocked = lockVal !== null && CONTENT_LOCK_VALUES.has(lockVal);
  // sdtContentLocked implies the control cannot be deleted either.
  const deleteLocked = lockVal === "sdtLocked" || lockVal === "sdtContentLocked";

  const binding = sdtPr ? findChild(sdtPr, "w:dataBinding") : null;
  const bound = !!binding;
  const bindingXpath = binding ? binding.getAttribute("w:xpath") : null;
  const storeItemId = binding ? binding.getAttribute("w:storeItemID") : null;

  const showingPlaceholder = !!(sdtPr && findChild(sdtPr, "w:showingPlcHdr"));

  // w:temporary marks a control Word removes as soon as its contents are
  // edited. Ledger-only (spec-fields-ledger §3 segment 6): it changes nothing
  // about the projection, but an agent planning a write needs to know the
  // control will not survive the edit.
  const temporaryEl = sdtPr ? findChild(sdtPr, "w:temporary") : null;
  const temporaryVal = temporaryEl ? val(temporaryEl) : null;
  const temporary =
    !!temporaryEl &&
    (temporaryVal === null || ["1", "true", "on"].includes(temporaryVal));

  const content = findChild(sdtElement, QN_W_SDTCONTENT);
  let placeholderText: string | null = null;
  if (showingPlaceholder && content) {
    // The ghost text is a perfectly ordinary run inside sdtContent - which is
    // exactly why it leaked into the projection as body text before CC-1.
    // Captured here so the consumer can render it as a bubble and nowhere else.
    const ghost = findAllDescendants(content, "w:t")
      .map((t: any) => t.textContent ?? "")
      .join("")
      .trim();
    placeholderText = ghost || null;
  }

  let options: ReadonlyArray<readonly [string, string]> = [];
  if (cls === "dropdown" || cls === "combobox") {
    const listEl = sdtPr
      ? findChild(sdtPr, cls === "dropdown" ? "w:dropDownList" : "w:comboBox")
      : null;
    if (listEl) {
      const items: Array<readonly [string, string]> = [];
      for (const child of Array.from(listEl.childNodes) as any[]) {
        if (child.nodeType !== 1 || child.tagName !== "w:listItem") continue;
        const display = child.getAttribute("w:displayText");
        const value = child.getAttribute("w:value");
        items.push([display ?? value ?? "", value ?? display ?? ""]);
      }
      options = items;
    }
  }

  let checked: boolean | null = null;
  if (cls === "checkbox") {
    const cb = sdtPr ? findChild(sdtPr, "w14:checkbox") : null;
    const raw = val(cb ? findChild(cb, "w14:checked") : null);
    checked = raw === "1" || raw === "true";
  }

  let dateFormat: string | null = null;
  if (cls === "date") {
    const dateEl = sdtPr ? findChild(sdtPr, "w:date") : null;
    dateFormat = val(dateEl ? findChild(dateEl, "w:dateFormat") : null);
  }

  const hasNestedSdt = !!content && findAllDescendants(content, QN_W_SDT).length > 0;

  // Flag order is normative (spec §2): locked, bound, group. A group is an
  // inherently locked region, so it never also emits `locked`.
  const flags: string[] = [];
  if (contentLocked && cls !== "group") flags.push("locked");
  if (bound) flags.push("bound");
  if (cls === "group") flags.push("group");

  return {
    element: sdtElement,
    cls,
    alias,
    tag,
    sdtId,
    contentLocked,
    deleteLocked,
    bound,
    bindingXpath,
    storeItemId,
    showingPlaceholder,
    placeholderText,
    temporary,
    options,
    checked,
    dateFormat,
    hasNestedSdt,
    ordinal,
    flags,
  };
}

/**
 * Yield every `w:sdt` under `partElement` in document order.
 *
 * Document order is exactly projection order WITHIN a part, including nested
 * controls: this is a pre-order walk, so a container comes before the controls
 * it wraps — which is what spec §1 requires ("1-based in projection order
 * across ALL classes").
 *
 * `findAllDescendants` is used rather than a hand-rolled walk so the ordering
 * matches the rest of the engine's descendant queries exactly.
 */
export function iterSdtElementsInOrder(partElement: any): any[] {
  if (!partElement) return [];
  return findAllDescendants(partElement, QN_W_SDT);
}

/**
 * Build the element -> SdtInfo map for a whole document.
 *
 * `partElements` is the ordered sequence of projected part roots (headers,
 * body, footers, notes — the flattened projection order used by
 * `iter_document_parts_with_kind`). Ordinals run 1..N across ALL parts and ALL
 * classes, so an un-anchored control still consumes its number (A1.3).
 *
 * This is the single pre-pass mandated by spec §9: ingest and the mapper both
 * consume THIS map rather than counting controls themselves, so the two cannot
 * drift the way they did over block separators (PROGRESS.md 2026-08-21).
 */
export function assignOrdinals(partElements: Iterable<any>): Map<any, SdtInfo> {
  const infos = new Map<any, SdtInfo>();
  let ordinal = 0;
  for (const partElement of partElements) {
    for (const el of iterSdtElementsInOrder(partElement)) {
      ordinal += 1;
      infos.set(el, classifySdt(el, ordinal));
    }
  }
  return infos;
}

/** A control boundary in the traversal stream. */
export interface SdtEvent {
  type: SdtEventType;
  info: SdtInfo;
}

/**
 * The boundary kinds. The `checkbox_*` trio is chrome around a checkbox's
 * three-character token (spec-projection.md §4): `checkbox_start` and
 * `checkbox_end` are the virtual brackets, while `checkbox_mark` is a
 * FALLBACK for the degenerate control that has no glyph run to carry the mark
 * as a real span.
 */
export type SdtEventType =
  | "sdt_start"
  | "sdt_end"
  | "checkbox_start"
  | "checkbox_mark"
  | "checkbox_end";

const SDT_EVENT_TYPES: ReadonlySet<string> = new Set([
  "sdt_start",
  "sdt_end",
  "checkbox_start",
  "checkbox_mark",
  "checkbox_end",
]);

/**
 * Narrow a traversal item to a control boundary.
 *
 * Structural rather than nominal, because the traversal stream carries plain
 * objects: an item is an SdtEvent iff it is one of the two boundary types AND
 * carries an `info`. Testing `type` alone would misfire the day some other
 * producer emits a like-named event.
 */
export function isSdtEvent(item: any): item is SdtEvent {
  return !!item && SDT_EVENT_TYPES.has(item.type) && "info" in item;
}

/**
 * The DOM root to scan for content controls in a projected part.
 *
 * `iter_document_parts_with_kind` yields heterogeneous objects (a Document, a
 * header/footer part, a NotesPart), so the ordinal pre-pass needs one place
 * that knows how to reach the element behind each of them - and BOTH producers
 * must reach it the same way, or they would scan different roots and number
 * the controls differently.
 */
export function partElement(part: any): any {
  return part?.element ?? part?._element ?? part;
}

/**
 * A block-level content control, yielded undescended by the block iterator.
 *
 * Distinct from `SdtEvent`, which lives in the *inline* stream. A block-level
 * control has to be visible to the BLOCK loop, because the "\n\n" separators
 * around it — and the rollback when it projects nothing — are decided there.
 *
 * Carries the raw element, not an `SdtInfo`: `iter_block_items` has no ordinal
 * map, so the consumer resolves it against the one pre-pass. That keeps the
 * pre-pass the single source of numbering.
 */
export class BlockSdt {
  constructor(public readonly element: any) {}
}

/**
 * The `w:sdt` that directly wraps this `w:tr`/`w:tc`, or null.
 *
 * Row- and cell-level controls are invisible to the row/cell walkers by
 * design — `findChildrenSdtTransparent` exists precisely to see THROUGH them
 * so the rows stay visible (CC-0). Rather than change that contract and every
 * caller with it, projection asks the element which control encloses it. One
 * hop up, not a search: a row-level control is exactly
 * `w:sdt > w:sdtContent > w:tr`.
 */
export function wrappingSdt(element: any): any {
  const parent = element?.parentNode;
  if (parent && parent.tagName === QN_W_SDTCONTENT) {
    const grandparent = parent.parentNode;
    if (grandparent && grandparent.tagName === QN_W_SDT) return grandparent;
  }
  return null;
}
