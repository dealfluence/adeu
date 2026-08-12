// FILE: node/packages/core/src/docx/long-hex-number.ts
/**
 * The one place Adeu produces an `ST_LongHexNumber`.
 *
 * `ST_LongHexNumber` is schema-typed `xsd:hexBinary` of length 4, so every
 * value from `00000000` to `FFFFFFFF` validates. **Word does not read it that
 * way.** Word parses it as a SIGNED 32-bit integer, and ECMA-376 states the
 * constraint in prose rather than in the schema:
 *
 * > The value ... shall be greater than `0x00000000` and less than `0x80000000`.
 *
 * Out-of-range values are not rejected. They are silently discarded and
 * regenerated on load, which breaks everything that referenced them, with no
 * error, no repair prompt and nothing wrong-looking in the XML:
 *
 * - `w14:paraId`       -> `w15:paraIdParent` dangles, the reply leaves its
 *                         thread and renders as a new top-level comment (B5)
 * - `w14:paraId`       -> a `{#cell:<paraId>}` anchor stops addressing
 *                         anything the moment Word saves the document
 * - `w16cid:durableId` -> the comment anchor collapses to a zero-length point:
 *                         right author, right text, no highlight (B3)
 * - `w14:paraId` == 0  -> Word refuses the package outright: "The file appears
 *                         to be corrupted" (Word-verified 2026-08-12)
 *
 * The blast radius is bigger than the one id. Word-verified on Word 16.0: a
 * package with no bad ids keeps all 32 of its `w14:paraId`s across an
 * open/save; push exactly ONE of them over `0x7FFFFFFF` and it keeps NONE —
 * Word renumbers the whole part. A single bad id therefore invalidates every
 * `{#cell:<paraId>}` anchor in the document, not just its own.
 *
 * **This module exists because the previous fix was per-attribute.** B3 was
 * closed by giving `durableId` a dedicated masked generator and writing down
 * that `paraId` and `rsid` were "opaque 32-bit tokens with no such constraint"
 * — an assumption, recorded as fact in two engines' docstrings, in
 * AI_CONTEXT.md and in two tests that went red when the bug was finally fixed.
 * It was wrong, and it is why B5 shipped. There is no attribute for which the
 * high half is safe: Word's own output never uses it.
 *
 * Do not "reclaim" the high half. 2^31 values minted per comment, in documents
 * with tens to hundreds of comments, is no collision pressure at all. See
 * BUG_paraId_signed_int32_thread_collapse.md.
 *
 * Mirrored byte-for-byte by `python/src/adeu/utils/long_hex_number.py`.
 */

/**
 * Smallest value Word accepts. `0x00000000` is forbidden — and unlike the high
 * half it is not silently repaired, it makes Word reject the package.
 */
export const ST_LONG_HEX_NUMBER_MIN = 0x00000001;

/** Largest value Word accepts: `0x80000000` and above are negative int32. */
export const ST_LONG_HEX_NUMBER_MAX = 0x7fffffff;

/**
 * Folds any integer into the legal range and renders it as Word writes it.
 *
 * For DERIVED ids (a hash of something stable) where the value cannot simply
 * be redrawn — `resolve_cell_anchor`'s FNV-1a fallback is the one caller.
 * Clearing the high bit is what Word effectively does anyway; the `|| MIN`
 * guards the one input that would otherwise map to the forbidden zero.
 */
export function toLongHexNumber(value: number): string {
  const masked = (value & ST_LONG_HEX_NUMBER_MAX) >>> 0;
  return (masked || ST_LONG_HEX_NUMBER_MIN).toString(16).toUpperCase().padStart(8, "0");
}

/**
 * A fresh `ST_LongHexNumber`: `w14:paraId`, `w16cid:durableId`, `w:rsid*`.
 *
 * Every minted ST_LongHexNumber comes from here. Adding a second generator is
 * how this bug happened twice; add call sites, not generators.
 */
export function generateLongHexNumber(): string {
  const span = ST_LONG_HEX_NUMBER_MAX - ST_LONG_HEX_NUMBER_MIN + 1;
  return toLongHexNumber(ST_LONG_HEX_NUMBER_MIN + Math.floor(Math.random() * span));
}

/** True when Word will keep `value` rather than discard and regenerate it. */
export function isWordReadableLongHexNumber(value: string): boolean {
  if (!/^[0-9A-Fa-f]{1,8}$/.test(value)) return false;
  const number = parseInt(value, 16);
  return number >= ST_LONG_HEX_NUMBER_MIN && number <= ST_LONG_HEX_NUMBER_MAX;
}
