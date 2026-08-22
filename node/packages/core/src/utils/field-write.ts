/**
 * CC-5 - the XML half of `set_field` (spec-set-field.md §4/§5).
 *
 * Twin of `python/src/adeu/utils/field_write.py`. The *tracked* half of a
 * fill is not here: `set_field` desugars into an ordinary `ModifyText` so it
 * inherits atomicity, author resolution, affix trimming, comment wrapping and
 * the write gates rather than reimplementing them. What lives here is
 * everything a `ModifyText` cannot express - the untracked placeholder
 * teardown Word performs, and the attribute syncs (`w:date/@w:fullDate`,
 * `w14:checked`, `w:dropDownList/@w:lastValue`) that Word writes with no
 * revision of their own, the URL_RETARGET precedent.
 *
 * Namespace note: this DOM uses PREFIXED tag names (`w14:checkbox`), not the
 * Clark notation the Python twin needs - see utils/content-controls.ts.
 */

import type { SdtInfo } from "./content-controls.js";

function childrenOf(el: any): any[] {
  const out: any[] = [];
  const nodes = el?.childNodes;
  if (!nodes) return out;
  for (let i = 0; i < nodes.length; i++) {
    if (nodes[i].nodeType === 1) out.push(nodes[i]);
  }
  return out;
}

function findChild(parent: any, tagName: string): any | null {
  for (const child of childrenOf(parent)) {
    if (child.tagName === tagName) return child;
  }
  return null;
}

function descendants(el: any, tagName: string): any[] {
  if (!el?.getElementsByTagName) return [];
  return Array.from(el.getElementsByTagName(tagName) as any);
}

export function sdtContent(sdt: any): any | null {
  return findChild(sdt, "w:sdtContent");
}

export function sdtPr(sdt: any): any | null {
  return findChild(sdt, "w:sdtPr");
}

export function contentRuns(sdt: any): any[] {
  const content = sdtContent(sdt);
  return content ? descendants(content, "w:r") : [];
}

/**
 * Take a control out of placeholder state the way Word does (§4.1-4.2).
 *
 * Untracked, and deliberately: CC-6(a) filled an empty control in Word and
 * got exactly ONE revision, the insertion. The `w:showingPlcHdr` flag and the
 * ghost run simply vanish. Emitting a `w:del` for the ghost would put words
 * into the document the author never wrote - a reviewer would see "Click here
 * to enter text" struck through as if it had been real content.
 */
export function clearPlaceholder(info: SdtInfo): boolean {
  const pr = sdtPr(info.element);
  if (!pr) return false;
  const flag = findChild(pr, "w:showingPlcHdr");
  if (!flag) return false;
  pr.removeChild(flag);

  // The ghost run(s) go with it. Removing the flag alone would leave the
  // prompt text behind as real content, which is the one outcome worse than
  // not clearing at all: the placeholder would become the value.
  const content = sdtContent(info.element);
  if (content) {
    for (const run of descendants(content, "w:r")) {
      run.parentNode?.removeChild(run);
    }
  }
  return true;
}

/**
 * Dissolve the `w:sdt` shell, leaving its content in place (§4.4).
 *
 * For `w:temporary` controls, which Word unwraps on ANY content edit -
 * tracked or untracked, placeholder or already filled (CC-6(c)). The revision
 * outlives the wrapper, so this is one-way: rejecting the fill restores the
 * old text but not the control.
 */
export function unwrapSdt(sdt: any): boolean {
  const content = sdtContent(sdt);
  const parent = sdt.parentNode;
  if (!content || !parent) return false;
  for (const child of childrenOf(content)) {
    parent.insertBefore(child, sdt);
  }
  parent.removeChild(sdt);
  return true;
}

// ---------------------------------------------------------------------------
// Per-class value rules (spec-set-field.md §2, §5)
// ---------------------------------------------------------------------------

/** Classes `set_field` can write in v1. */
export const VALUE_BEARING = new Set(["text", "richtext", "dropdown", "combobox", "date", "checkbox"]);

/**
 * Classes that hold no single value. Refusing these is not a limitation, it
 * is data protection: a group's "content" is the other controls inside it, so
 * replacing it with a string would delete every field it contains.
 */
export const NON_VALUE = new Set(["group", "repeating", "repeating-item", "picture", "building-block"]);

