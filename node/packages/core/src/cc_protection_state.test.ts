// FILE: node/packages/core/src/cc_protection_state.test.ts
/**
 * CC-4 — load-time document protection state (spec-gates.md §3).
 *
 * `w:documentProtection` was not parsed anywhere in either engine before CC-4;
 * these tests pin the reader that every G4-G7 gate consults. The python twin
 * is `python/tests/test_cc_protection_state.py` and asserts the same things.
 *
 * The two behaviours worth stating out loud, because both are choices rather
 * than consequences:
 *
 * - An **unenforced** restriction is not a restriction. Word writes
 *   `w:documentProtection` with `w:enforcement="0"` when a user configures
 *   Restrict Editing and then switches it off; Word does not apply it, so
 *   neither does Adeu. Gating on the mode alone would refuse edits Word
 *   permits, which is the one direction of wrongness these gates must never
 *   take.
 * - An **unrecognised** `w:edit` mode is treated as unprotected, for the same
 *   reason: refusing on semantics we have not verified against real Word would
 *   invent policy.
 */

import { describe, it, expect } from "vitest";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import {
  UNPROTECTED,
  describeProtection,
  isProtectionActive,
  readDocumentProtection,
} from "./utils/protection.js";

const BODY = '<w:p><w:r><w:t xml:space="preserve">Body text.</w:t></w:r></w:p>';

type Mode = "forms" | "readOnly" | "comments" | "trackedChanges";

async function load(protection?: Mode, enforcement: string | null = "1") {
  const bytes = ccFixtureBytes(protection, BODY, enforcement);
  return DocumentObject.load(Buffer.from(bytes));
}

describe("document protection state (CC-4)", () => {
  it("an unprotected document reads as unprotected", async () => {
    expect(readDocumentProtection(await load())).toEqual(UNPROTECTED);
  });

  for (const mode of ["readOnly", "forms", "comments", "trackedChanges"] as Mode[]) {
    it(`${mode} is read and active`, async () => {
      const prot = readDocumentProtection(await load(mode));
      expect(prot.edit).toBe(mode);
      expect(prot.enforced).toBe(true);
      expect(isProtectionActive(prot)).toBe(true);
    });
  }

  it("an unenforced restriction is not active", async () => {
    // Configured but switched off. Word does not apply it; neither do we.
    const prot = readDocumentProtection(await load("readOnly", "0"));
    expect(prot.edit).toBe("readOnly");
    expect(prot.enforced).toBe(false);
    expect(isProtectionActive(prot)).toBe(false);
  });

  it("a missing enforcement attribute defaults to true", async () => {
    // The OOXML boolean rule: the attribute's absence means the element is on.
    const prot = readDocumentProtection(await load("forms", null));
    expect(prot.enforced).toBe(true);
    expect(isProtectionActive(prot)).toBe(true);
  });

  it("an unknown edit mode is treated as unprotected", async () => {
    // `readOnlyRecommended` is a suggestion, not an enforced restriction.
    // Gating on a mode whose semantics were never verified against Word would
    // invent policy, so unknown modes read as no restriction at all.
    const bytes = ccFixtureBytes(
      "readOnlyRecommended" as unknown as Mode,
      BODY,
    );
    const doc = await DocumentObject.load(Buffer.from(bytes));
    expect(readDocumentProtection(doc)).toEqual(UNPROTECTED);
  });

  it("describe names the mode and the enforcement", async () => {
    // A3.4 pins `read-only` and `enforced` as substrings of G4's error.
    expect(describeProtection(readDocumentProtection(await load("readOnly")))).toBe(
      "read-only, enforced",
    );
    expect(
      describeProtection(readDocumentProtection(await load("readOnly", "0"))),
    ).toBe("read-only, not enforced");
    expect(describeProtection(UNPROTECTED)).toBe("unprotected");
  });

  it("a document with no settings part does not throw", () => {
    // Defensive by design: failing to load a document is far worse than
    // failing to gate one, so anything unreadable reads as unprotected.
    expect(readDocumentProtection({} as any)).toEqual(UNPROTECTED);
    expect(readDocumentProtection(null as any)).toEqual(UNPROTECTED);
  });
});
