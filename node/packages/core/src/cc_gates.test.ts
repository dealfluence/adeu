// FILE: node/packages/core/src/cc_gates.test.ts
/**
 * CC-4 acceptance: the write-path gate matrix (A3, spec-gates.md §2).
 *
 * Error assertions pin the FOUR components the contract requires — the CC:N
 * reference, the rule, the sanctioned alternative, and the override parameter
 * — and deliberately NOT the full sentences. A3 says so explicitly: pinning
 * canonical prose would make every wording improvement a test failure, and the
 * components are what the agent actually consumes.
 *
 * Python twin: `python/tests/test_cc_gates.py`, same assertions in the same
 * order. Where behaviour differs from the A3 text as originally frozen, the
 * reason is spelled out inline rather than quietly encoded — see the A3.5
 * block.
 */

import { describe, it, expect } from "vitest";
import { unzipSync, strFromU8 } from "fflate";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine, BatchValidationError } from "./engine.js";

async function engine(
  protection?: string,
  overrides: Record<string, boolean> = {},
): Promise<RedlineEngine> {
  const doc = await DocumentObject.load(
    Buffer.from(ccFixtureBytes(protection as any)),
  );
  return new RedlineEngine(doc, "Gate Test", overrides);
}

const modify = (target_text: string, new_text: string, extra = {}) => ({
  type: "modify",
  target_text,
  new_text,
  ...extra,
});

const errorsFor = (eng: RedlineEngine, edit: any) =>
  eng.validate_edits([edit]).join("\n");

/** The error contract, spec-gates §2. All four, or the agent has to guess. */
function assertFourComponents(
  err: string,
  parts: { ref: string; rule: string; alternative: string; override: string | null },
) {
  expect(err, `missing control reference ${parts.ref}`).toContain(parts.ref);
  expect(err.toLowerCase(), `missing rule ${parts.rule}`).toContain(
    parts.rule.toLowerCase(),
  );
  expect(
    err.toLowerCase(),
    `missing alternative ${parts.alternative}`,
  ).toContain(parts.alternative.toLowerCase());
  if (parts.override) {
    expect(err, `missing override param ${parts.override}`).toContain(parts.override);
  } else {
    // A gate with no override must not invent one: naming a parameter that
    // does not exist sends the agent chasing a flag it cannot pass.
    expect(err).not.toContain("ignore_control_locks");
    expect(err).not.toContain("ignore_document_protection");
  }
}

/** word/document.xml of the saved package (the .docx is a ZIP). */
async function documentXml(eng: RedlineEngine): Promise<string> {
  const saved = await eng.doc.save();
  return strFromU8(unzipSync(new Uint8Array(saved))["word/document.xml"]);
}

// ---------------------------------------------------------------------------
// A3.1 — content-locked control refuses edits (G1)
// ---------------------------------------------------------------------------

