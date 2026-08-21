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
  showingPlaceholder: boolean;
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

  const showingPlaceholder = !!(sdtPr && findChild(sdtPr, "w:showingPlcHdr"));

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

  const content = findChild(sdtElement, QN_W_SDTCONTENT);
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
    showingPlaceholder,
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
