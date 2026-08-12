// FILE: node/packages/core/src/docx/cell-anchor.ts
/**
 * Shared empty-cell anchor resolution for ingest.extract_table and
 * mapper._map_table (the two byte-identical twins of the Virtual Text
 * contract).
 *
 * A cell with no w14:paraId on its first paragraph and no projected content
 * still needs a stable, document-native `{#cell:<id>}` anchor. The fallback
 * id is deterministic: FNV-1a over `fallback-paraId-${index}`, folded into the
 * ST_LongHexNumber range Word accepts (see docx/long-hex-number.ts), where
 * `index` is the paragraph's document-order position among ALL w:p elements of
 * its OPC part. Word-assigned paraIds and this derivation must both survive
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

import { toLongHexNumber } from "./long-hex-number.js";

interface WpIndexCache {
  inc: number;
  map: Map<Element, number>;
}

const CACHE_KEY = "__adeu_wp_index_cache";
const CLEAN_INC_KEY = "__adeu_clean_inc";

function docInc(ownerDoc: any): number | null {
  return typeof ownerDoc._inc === "number" ? ownerDoc._inc : null;
}

/**
 * Cleanliness contract for the engine's lazy transactional snapshot
 * (docs/PERFORMANCE.md §5.2): a part is "clean" when its live DOM is
 * byte-reconstructible from its pristine load-time XML (`part.blob`) —
 * IGNORING this module's deterministic anchor stamps, which any fresh parse
 * re-derives identically (§3.5 invariant). Rollback may then restore a clean
 * part by re-parsing its blob instead of deep-cloning 2.7M nodes up front.
 *
 * `markPartClean` pins the marker to the document's current mutation counter
 * (called at load, and after a blob-faithful restore). The stamping code
 * below advances the marker across its OWN mutations, exactly like the
 * wp-index resync — foreign mutations (real edits) leave it behind, making
 * the part dirty.
 */
export function markPartClean(ownerDoc: any): void {
  const inc = docInc(ownerDoc);
  if (inc !== null) ownerDoc[CLEAN_INC_KEY] = inc;
}

/** True when the part's DOM still matches blob-modulo-anchor-stamps. A
 * document without mutation counters is never considered clean. */
export function isPartClean(ownerDoc: any): boolean {
  const inc = docInc(ownerDoc);
  return inc !== null && ownerDoc[CLEAN_INC_KEY] === inc;
}

/**
 * Preorder walk assigning each w:p its document-order index (matches
 * getElementsByTagName order).
 *
 * Driven by an explicit cursor stack over childNodes arrays rather than
 * nextSibling/parentNode pointers. fast-xml implements nextSibling as
 * parentNode.childNodes.indexOf(this) — O(siblings), not the O(1) linked-list
 * step xmldom provided — so a pointer walk over a wide w:body was quadratic:
 * measured 1.6ms / 14.4ms / 223.6ms at 2k / 8k / 32k paragraphs (~n^1.96),
 * extrapolating to ~35s per build on the 45MB document this index exists to
 * make fast. Same traversal strategy as FastParent.getElementsByTagName.
 */
function buildWpIndexMap(ownerDoc: any): Map<Element, number> {
  const map = new Map<Element, number>();
  const root = ownerDoc.documentElement;
  if (!root) return map;
  let i = 0;
  if (root.nodeType === 1 && root.tagName === "w:p") map.set(root, i++);
  // Frames of [node, next-child-cursor]; only elements are pushed, since no
  // other node type can contain a w:p.
  const nodes: any[] = [root];
  const cursors: number[] = [0];
  while (nodes.length) {
    const top = nodes.length - 1;
    const children = nodes[top].childNodes;
    if (!children || cursors[top] >= children.length) {
      nodes.pop();
      cursors.pop();
      continue;
    }
    const child = children[cursors[top]++];
    if (child.nodeType === 1) {
      if (child.tagName === "w:p") map.set(child, i++);
      nodes.push(child);
      cursors.push(0);
    }
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
    // Stamps (and the created placeholder paragraph) are deterministic
    // projection artifacts a fresh parse re-derives identically — they must
    // not flip the part to "dirty" for the lazy-snapshot contract above.
    const wasClean = isPartClean(ownerDoc);
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
    // The derived id is STAMPED into word/document.xml and handed to agents as
    // a `{#cell:<paraId>}` address, so it has to be an id Word will keep.
    // `(hash >>> 0)` spans the full UNSIGNED 32-bit range; Word reads
    // w14:paraId as a SIGNED int32 and discards anything at or above
    // 0x80000000 — then renumbers every other paraId in the part with it. That
    // is not a coin flip here: the derivation is deterministic, and it put 95
    // of the first 128 paragraph indices (indices 0-7 among them, i.e. the
    // first tables in a document) in the high half. See
    // BUG_paraId_signed_int32_thread_collapse.md.
    paraId = toLongHexNumber(hash);
    firstP.setAttribute("w14:paraId", paraId);
    resyncAfterOwnAttributeMutation(ownerDoc);
    if (wasClean) markPartClean(ownerDoc);
  }

  return { paraId, firstP };
}