describe("A3.1 content lock (G1)", () => {
  it("refuses edits inside a content-locked control", async () => {
    const err = errorsFor(
      await engine(),
      modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days."),
    );
    assertFourComponents(err, {
      ref: "CC:7",
      rule: "content-locked",
      alternative: "remove the lock",
      override: "ignore_control_locks",
    });
    expect(err).toContain('"Payment Terms"');
    expect(err).toContain("fixed_clause");
  });

  it("lets the edit through with the override", async () => {
    const eng = await engine(undefined, { ignore_control_locks: true });
    expect(
      errorsFor(eng, modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days.")),
    ).toBe("");
  });

  it("discloses the override in the report", async () => {
    // The other half of the override bargain: the caller opted out of a
    // safety rail and the report says so where a human reviewing sees it.
    const eng = await engine(undefined, { ignore_control_locks: true });
    const stats: any = eng.process_batch([
      modify("Payment terms are Net 30 days.", "Payment terms are Net 90 days."),
    ]);
    expect(stats.overrides_note ?? "").toContain("ignore_control_locks");
    expect(stats.overrides_note ?? "").toContain("CC:7");
  });
});

// ---------------------------------------------------------------------------
// A3.2 — group refuses non-field edits, permits nested-field edits (G3)
// ---------------------------------------------------------------------------

describe("A3.2 group region (G3)", () => {
  it("refuses boilerplate edits inside a group", async () => {
    const err = errorsFor(await engine(), modify("must not be modified", "may be modified"));
    assertFourComponents(err, {
      ref: "CC:8",
      rule: "group",
      alternative: "nested",
      override: "ignore_control_locks",
    });
    expect(err).toContain('"Standard Terms"');
  });

  it("keeps a nested field inside a group editable", async () => {
    // The half that is easy to get wrong: over-broad group gating would make
    // the group's own fields uneditable, the opposite of the author's intent.
    expect(
      errorsFor(await engine(), modify("123 Main Street, Ottawa", "1 King Street, Toronto")),
    ).toBe("");
  });
});

// ---------------------------------------------------------------------------
// A3.3 — delete-locked wrapper survives (G2)
// ---------------------------------------------------------------------------

describe("A3.3 delete lock (G2)", () => {
  it("allows emptying a control", async () => {
    // sdtLocked protects the control's EXISTENCE, not its text.
    expect(errorsFor(await engine(), modify("123 Main Street, Ottawa", ""))).toBe("");
  });

  it("leaves the wrapper standing in the XML", async () => {
    const eng = await engine();
    eng.process_batch([modify("123 Main Street, Ottawa", "")]);
    const xml = await documentXml(eng);
    // The wrapper survives as an element even though its content is now a
    // tracked deletion — that is the whole point of G2's narrowness.
    expect(xml).toContain('w:val="sdtLocked"');
    expect(xml).toContain("notice_address");
  });
});

// ---------------------------------------------------------------------------
// A3.4 — readOnly protection blocks everything (G4)
// ---------------------------------------------------------------------------

describe("A3.4 readOnly protection (G4)", () => {
  it("blocks every edit", async () => {
    const err = errorsFor(
      await engine("readOnly"),
      modify("Signed by the parties below.", "Signed below."),
    );
    assertFourComponents(err, {
      ref: "read-only",
      rule: "blocks every modification",
      alternative: "restrict editing",
      override: "ignore_document_protection",
    });
    expect(err).toContain("enforced");
  });

  it("blocks edits inside controls too", async () => {
    // Protection binds regardless of where the edit lands — checked before
    // anything about the control.
    expect(errorsFor(await engine("readOnly"), modify("ACME Corp", "Globex"))).toContain(
      "read-only",
    );
  });

  it("lets the edit through with the override", async () => {
    const eng = await engine("readOnly", { ignore_document_protection: true });
    expect(errorsFor(eng, modify("Signed by the parties below.", "Signed below."))).toBe("");
  });
});

// ---------------------------------------------------------------------------
// A3.5 — forms protection allows exactly the form surface (G5)
// ---------------------------------------------------------------------------

describe("A3.5 forms protection (G5)", () => {
  it("refuses body text outside controls", async () => {
    const err = errorsFor(
      await engine("forms"),
      modify("approved boilerplate", "revised boilerplate"),
    );
    assertFourComponents(err, {
      ref: "fill-in-forms",
      rule: "body text outside a content control is locked",
      alternative: "form field",
      override: "ignore_document_protection",
    });
  });

  it("refuses even the permitted fills by default", async () => {
    // A3.5 as frozen says this edit applies; spec-gates §1a supersedes that.
    //
    // Mikko's 2026-08-21 decision (§1a) added a SECOND gate on the writes Word
    // permits here: under `forms` protection Word records them untracked and
    // reading TrackRevisions throws, so Adeu's "always tracked" contract is
    // unenforceable. Refuse by default, opt in explicitly.
    //
    // A3.5's "(b) and (c) apply" predates that decision and was not restated
    // when §1a landed. The decision is newer and more specific, and §1a is
    // unambiguous that the permitted writes are "additionally gated", so it
    // wins. Flagged in PROGRESS.md and A3.5 updated to match.
    const err = errorsFor(await engine("forms"), modify("ACME Corp", "Globex"));
    assertFourComponents(err, {
      ref: "fill-in-forms",
      rule: "untracked",
      alternative: "remove the protection",
      override: "allow_untracked_writes",
    });
  });

  it("applies the fill with the opt-in", async () => {
    const eng = await engine("forms", { allow_untracked_writes: true });
    expect(errorsFor(eng, modify("ACME Corp", "Globex"))).toBe("");
  });

  it("keeps the two protection params non-interchangeable", async () => {
    // §1a is explicit that these are different admissions: one bypasses a gate
    // the author set, the other accepts a downgrade of Adeu's own guarantee.
    const protectionOnly = await engine("forms", { ignore_document_protection: true });
    expect(errorsFor(protectionOnly, modify("ACME Corp", "Globex"))).toContain("untracked");

    const trackingOnly = await engine("forms", { allow_untracked_writes: true });
    expect(
      errorsFor(trackingOnly, modify("approved boilerplate", "revised")),
    ).toContain("fill-in-forms");
  });
});

// ---------------------------------------------------------------------------
// A3.6 — trackedChanges protection blocks review actions only (G7)
// ---------------------------------------------------------------------------

describe("A3.6 trackedChanges protection (G7)", () => {
  it("permits text edits", async () => {
    // Adeu always writes tracked changes, exactly what this protection permits.
    expect(errorsFor(await engine("trackedChanges"), modify("ACME Corp", "Globex"))).toBe("");
  });

  it("refuses Accept", async () => {
    const eng = await engine("trackedChanges");
    let err = "";
    try {
      eng.process_batch([{ type: "accept", target_id: "Chg:1" }]);
    } catch (e) {
      err = (e as BatchValidationError).errors.join("\n");
    }
    expect(err).toContain("tracked-changes-only");
    expect(err.toLowerCase()).toContain("resolving revisions");
    expect(err).toContain("ignore_document_protection");
  });

  it("does NOT gate review on locks — G9 is allow", async () => {
    // CC-6(d) measured Word permitting Accept/Reject inside sdtContentLocked:
    // the lock stops typing, not review. Gating it would make Adeu stricter
    // than Word and strand revisions the user can resolve in two clicks.
    const eng = await engine();
    let err = "";
    try {
      eng.process_batch([{ type: "accept", target_id: "Chg:404" }]);
    } catch (e) {
      err = (e as BatchValidationError).errors.join("\n");
    }
    // Fails because the id does not exist, NOT because of any lock.
    expect(err.toLowerCase()).not.toContain("lock");
  });
});

// ---------------------------------------------------------------------------
// A3.7 — placeholder ghosts are not editable text (G8)
// ---------------------------------------------------------------------------

describe("A3.7 placeholder ghosts (G8)", () => {
  it("refuses an edit to placeholder text", async () => {
    const err = errorsFor(
      await engine(),
      modify("Click or tap here to enter text.", "Ministry of Example"),
    );
    assertFourComponents(err, {
      ref: "CC:2",
      rule: "placeholder",
      alternative: "set_field",
      override: null,
    });
    expect(err).toContain('"Client Name"');
    // BOTH sanctioned fills, per A3.7.
    expect(err).toContain("{#cc:2}{#/cc:2}");
  });

  it("leaves the XML untouched", async () => {
    // A3.7 pins the XML, not just the refusal: a gate that rejected but had
    // already mutated the ghost run would leave the document worse than if it
    // had done nothing.
    const eng = await engine();
    expect(() =>
      eng.process_batch([modify("Click or tap here to enter text.", "Ministry of Example")]),
    ).toThrow();
    const xml = await documentXml(eng);
    expect(xml).toContain("showingPlcHdr");
    expect(xml).toContain("Click or tap here to enter text.");
    expect(xml).not.toContain("Ministry of Example");
  });
});

// ---------------------------------------------------------------------------
// A3.8 — checkbox tokens accept only the toggle (G11)
// ---------------------------------------------------------------------------

describe("A3.8 checkbox (G11)", () => {
  it("refuses arbitrary text", async () => {
    const err = errorsFor(await engine(), modify("[x]", "yes"));
    assertFourComponents(err, {
      ref: "CC:6",
      rule: "checkbox",
      alternative: "set_field",
      override: null,
    });
    expect(err).toContain("[ ]");
  });

  it("permits the toggle", async () => {
    expect(errorsFor(await engine(), modify("[x]", "[ ]"))).toBe("");
  });
});

// ---------------------------------------------------------------------------
// A3.9 — bound content redirects to set_field (G13)
// ---------------------------------------------------------------------------

describe("A3.9 bound control (G13)", () => {
  it("redirects to set_field", async () => {
    const err = errorsFor(await engine(), modify("M-2026-001", "M-2026-002"));
    assertFourComponents(err, {
      ref: "CC:10",
      rule: "data-bound",
      alternative: "set_field",
      override: null,
    });
    expect(err).toContain('"Matter Number"');
    expect(err).toContain("/root[1]/matter[1]");
  });

  it("has no override", async () => {
    // Unlike the lock gates, no parameter unlocks this: the others refuse what
    // Word would refuse, so overriding accepts Word's verdict. Here the write
    // would appear to succeed and then silently revert on open, and no flag
    // can make the text path keep the store consistent.
    const eng = await engine(undefined, {
      ignore_control_locks: true,
      ignore_document_protection: true,
      allow_untracked_writes: true,
    });
    expect(errorsFor(eng, modify("M-2026-001", "M-2026-002"))).toContain("data-bound");
  });
});

// ---------------------------------------------------------------------------
// A3.10 — boundary auto-segmentation (G14)
// ---------------------------------------------------------------------------

describe("A3.10 boundary segmentation (G14)", () => {
  it("applies an edit whose change stays outside the control", async () => {
    // The changed word is outside CC:3; the unchanged tail crosses into it.
    // trim_common_context narrows the effective range to "Counterparty:"
    // before any gate sees it, so the control is never touched. This is the
    // segmentation A3.10 asks for, already performed by machinery that
    // predates the gates.
    expect(
      errorsFor(
        await engine(),
        modify("Counterparty: {#cc:3}ACME Corp", "Supplier: {#cc:3}ACME Corp"),
      ),
    ).toBe("");
  });

  it("applies a genuine crossing and discloses it", async () => {
    // Here BOTH sides change, so the effective range really does span the
    // wall. Neither side is locked, so the edit is valid and applies — but the
    // report must say it touched text on both sides of the control, because an
    // agent that asked to change "CC:3" and silently got a change half outside
    // it has been told something untrue by omission.
    const eng = await engine();
    const stats: any = eng.process_batch([
      modify("Counterparty: {#cc:3}ACME Corp", "Supplier: {#cc:3}GLOBEX Inc"),
    ]);
    expect(stats.edits_applied).toBe(1);
    const warning = (stats.edits[0]?.warning ?? "").toLowerCase();
    expect(warning).toContain("segmented");
    expect(warning).toContain("cc:3");
  });
});

// ---------------------------------------------------------------------------
// A3.11 — no merges across block-control walls (G15)
// ---------------------------------------------------------------------------

describe("A3.11 merge across control walls (G15)", () => {
  it("refuses a merge out of an anchored block control", async () => {
    // A3.11's own example is caught one layer earlier, by CC-1e's anchor gate:
    // the merge would have to delete {#/cc:1}. That is a MORE precise error
    // than a generic merge refusal, so it is the right one to keep — but it
    // means A3.11 does not, by itself, exercise G15.
    const err = errorsFor(
      await engine(),
      modify(
        "third-party claims.\n{#/cc:1}\n\nThis Agreement is made between",
        "third-party claims and this Agreement is made between",
      ),
    );
    expect(err.toLowerCase()).toContain("anchor");
  });

  it("refuses a merge across an UNANCHORED control wall", async () => {
    // G15's real job. CC:12 and CC:13 are repeating-section items: they are
    // UNANCHORED, so they project no tokens and the anchor gate cannot see
    // them. Without G15 a merge would silently hoist one item's content into
    // the other, and the repeating section would lose an item.
    const err = errorsFor(
      await engine(),
      modify(
        "Initial report, due 2026-02-01.\n\nDeliverable: Final report",
        "Initial report and the final report",
      ),
    );
    expect(err).toContain("CC:12");
    expect(err).toContain("CC:13");
    expect(err.toLowerCase()).toMatch(/merge|hoisted/);
    expect(err.toLowerCase()).toMatch(/two edits|split/);
  });
});