const NON_VALUE_ADVICE: Record<string, string> = {
  group: "Edit the fields nested inside it individually - each has its own CC: id.",
  repeating:
    "Fill the fields inside a specific item instead; repeating-section operations (add/remove item) are not supported in v1.",
  "repeating-item":
    "Fill the fields inside the item instead; repeating-section operations (add/remove item) are not supported in v1.",
  picture: "Picture controls hold an image, which set_field cannot write.",
  "building-block": "Building-block galleries insert document parts, not text.",
};

/** Does this plain-text control permit `w:br` (a `w:text w:multiLine`)? */
export function isMultiline(info: SdtInfo): boolean {
  const pr = sdtPr(info.element);
  if (!pr) return false;
  const textEl = findChild(pr, "w:text");
  if (!textEl) return false;
  const v = textEl.getAttribute("w:multiLine");
  return v !== null && v !== "" && !["0", "false", "off"].includes(String(v).toLowerCase());
}

/** The A4.11 refusal for a control that holds no single value. */
export function refuseClass(cls: string, ordinal: number): string | null {
  if (VALUE_BEARING.has(cls)) return null;
  const advice = NON_VALUE_ADVICE[cls] ?? "set_field fills value-bearing fields only.";
  return (
    `CC:${ordinal} is a ${cls} and is not a value-bearing field. ${advice} ` +
    "set_field fills text, rich-text, dropdown, combobox, date and checkbox controls."
  );
}

/**
 * Everything about `value` that can be judged before writing anything.
 *
 * The A4.7 structure rules — what a class cannot physically hold — plus G10
 * (dropdown membership) and G12 (date parsing).
 *
 * G10 and G12 live here, rather than only in the apply path where they were
 * first written, because every CC-4 gate refuses during validation and a
 * contract that validates some rules early and others late costs the caller a
 * round trip to discover (Mikko, 2026-08-22). The apply path still performs
 * both checks: it computes the value it is about to write anyway, and a
 * backstop that agrees with its gate is the same belt-and-braces shape the
 * lock gates use.
 */
