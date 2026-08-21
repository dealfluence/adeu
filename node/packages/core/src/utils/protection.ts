/**
 * Document protection state, read once at load (spec-gates.md §3).
 *
 * `w:documentProtection` in `word/settings.xml` is what Word writes when a
 * user picks Review -> Restrict Editing. It carries two things this engine
 * cares about: which editing mode is permitted (`w:edit`) and whether the
 * restriction is actually being enforced (`w:enforcement`).
 *
 * **Adeu never verifies or cracks `w:hash`.** Enforcement is honoured as
 * stated intent, not as a security boundary, because it is not one: Word's own
 * enforcement is equally advisory at the XML level - anything that can write
 * the file can clear the element. The sanctioned bypass is the override
 * parameters, which are explicit and disclosed in the batch report, rather
 * than a silent decision by the engine about whether a password looks real.
 *
 * The python twin is `python/src/adeu/utils/protection.py` and must stay
 * behaviourally identical.
 */

import { findDescendantsByLocalName } from "../sanitize/transforms.js";

const SETTINGS_PART_PATH = "word/settings.xml";

/**
 * `w:edit` values this engine gates on. Anything else (e.g. Word's
 * `readOnlyRecommended`, which is a suggestion carried elsewhere) is treated
 * as no restriction, because gating on a mode whose semantics we have not
 * verified against Word would refuse writes Word itself permits.
 */
export const KNOWN_EDIT_MODES: ReadonlySet<string> = new Set([
  "readOnly",
  "forms",
  "comments",
  "trackedChanges",
]);

/**
 * Human-readable phrasing per mode, used in gate errors. The wording matters:
 * A3.4 pins "read-only" and "enforced" as substrings of G4's error.
 */
const MODE_PROSE: Record<string, string> = {
  readOnly: "read-only",
  forms: "fill-in-forms",
  comments: "comments-only",
  trackedChanges: "tracked-changes-only",
};

/** The parsed `w:documentProtection`, or the absence of one. */
export interface DocumentProtection {
  /** `w:edit` verbatim when it is one of `KNOWN_EDIT_MODES`, else `null`. */
  readonly edit: string | null;
  /** `w:enforcement` resolved through the OOXML boolean rule. */
  readonly enforced: boolean;
}

/** The shared "no restriction" value. */
export const UNPROTECTED: DocumentProtection = Object.freeze({
  edit: null,
  enforced: false,
});

/**
 * Is there a restriction this engine should gate on?
 *
 * Both halves are required. An unenforced `w:documentProtection` is Word's own
 * "restriction configured but switched off" state: Word does not apply it, so
 * neither do we. Gating on the mode alone would refuse edits that Word
 * permits, which is the one direction of wrongness these gates must never
 * take.
 */
export function isProtectionActive(p: DocumentProtection): boolean {
  return p.edit !== null && p.enforced;
}

/** Phrasing for gate errors, e.g. `read-only, enforced`. */
export function describeProtection(p: DocumentProtection): string {
  if (p.edit === null) return "unprotected";
  const prose = MODE_PROSE[p.edit] ?? p.edit;
  return `${prose}, ${p.enforced ? "enforced" : "not enforced"}`;
}

/** The OOXML boolean rule: absent attribute means true when the element is present. */
function isTruthy(value: string | null | undefined): boolean {
  if (value === null || value === undefined) return true;
  return !["0", "false", "off"].includes(value.toLowerCase());
}

/**
 * Read protection state from a loaded document.
 *
 * Defensive throughout, and deliberately so: this runs on every engine load,
 * including for documents Adeu did not write. A malformed or unreadable
 * settings part means "unprotected" rather than an exception, because failing
 * to load a document is a much worse outcome than failing to gate one - and
 * the gates are a safety rail over Word's own behaviour, not a security
 * control.
 */
export function readDocumentProtection(doc: any): DocumentProtection {
  let element: any = null;
  try {
    const part = doc?.pkg?.getPartByPath?.(SETTINGS_PART_PATH);
    if (!part?._element) return UNPROTECTED;
    // Local-name matching rather than a qualified lookup, mirroring
    // `domain.ts`'s privacy-flag reader: settings.xml variants from different
    // Word versions are not reliably prefixed the way the canonical schema is.
    element = findDescendantsByLocalName(part._element, "documentProtection")[0] ?? null;
  } catch {
    return UNPROTECTED;
  }
  if (!element) return UNPROTECTED;

  let edit: string | null = null;
  let enforcement: string | null = null;
  const attrs = element.attributes;
  if (attrs) {
    for (let i = 0; i < attrs.length; i++) {
      const attr = attrs[i];
      const local: string = (attr.name ?? "").split(":").pop() ?? "";
      if (local === "edit") edit = attr.value;
      else if (local === "enforcement") enforcement = attr.value;
    }
  }

  if (edit === null || !KNOWN_EDIT_MODES.has(edit)) {
    // A restriction we do not model. Treated as unprotected on purpose -
    // see KNOWN_EDIT_MODES.
    return UNPROTECTED;
  }

  return { edit, enforced: isTruthy(enforcement) };
}
