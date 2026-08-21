/**
 * CC-1e / A1.7 — anchor fabrication, mutation and deletion are refused.
 *
 * Twin of python/tests/test_cc_anchor_fabrication_refusal.py.
 *
 * `{#cc:N}` / `{#/cc:N}` are read-only projections of a control's structure.
 * An agent that can write them can invent a control that does not exist,
 * silently unbalance a pair, or strip a `locked` flag it may not clear.
 *
 * VAL-OBS-9 already refused anchors that GAINED copies, so A1.7(a)
 * fabrication and A1.7(c) flag-stripping were covered before this task.
 * A1.7(b) — deletion — was not: that loop iterates `new_text`'s anchors, so an
 * edit whose target covers `{#/cc:3}` and whose `new_text` omits it had
 * nothing to iterate and passed.
 *
 * The refusal is scoped to `cc` anchors on purpose. Two anchor classes are
 * deliberate TARGETING surfaces that a blanket symmetric rule would break, and
 * both are pinned below: `{#cell:paraId}` empty-cell writes, and the empty
 * pair `{#cc:N}{#/cc:N}` that spec-projection.md §3 names as sanctioned edit
 * surface #1.
 */
import { describe, it, expect } from "vitest";
import { validate_edit_strings, RedlineEngine, BatchValidationError } from "./engine.js";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";

const REFUSAL = "content-control anchor markers";

function errorsFor(target: string, newText: string): string[] {
  return validate_edit_strings([
    { type: "modify", target_text: target, new_text: newText } as any,
  ]);
}

function refused(target: string, newText: string): boolean {
  return errorsFor(target, newText).some((e) => e.includes(REFUSAL));
}

describe("CC-1e / A1.7 — anchor tokens are structural in both directions", () => {
  it("(a) fabricating an anchor is refused", () => {
    expect(refused("Counterparty: ", "Counterparty: {#cc:99}ACME{#/cc:99}")).toBe(true);
  });

  it("(b) deleting a closing anchor is refused", () => {
    // The regression this task existed for: deletion had no check at all.
    expect(refused("ACME Corp{#/cc:3}", "ACME Corp")).toBe(true);
  });

  it("(b) deleting an opening anchor is refused", () => {
    expect(refused("{#cc:3}ACME Corp", "ACME Corp")).toBe(true);
  });

  it("(b) deleting both halves is refused", () => {
    // Not the empty-pair surface: this target carries CONTENT between them.
    expect(refused("{#cc:3}ACME Corp{#/cc:3}", "ACME Corp")).toBe(true);
  });

  it("(c) stripping a flag is refused", () => {
    expect(refused("{#cc:7 locked}", "{#cc:7}")).toBe(true);
  });

  it("adding a flag is refused", () => {
    expect(refused("{#cc:7}", "{#cc:7 locked}")).toBe(true);
  });

  it("renumbering an anchor is refused", () => {
    expect(refused("{#cc:3}ACME{#/cc:3}", "{#cc:4}ACME{#/cc:4}")).toBe(true);
  });

  it("inverting a pair is refused", () => {
    // Caught only by an ORDERED comparison — the multiset is identical.
    expect(refused("{#cc:3}ACME{#/cc:3}", "{#/cc:3}ACME{#cc:3}")).toBe(true);
  });
});

describe("CC-1e — the surfaces that must stay open", () => {
  it("editing content between the anchors is allowed", () => {
    expect(refused("{#cc:3}ACME Corp{#/cc:3}", "{#cc:3}Beta Ltd{#/cc:3}")).toBe(false);
  });

  it.each([
    "{#cc:5}{#/cc:5}",
    "{#cc:5 locked}{#/cc:5}",
    "{#cc:2}{>>placeholder: Click or tap here to enter text.<<}{#/cc:2}",
  ])("the empty-pair fill surface stays open (%s)", (target) => {
    // spec-projection.md §3, sanctioned edit surface #1. The anchors are not
    // deleted here — the wrapper survives and only the content changes.
    expect(refused(target, "Jane Roe")).toBe(false);
  });

  it("the empty-cell write surface stays open", () => {
    expect(refused("{#cell:abc123}", "Hello")).toBe(false);
  });

  it("a mismatched pair is not the empty-pair surface", () => {
    expect(refused("{#cc:5}{#/cc:6}", "Jane Roe")).toBe(true);
  });

  it("bookmark anchors keep their old asymmetric rule", () => {
    expect(refused("{#_Ref44}old", "{#_Ref44}new")).toBe(false);
    expect(refused("{#_Ref44}old", "new")).toBe(false);
  });

  it("plain edits are unaffected", () => {
    expect(errorsFor("old text", "new text")).toEqual([]);
  });
});

describe("CC-1e — the document is unchanged after a refusal", () => {
  it("surfaces as BatchValidationError with no write", async () => {
    const doc = await DocumentObject.load(Buffer.from(ccFixtureBytes()));
    const engine = new RedlineEngine(doc);
    const before = engine.doc.element.toString();

    // process_batch throws SYNCHRONOUSLY, so `rejects` would never see a
    // promise — it would blow up before expect() was called.
    let caught: unknown;
    try {
      await engine.process_batch([
        { type: "modify", target_text: "ACME Corp{#/cc:3}", new_text: "ACME Corp" } as any,
      ]);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(BatchValidationError);
    expect((caught as BatchValidationError).errors.some((e) => e.includes(REFUSAL))).toBe(
      true,
    );

    expect(engine.doc.element.toString(), "a refused batch must not mutate the document").toBe(
      before,
    );
  });
});