export function refuseValue(info: SdtInfo, ordinal: number, value: string): string | null {
  if (info.cls === "dropdown") {
    const [, err] = resolveOption(info, value);
    return err ? `CC:${ordinal}: ${err}` : null;
  }

  if (info.cls === "date") {
    if (parseIsoDate(value) === null) {
      return (
        `CC:${ordinal} is a date control; '${value}' is not a date. ` +
        "Use the canonical YYYY-MM-DD form (e.g. 2026-03-01)."
      );
    }
    return null;
  }

  if (info.cls !== "text") return null;
  if (value.includes("\n\n")) {
    return (
      `CC:${ordinal} is a plain-text control and cannot hold paragraphs. ` +
      "Remove the blank line, or use a rich-text control for multi-paragraph content."
    );
  }
  if (value.includes("\n") && !isMultiline(info)) {
    return (
      `CC:${ordinal} is a single-line plain-text control and cannot hold a line break. ` +
      "Remove the newline, or set the control's multiLine property in Word."
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// Checkbox (spec-set-field.md §5)
// ---------------------------------------------------------------------------

/**
 * Accepted truthy/falsy spellings. Generous on input because the caller is a
 * language model reading a checkbox rendered as a bracket token, and strict
 * rejection of "checked" would be pedantry rather than safety.
 */
const TRUTHY = new Set(["true", "x", "[x]", "checked", "1", "yes", "on"]);
const FALSY = new Set(["false", "[ ]", "[]", "unchecked", "0", "no", "off", ""]);

/** `true`/`false`, or `null` when the string names neither state (G11). */
export function parseCheckboxValue(value: string): boolean | null {
  const v = value.trim().toLowerCase();
  if (TRUTHY.has(v)) return true;
  if (FALSY.has(v)) return false;
  return null;
}

/**
 * The `[character, font]` this control uses for the given state.
 *
 * Read from `w14:checkedState` / `w14:uncheckedState` rather than assumed: a
 * control may use any character in any symbol font, and hardcoding the common
 * Segoe UI Symbol pair would silently change the document's glyph on every
 * checkbox that used something else.
 */
export function checkboxGlyph(info: SdtInfo, checked: boolean): [string, string | null] {
  const fallback: [string, string | null] = checked ? ["\u2612", null] : ["\u2610", null];
  const pr = sdtPr(info.element);
  if (!pr) return fallback;
  const checkbox = findChild(pr, "w14:checkbox");
  if (!checkbox) return fallback;
  const state = findChild(checkbox, checked ? "w14:checkedState" : "w14:uncheckedState");
  if (!state) return fallback;
  const raw = state.getAttribute("w14:val");
  const font = state.getAttribute("w14:font");
  const char = raw ? String.fromCodePoint(parseInt(raw, 16)) : fallback[0];
  return [char, font || null];
}

/**
 * Flip `w14:checked/@w14:val`.
 *
 * SILENTLY, with no revision of its own: this is the URL_RETARGET class of
 * change (spec §5). The visible glyph swap carries the redline; a revision on
 * the attribute too would show the reviewer two changes for one act.
 */
export function setCheckboxChecked(info: SdtInfo, checked: boolean): void {
  const pr = sdtPr(info.element);
  if (!pr) return;
  const checkbox = findChild(pr, "w14:checkbox");
  if (!checkbox) return;
  let node = findChild(checkbox, "w14:checked");
  if (!node) {
    node = checkbox.ownerDocument.createElement("w14:checked");
    checkbox.appendChild(node);
  }
  node.setAttribute("w14:val", checked ? "1" : "0");
}

/** The run carrying the checkbox's visible character. */
export function glyphRun(info: SdtInfo): any | null {
  const runs = contentRuns(info.element);
  return runs.length ? runs[0] : null;
}

// ---------------------------------------------------------------------------
// Dropdown / combobox (G10) and date (G12)
// ---------------------------------------------------------------------------

/**
 * Map a caller's string onto a list item: `[displayText, error]`.
 *
 * A `displayText` match wins; a `w:value` match resolves to that item's
 * displayText, because the display text is what the document shows and what
 * the next reader will diff against. Only one of the two can be written, and
 * writing the machine value would make the document say `BC` where every
 * other row says `British Columbia`.
 */
export function resolveOption(info: SdtInfo, value: string): [string | null, string | null] {
  const options = info.options ?? [];
  if (!options.length) return [value, null];
  for (const [display] of options) {
    if (display === value) return [display, null];
  }
  for (const [display, val] of options) {
    if (val && val === value) return [display, null];
  }
  if (info.cls === "combobox") {
    // Free text is legal here; the report says so rather than the engine
    // refusing something Word permits.
    return [value, null];
  }
  const listed = options.map(([display]) => display).join(" | ");
  return [null, `'${value}' is not one of this dropdown's options. Choose one of: ${listed}.`];
}

export function optionIsListed(info: SdtInfo, value: string): boolean {
  return (info.options ?? []).some(([display, val]) => display === value || (!!val && val === value));
}

/**
 * Update `w:dropDownList/@w:lastValue` to match the written text.
 *
 * Silent, like the checkbox attribute: Word records the last selection here
 * and a stale value re-selects the old option in the dropdown UI while the
 * document text says something else.
 */
export function setDropdownLastValue(info: SdtInfo, displayText: string): void {
  const pr = sdtPr(info.element);
  if (!pr) return;
  for (const name of ["w:dropDownList", "w:comboBox"]) {
    const node = findChild(pr, name);
    if (node) {
      node.setAttribute("w:lastValue", displayText);
      return;
    }
  }
}

/**
 * The `w:dateFormat` letter-runs this engine renders in v1. Deliberately a
 * set of whole RUNS, not substrings: `dddd` is the day NAME and `MMMM` the
 * month name, and a substring test sees the supported `dd`/`MM` inside them.
 * Testing substrings turned `dddd, MMMM d` into `0101, 0303 1` - a date that
 * is not merely misformatted but unreadable, written silently.
 */
const SUPPORTED_DATE_RUNS = new Set(["yyyy", "MM", "M", "dd", "d"]);

/** `[y, m, d]` for a canonical `YYYY-MM-DD`, else `null`. */
export function parseIsoDate(value: string): [number, number, number] | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const probe = new Date(Date.UTC(y, mo - 1, d));
  if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== mo - 1 || probe.getUTCDate() !== d) {
    return null;
  }
  return [y, mo, d];
}

const pad = (n: number, width: number) => String(n).padStart(width, "0");

/**
 * `[text, unsupportedFormat]` for the control's own `w:dateFormat`.
 *
 * ISO when the control declares no format, or when it declares one this
 * engine cannot render faithfully - with the flag set so the caller's report
 * says so. Writing an approximation of a format the document asked for is
 * worse than writing the canonical form and admitting it.
 */
export function renderDate(parts: [number, number, number], dateFormat: string | null): [string, boolean] {
  const [y, mo, d] = parts;
  const iso = `${pad(y, 4)}-${pad(mo, 2)}-${pad(d, 2)}`;
  if (!dateFormat) return [iso, false];

  const runs = dateFormat.match(/[A-Za-z]+/g) ?? [];
  if (!runs.length || runs.some((run) => !SUPPORTED_DATE_RUNS.has(run))) {
    return [iso, true];
  }
  const values: Record<string, string> = {
    yyyy: pad(y, 4),
    MM: pad(mo, 2),
    M: String(mo),
    dd: pad(d, 2),
    d: String(d),
  };
  return [dateFormat.replace(/[A-Za-z]+/g, (run) => values[run]), false];
}

/** Sync `w:date/@w:fullDate`, silently (spec §5, URL_RETARGET class). */
export function setFullDate(info: SdtInfo, parts: [number, number, number]): void {
  const pr = sdtPr(info.element);
  if (!pr) return;
  const node = findChild(pr, "w:date");
  if (!node) return;
  const [y, mo, d] = parts;
  node.setAttribute("w:fullDate", `${pad(y, 4)}-${pad(mo, 2)}-${pad(d, 2)}T00:00:00Z`);
}

// ---------------------------------------------------------------------------
// Bound controls (spec-set-field.md §6)
// ---------------------------------------------------------------------------

const DS_ITEM_ID_ATTRS = ["ds:itemID", "itemID"];

function digitsOf(partname: string): string {
  const file = partname.split("/").pop() ?? "";
  return file.replace(/\D/g, "");
}

/**
 * The CustomXML part for `storeItemId`, or `null`.
 *
 * Resolved by item id rather than by trying each store in turn: a package may
 * carry several, and writing the caller's value into whichever one happened
 * to match the xpath would corrupt an unrelated data island.
 */
/**
 * Word exposes three PACKAGE parts through the data store under fixed item ids,
 * so a binding to one of them is live even though no `customXml/item*.xml`
 * carries that id. Measured on Word 16.0 (CC-20): `XMLMapping.IsMapped` is true,
 * the store still wins on open, and Word dual-writes the part — so these behave
 * exactly like a customXml store and must be written the same way. Without this
 * the resolver reports "the data store could not be resolved" and downgrades to
 * a content-only write, which the next open silently reverts.
 */
export const WELL_KNOWN_STORE_PARTS: Record<string, string> = {
  "6c3c8bc8-f283-45ae-878a-bab7291924a1": "docProps/core.xml",
  "6668398d-a668-4e3e-a5eb-62b293d839f1": "docProps/app.xml",
  "55af091b-3c7a-41e3-b477-f2fdaa23cfda": "docProps/custom.xml",
};

export function findBoundStore(doc: any, storeItemId: string | null): any | null {
  if (!storeItemId) return null;
  const want = storeItemId.replace(/[{}]/g, "").toLowerCase();
  const parts: any[] = doc?.part?.package?.parts ?? [];

  const wellKnown = WELL_KNOWN_STORE_PARTS[want];
  if (wellKnown) {
    const hit = parts.find((p) => {
      const name = String(p.partname).replace(/^\//, "");
      return name === wellKnown;
    });
    // The id is one Word reserves, but the part is absent: genuinely dangling.
    return hit ?? null;
  }

  const props = parts.filter((p) => String(p.partname).includes("/customXml/itemProps"));
  const items = parts.filter(
    (p) => String(p.partname).includes("/customXml/item") && !String(p.partname).includes("itemProps"),
  );

  for (const propPart of props) {
    const root = propPart._element;
    if (!root) continue;
    let itemId: string | null = null;
    for (const attr of DS_ITEM_ID_ATTRS) {
      const v = root.getAttribute?.(attr);
      if (v) {
        itemId = v;
        break;
      }
    }
    if (!itemId || itemId.replace(/[{}]/g, "").toLowerCase() !== want) continue;
    // itemProps1.xml describes item1.xml: the trailing digits pair them,
    // which is the convention every producer follows and is cheaper than
    // walking relationships for a part that may not expose them.
    const want_digits = digitsOf(String(propPart.partname));
    const match = items.find((p) => digitsOf(String(p.partname)) === want_digits);
    if (match) return match;
  }
  return null;
}

/**
 * `xmlns:ns0='...' xmlns:ns2='...'` -> `{ns0: "...", ns2: "..."}`.
 *
 * The value is not necessarily a URI: SharePoint binds list columns under a
 * bare GUID namespace (`2f9f1944-3a9b-49e1-93d3-d1cb06258e09`), so nothing
 * here may assume a scheme.
 */
/**
 * The namespace URI an element's own prefix resolves to, or null.
 *
 * Resolved by walking `xmlns:*` attributes up the tree rather than by reading
 * `namespaceURI`, which the core's DOM deliberately does not implement
 * (`docx/fast-xml.ts`: prefixes are opaque name parts, xmlns:* are ordinary
 * attributes). Keeping the namespace awareness local to the one caller that
 * needs it is cheaper than making the whole DOM namespace-aware for a
 * tie-break, and leaves that invariant standing.
 */
function namespaceUriOf(el: any): string | null {
  const name: string = el?.tagName ?? el?.nodeName ?? "";
  const colon = name.indexOf(":");
  const attr = colon === -1 ? "xmlns" : `xmlns:${name.slice(0, colon)}`;
  for (let node = el; node && node.nodeType === 1; node = node.parentNode) {
    const uri = node.getAttribute?.(attr);
    if (uri) return uri;
  }
  return null;
}

export function parsePrefixMappings(raw: string | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!raw) return out;
  const re = /xmlns:([\w.-]+)\s*=\s*['"]([^'"]*)['"]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) out[m[1]] = m[2];
  return out;
}

