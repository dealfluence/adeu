// FILE: node/packages/core/src/content_control_classification.test.ts
/**
 * CC-1a — classification and ordinal assignment for the 16-control fixture.
 *
 * Every expectation here is read off the normative listing in
 * `shared/fixtures/fixture-standard.md`; the fixture body
 * itself is the shared `shared/fixtures/cc_fixture.body.xml` that
 * `scripts/make_cc_fixture.py` and the python suite also read, so a change to
 * the fixture cannot silently desynchronise the two engines.
 *
 * The python twin is `python/tests/test_content_control_classification.py` and
 * asserts the same table.
 */

import { describe, it, expect } from "vitest";
import { ccFixtureBodyElement } from "./test-utils.js";
import {
  assignOrdinals,
  classifySdt,
  closeToken,
  isAnchored,
  iterSdtElementsInOrder,
  openToken,
  type SdtInfo,
} from "./utils/content-controls.js";

// [ordinal, class, tag, anchored, flags] — straight from fixture-standard.md.
const EXPECTED: ReadonlyArray<
  readonly [number, string, string | null, boolean, string[]]
> = [
  [1, "richtext", "indemnity", true, []],
  [2, "text", "client_name", true, []],
  [3, "text", "counterparty", true, []],
  [4, "dropdown", "governing_law", true, []],
  [5, "date", "effective_date", true, []],
  [6, "checkbox", "confidential", false, []],
  [7, "text", "fixed_clause", true, ["locked"]],
  [8, "group", "std_terms", true, ["group"]],
  [9, "text", "notice_address", true, []],
  [10, "text", "matter_number", true, ["bound"]],
  [11, "repeating", "deliverables", false, []],
  [12, "repeating-item", null, false, []],
  [13, "repeating-item", null, false, []],
  [14, "text", "cell_role", true, []],
  [15, "richtext", "row_approver", true, []],
  [16, "richtext", "cell_notes", true, []],
];

function fixtureInfos(): SdtInfo[] {
  const body = ccFixtureBodyElement();
  return iterSdtElementsInOrder(body).map((el, i) => classifySdt(el, i + 1));
}

describe("content-control classification (CC-1a)", () => {
  const infos = fixtureInfos();

  it("the fixture has exactly sixteen controls", () => {
    expect(infos.length).toBe(16);
  });

  for (const [ordinal, cls, tag, anchored, flags] of EXPECTED) {
    it(`CC:${ordinal} classifies as ${cls}`, () => {
      const info = infos[ordinal - 1];
      expect(info.ordinal).toBe(ordinal);
      expect(info.cls, `CC:${ordinal} classified ${info.cls}`).toBe(cls);
      expect(info.tag).toBe(tag);
      expect(isAnchored(info), `CC:${ordinal} (${cls}) anchored`).toBe(anchored);
      expect(Array.from(info.flags), `CC:${ordinal} flags`).toEqual(flags);
    });
  }

  it("ordinals are document-ordered and gapless (A1.3)", () => {
    expect(infos.map((i) => i.ordinal)).toEqual(
      Array.from({ length: 16 }, (_, i) => i + 1),
    );
    // The checkbox (CC:6) and the repeating trio (CC:11-13) are un-anchored yet
    // still consume ordinals — the property that makes ordinals stable when a
    // future change starts or stops anchoring a class.
    expect(infos.filter((i) => !isAnchored(i)).map((i) => i.ordinal)).toEqual([
      6, 11, 12, 13,
    ]);
  });

  it("ordinals are stable across independent loads (A1.3)", () => {
    const read = () =>
      Array.from(assignOrdinals([ccFixtureBodyElement()]).values())
        .map((i) => [i.ordinal, i.cls, i.tag] as const)
        .sort((a, b) => a[0] - b[0]);
    expect(read()).toEqual(read());
    expect(read().map((r) => r[0])).toEqual(
      Array.from({ length: 16 }, (_, i) => i + 1),
    );
  });

  it("nested controls are ordered container-first", () => {
    // Pre-order matters: the open token of a container must be able to carry a
    // lower ordinal than anything inside it, or block-level anchor pairs would
    // interleave rather than nest.
    expect(infos[7].cls).toBe("group");
    expect(infos[8].tag).toBe("notice_address");
    expect(infos[10].cls).toBe("repeating");
    expect(infos.slice(11, 13).map((i) => i.cls)).toEqual([
      "repeating-item",
      "repeating-item",
    ]);
  });

  it("distinguishes a delete lock from a content lock", () => {
    // `sdtLocked` alone is delete-locked but editable — ledger-only, no flag.
    const group = infos[7];
    expect(group.deleteLocked).toBe(true);
    expect(
      group.contentLocked,
      "w:lock=sdtLocked must NOT count as content-locked (spec §2)",
    ).toBe(false);
    expect(Array.from(group.flags)).toEqual(["group"]);

    const fixed = infos[6];
    expect(fixed.contentLocked).toBe(true);
    expect(
      fixed.deleteLocked,
      "sdtContentLocked implies delete-locked too",
    ).toBe(true);
    expect(Array.from(fixed.flags)).toEqual(["locked"]);
  });

  it("a richtext containing a control is not anchored (spec §1)", () => {
    expect(infos[7].hasNestedSdt).toBe(true);
    // CC:16 is a plain in-cell richtext with no nested control, so it anchors.
    expect(infos[15].hasNestedSdt).toBe(false);
    expect(isAnchored(infos[15])).toBe(true);
  });

  it("captures dropdown options and the date format", () => {
    expect(infos[3].options.map(([display]) => display)).toEqual([
      "Ontario",
      "British Columbia",
      "Federal",
    ]);
    expect(infos[3].options.map(([, value]) => value)).toEqual([
      "ON",
      "BC",
      "FED",
    ]);
    expect(infos[4].dateFormat).toBe("yyyy-MM-dd");
  });

  it("captures the checkbox state and the binding xpath", () => {
    expect(infos[5].checked).toBe(true);
    expect(infos[9].bound).toBe(true);
    expect(infos[9].bindingXpath).toBe("/root[1]/matter[1]");
    // A control with no binding reports null rather than "" so the ledger can
    // distinguish "not bound" from "bound to nothing".
    expect(infos[2].bound).toBe(false);
    expect(infos[2].bindingXpath).toBe(null);
  });

  it("detects the placeholder state only where declared", () => {
    expect(infos[1].showingPlaceholder, "CC:2 carries w:showingPlcHdr").toBe(true);
    expect(
      infos.filter((i) => i.showingPlaceholder).map((i) => i.ordinal),
    ).toEqual([2]);
  });

  it("renders tokens with the normative flag order", () => {
    expect(openToken(infos[0])).toBe("{#cc:1}");
    expect(closeToken(infos[0])).toBe("{#/cc:1}");
    expect(openToken(infos[6])).toBe("{#cc:7 locked}");
    expect(openToken(infos[7])).toBe("{#cc:8 group}");
    expect(openToken(infos[9])).toBe("{#cc:10 bound}");
  });
});
