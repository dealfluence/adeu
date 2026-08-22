// FILE: node/packages/core/src/cc_span_control_identity.test.ts
/**
 * CC-4 — every projected SPAN knows which content controls enclose it.
 *
 * `cc_run_control_identity.test.ts` pins the run-level half. This file pins
 * the half the gates actually consume: `TextSpan.sdt_stack` and the
 * `control_ranges` / `controls_at` / `controls_intersecting` queries derived
 * from it, over the standard 16-control fixture. Python twin:
 * `python/tests/test_cc_span_control_identity.py`.
 *
 * The two halves are separate because they have different blind spots and
 * only together cover the document. The run walk sees inline controls and
 * misses block ones (a block control wraps whole paragraphs;
 * iter_paragraph_content is never told it opened). The mapper's block cursor
 * sees block controls and misses inline ones (it does not descend into runs).
 * A span concatenates both, and the assertions below are chosen so that
 * either source going missing fails something: CC:7 is inline-only, CC:8 is
 * block-only, and CC:9 is an inline control nested inside a block one, which
 * no single source can produce.
 *
 * This is the control-wall twin of `part_index`, and deliberately so — the
 * OPC part wall is the same shape of problem with a decade of scar tissue
 * behind it (QA 2026-07-18 C1), so the gates inherit its structure rather
 * than invent one.
 */

import { describe, it, expect, beforeAll } from "vitest";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { DocumentMapper } from "./mapper.js";
import type { SdtInfo } from "./utils/content-controls.js";

let mapper: DocumentMapper;

beforeAll(async () => {
  mapper = new DocumentMapper(
    await DocumentObject.load(Buffer.from(ccFixtureBytes())),
  );
});

const ordinals = (infos: SdtInfo[]) => infos.map((i) => i.ordinal);

/** Offset of `needle` in the projection, asserted unique. */
function at(needle: string): number {
  const first = mapper.full_text.indexOf(needle);
  expect(first, `${needle} not in projection`).not.toBe(-1);
  expect(
    mapper.full_text.indexOf(needle, first + 1),
    `${needle} is ambiguous`,
  ).toBe(-1);
  return first;
}

describe("span control identity (CC-4)", () => {
  it("body text outside every control is unenclosed", () => {
    expect(mapper.controls_at(at("SERVICES AGREEMENT (fixture)"))).toEqual([]);
  });

  it("an inline control encloses its own content only", () => {
    // CC:7, the content-locked one A3.1 rejects edits inside.
    expect(ordinals(mapper.controls_at(at("Payment terms are Net 30 days.")))).toEqual([7]);
    // "Fixed clause: " is the run BEFORE the control in the same paragraph.
    // If the inline stack leaked past sdt_end this would also report CC:7,
    // and G1 would refuse edits to ordinary body text.
    expect(mapper.controls_at(at("Fixed clause: "))).toEqual([]);
  });

  it("a block control encloses whole paragraphs", () => {
    // CC:1 is block-level: no run inside it carries CC:1 on Run.sdtStack, so
    // this assertion passes only via the mapper's block cursor.
    expect(ordinals(mapper.controls_at(at("The Supplier shall indemnify")))).toEqual([1]);
  });

  it("nesting reports outermost first", () => {
    // CC:9 (inline) inside CC:8 (block group). Neither source alone can
    // produce this pair, and A3.2 depends on the ORDER: the group is the
    // locked region, the nested leaf is the editable exception.
    expect(ordinals(mapper.controls_at(at("123 Main Street, Ottawa")))).toEqual([8, 9]);
    // Boilerplate in the group but outside the nested leaf: group only.
    // Exactly the text A3.2(a) must reject and A3.2(b) must not.
    expect(ordinals(mapper.controls_at(at("approved boilerplate")))).toEqual([8]);
  });

  it("unanchored controls get ranges too", () => {
    // CC:6 is a checkbox: UNANCHORED, so it projects "[x]" and no {#cc:6}
    // token. A gate reading anchor events would not see it at all.
    //
    // The brackets are virtual chrome and the MARK is the run-backed span, so
    // the control owns offset+1 and not offset. That asymmetry is load-bearing
    // for G11: the toggle edit targets "[x]", whose first character is not
    // inside the control, so G11 cannot be written as a containment test on
    // the target's start offset alone.
    const bracket = at("[x]");
    expect(mapper.controls_at(bracket)).toEqual([]);
    expect(ordinals(mapper.controls_at(bracket + 1))).toEqual([6]);
    expect(ordinals(mapper.controls_intersecting(bracket, 3))).toEqual([6]);
  });

  it("intersecting reports a span that crosses a wall", () => {
    // A3.10's target: starts outside CC:3, ends inside it. This is the query
    // G14 segments on, so it must report the control rather than stay silent.
    const target = "Counterparty: {#cc:3}ACME Corp";
    expect(ordinals(mapper.controls_intersecting(at(target), target.length))).toEqual([3]);
  });

  it("a zero-length range reports nothing", () => {
    // An insertion point is not "inside" anything for lock purposes; the
    // boundary logic owns it. If this returned the enclosing control, every
    // insertion adjacent to a locked control would be refused.
    expect(mapper.controls_intersecting(at("Payment terms are Net 30 days."), 0)).toEqual([]);
  });

  it("control ranges cover exactly the content", () => {
    const range = mapper.control_ranges.find(([, , i]) => i.ordinal === 7)!;
    expect(mapper.full_text.slice(range[0], range[1])).toBe("Payment terms are Net 30 days.");
  });

  it("table cell and row controls are enclosing too", () => {
    // CC:14 (cell-level) and CC:15 (row-level) wrap w:tc / w:tr, which the
    // mapper reaches through _map_table rather than the block or run walks.
    // Both were invisible to the first cut of this field — table controls are
    // the third structural kind and they need their own push site.
    expect(ordinals(mapper.controls_at(at("Contracting Officer")))).toEqual([14]);
    expect(ordinals(mapper.controls_at(at("Jane Roe")))).toEqual([15]);
    // CC:16 is an ordinary block control that merely lives inside a cell; it
    // must NOT pick up a phantom cell wrapper.
    expect(ordinals(mapper.controls_at(at("Approved without conditions.")))).toEqual([16]);
  });

  it("every content-bearing control has a range", () => {
    // A control silently missing from control_ranges is a hole in every gate
    // at once, and the failure mode is permissive, not loud — so assert the
    // whole set, not samples.
    //
    // CC:2 is the sole exclusion and is not an oversight: it is EMPTY, and
    // its placeholder ghost projects as a virtual {>>placeholder: ...<<}
    // bubble rather than as content. It therefore has no content range at
    // all, which is why G8 (A3.7) cannot be a span-intersection gate like its
    // siblings and must match the target against the placeholder text.
    const present = mapper.control_ranges.map(([, , i]) => i.ordinal).sort((a, b) => a - b);
    const expected = Array.from({ length: 16 }, (_, n) => n + 1).filter((n) => n !== 2);
    expect(present).toEqual(expected);
  });
});