/**
 * Evaluate the subset of XPath a `w:dataBinding` actually uses.
 *
 * Deliberately not a general XPath engine, and CC-18 established that this is
 * the CORRECT answer rather than a cheap one. Python shipped the "proper"
 * version — lxml's engine — and it failed on every binding Word writes, because
 * the prefixes live in `w:prefixMappings` rather than in the store and a bare
 * call raises `Undefined namespace prefix`. Even fed the mappings it still
 * misses the shape where the intermediate element inherits a DEFAULT namespace,
 * since an unprefixed step means "no namespace" to XPath 1.0 and Word does not
 * mean that. Word matches on local name; so does this.
 *
 * `prefixMappings` is consulted only to DISAMBIGUATE — when a step carries a
 * prefix that resolves to a URI, a same-named child in that namespace wins over
 * one that is not. It can tighten a match, never break one.
 *
 * Anything outside the subset returns null, which routes to the same
 * dangling-binding warning as a missing store - the honest answer, rather than
 * a silent partial write.
 */
function resolveBindingPath(root: any, xpath: string, prefixMappings?: string | null): any | null {
  const steps = xpath.split("/").filter((s) => s.length > 0);
  if (!steps.length) return null;
  const prefixes = parsePrefixMappings(prefixMappings);
  const localOf = (n: string | undefined) => n?.split(":").pop();
  let node: any = null;
  for (let i = 0; i < steps.length; i++) {
    const m = /^([A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)(?:\[(\d+)\])?$/.exec(steps[i]);
    if (!m) return null;
    const name = m[1];
    const index = m[2] ? Number(m[2]) : 1;
    const colon = name.lastIndexOf(":");
    const local = colon === -1 ? name : name.slice(colon + 1);
    const wantUri = colon === -1 ? undefined : prefixes[name.slice(0, colon)];
    if (i === 0) {
      const rootName = root.tagName ?? root.nodeName;
      if (localOf(rootName) !== local) return null;
      if (index !== 1) return null;
      node = root;
      continue;
    }
    let matches = childrenOf(node).filter((c) => localOf(c.tagName ?? c.nodeName) === local);
    if (wantUri) {
      const exact = matches.filter((c) => namespaceUriOf(c) === wantUri);
      if (exact.length) matches = exact;
    }
    if (matches.length < index) return null;
    node = matches[index - 1];
  }
  return node;
}

/**
 * Set the bound node's text to `value`. True when the node was found.
 *
 * Mandatory rather than tidy (CC-6(e)): when `sdtContent` and the bound node
 * disagree, Word rewrites the CONTENT from the store on open, with no
 * revision. A tracked edit written to the content alone is not merely
 * inconsistent - it is destroyed the next time anyone opens the document.
 */
export function writeBoundValue(
  part: any,
  xpath: string | null,
  value: string,
  prefixMappings?: string | null,
): boolean {
  if (!xpath || !part?._element) return false;
  const node = resolveBindingPath(part._element, xpath, prefixMappings);
  if (!node) return false;
  for (const child of Array.from(node.childNodes ?? []) as any[]) {
    node.removeChild(child);
  }
  node.appendChild(node.ownerDocument.createTextNode(value));
  return true;
}
