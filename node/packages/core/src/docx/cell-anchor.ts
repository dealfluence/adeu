// FILE: node/packages/core/src/docx/cell-anchor.ts
/**
 * Shared empty-cell anchor resolution for ingest.extract_table and
 * mapper._map_table (the two byte-identical twins of the Virtual Text
 * contract).
 *
 * A cell with no w14:paraId on its first paragraph and no projected content
 * still needs a stable, document-native `{#cell:<id>}` anchor. The fallback
 * id is deterministic: FNV-1a over `fallback-paraId-${index}` where `index`
 * is the paragraph's document-order position among ALL w:p elements of its
 * OPC part. Word-assigned paraIds and this derivation must both survive
 * re-reads across processes, so the index MUST equal what
 * `Array.from(ownerDocument.getElementsByTagName("w:p")).indexOf(firstP)`
 * would produce — that expression was the historical implementation.
 *
 * Why it was replaced: that expression is a whole-document scan, and the
 * same code path MUTATES the DOM right after (setAttribute/appendChild bump
 * xmldom's Document._inc, invalidating every live NodeList), so EVERY
 * fallback cell re-walked the entire tree: O(empty cells × document size).
 * On a 45 MB document.xml (2.68M elements, 430 empty cells) that is ~1.15e9
 * node visits — minutes of CPU inside one read_docx call.
 *
 * The cache below keeps the historical observable semantics exactly:
 * - Built lazily with ONE preorder walk on first fallback need; documents
 *   with no fallback cells never pay it.
 * - Freshness is keyed on xmldom's Document._inc mutation counter — any
 *   foreign mutation (engine edits between extractions) invalidates the
 *   cache, exactly like the historical rescan-per-call.
 * - The fallback's OWN mutations stay coherent explicitly: setAttribute
 *   cannot change the w:p set (resync the stored inc); a created paragraph
 *   is absent from the map, which forces a rebuild that includes it (rare —
 *   only cells with no w:p child at all).
 * - On DOM implementations without `_inc`, every lookup rebuilds — the
 *   historical cost, never stale data.
 */

interface WpIndexCache {
  inc: number;
  map: Map<Element, number>;
}

const CACHE_KEY = "__adeu_wp_index_cache";

function docInc(ownerDoc: any): number | null {
  return typeof ownerDoc._inc === "number" ? ownerDoc._inc : null;
}

/** Preorder walk assigning each w:p its document-order index (matches
 * getElementsByTagName order). */
function buildWpIndexMap(ownerDoc: any): Map<Element, number> {
  const map = new Map<Element, number>();
  const root = ownerDoc.documentElement;
  if (!root) return map;
  let i = 0;
  let node: any = root;
  while (node) {
    if (node.nodeType === 1 && node.tagName === "w:p") {
      map.set(node, i++);
    }
    if (node.firstChild) {
      node = node.firstChild;
      continue;
    }
    while (node && node !== root && !node.nextSibling) {
      node = node.parentNode;
    }
    if (!node || node === root) break;
    node = node.nextSibling;
  }
  return map;
}

function wpDocumentOrderIndex(ownerDoc: any, target: Element): number {
  const inc = docInc(ownerDoc);
  let cache: WpIndexCache | undefined = ownerDoc[CACHE_KEY];
  if (
    !cache ||
    inc === null ||
    cache.inc !== inc ||
    !cache.map.has(target)
  ) {
    cache = { inc: inc ?? NaN, map: buildWpIndexMap(ownerDoc) };
    ownerDoc[CACHE_KEY] = cache;
  }
  const idx = cache.map.get(target);
  return idx === undefined ? -1 : idx;
}

/** Our own setAttribute after a lookup cannot have changed the w:p set —
 * re-stamp the stored inc so the next cell's lookup stays a cache hit. */
function resyncAfterOwnAttributeMutation(ownerDoc: any): void {
  const cache: WpIndexCache | undefined = ownerDoc[CACHE_KEY];
  const inc = docInc(ownerDoc);
  if (cache && inc !== null) cache.inc = inc;
}

/**
 * Resolves the `{#cell:<paraId>}` anchor id for a table cell, applying the
 * deterministic fallback (and its DOM side effects) when the cell is empty
 * and unlabeled. `is_empty` is caller-defined: ingest keys on projected cell
 * text, the mapper on projected width — the two predicates must stay exactly
 * as they were.
 *
 * Returns the resolved paraId (null when the cell has content but no
 * paraId — historical behavior: no anchor) and the first paragraph element
 * (created if the fallback ran on a paragraph-less cell).
 */
export function resolve_cell_anchor(
  cell_element: Element,
  is_empty: boolean,
): { paraId: string | null; firstP: Element | undefined } {
  let firstP = cell_element.getElementsByTagName("w:p")[0] as
    | Element
    | undefined;
  let paraId = firstP ? firstP.getAttribute("w14:paraId") : null;

  if (!paraId && is_empty) {
    const ownerDoc = cell_element.ownerDocument! as any;
    if (!firstP) {
      firstP = ownerDoc.createElement("w:p") as Element;
      cell_element.appendChild(firstP);
    }
    const index = wpDocumentOrderIndex(ownerDoc, firstP);
    let hash = 2166136261;
    const str = `fallback-paraId-${index}`;
    for (let i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash +=
        (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }
    paraId = (hash >>> 0).toString(16).toUpperCase().padStart(8, "0");
    firstP.setAttribute("w14:paraId", paraId);
    resyncAfterOwnAttributeMutation(ownerDoc);
  }

  return { paraId, firstP };
}
