// FILE: node/packages/core/src/cc_projection_inline.test.ts
/**
 * CC-1b — inline content-control projection (A1.2 partial, A1.4, A1.10).
 *
 * Covers the inline half of A1: anchored leaf controls, flags, the empty-pair
 * edit surface and the placeholder bubble. Block-level anchors (CC:1), groups
 * (CC:8) and the table controls (CC:14-16) are still transparent and are
 * asserted as such below, so this file records exactly how far CC-1b has got
 * rather than quietly passing on a partial implementation.
 *
 * The python twin is `python/tests/test_cc_projection_inline.py` and asserts
 * the same strings.
 */

import { describe, it, expect } from "vitest";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const GHOST = "Click or tap here to enter text.";

const fixture: Uint8Array = ccFixtureBytes();

const project = (cleanView: boolean) =>
  extractTextFromBuffer(fixture, cleanView, false);

describe("inline content-control projection (CC-1b)", () => {
  for (const cleanView of [false, true]) {
    it(`ingest and the mapper agree on the fixture (clean=${cleanView})`, async () => {
      const projected = await project(cleanView);
      const mapped = new DocumentMapper(
        await DocumentObject.load(fixture),
        cleanView,
      ).full_text;
      expect(mapped).toBe(projected);
    });
  }

  it("renders inline anchors with flags", async () => {
    const text = await project(false);
    expect(text).toContain("Counterparty: {#cc:3}ACME Corp{#/cc:3}.");
    expect(text).toContain("Governing law: {#cc:4}Ontario{#/cc:4}.");
    expect(text).toContain("Effective date: {#cc:5}2026-01-15{#/cc:5}.");
    expect(text).toContain(
      "Fixed clause: {#cc:7 locked}Payment terms are Net 30 days.{#/cc:7}",
    );
    expect(text).toContain("Notices to: {#cc:9}123 Main Street, Ottawa{#/cc:9}");
    expect(text).toContain("Matter number: {#cc:10 bound}M-2026-001{#/cc:10}");
  });

  it("A1.4 — ghost text never projects as body text", async () => {
    // The single worst pre-CC-1 defect: the placeholder run projected like any
    // other run, so a reader could not tell the ghost from a real party name.
    const raw = await project(false);
    expect(raw, "the bubble must still disclose the placeholder").toContain(GHOST);
    expect(raw.split(GHOST).length - 1).toBe(1);
    expect(raw).toContain(`{>>placeholder: ${GHOST}<<}`);
    expect(raw).not.toContain(`between ${GHOST}`);

    const clean = await project(true);
    expect(clean, "clean view must not contain the ghost text at all").not.toContain(
      GHOST,
    );
  });

  it("an empty control is a matchable adjacent pair (spec §3)", async () => {
    // The empty pair is deliberately adjacent and matchable — it is the target
    // a text-first fill resolves against, the `{#cell:paraId}` precedent.
    const raw = await project(false);
    const clean = await project(true);
    expect(raw).toContain(
      `This Agreement is made between {#cc:2}{>>placeholder: ${GHOST}<<}` +
        `{#/cc:2} and the Government of Example.`,
    );
    expect(clean).toContain(
      "This Agreement is made between {#cc:2}{#/cc:2} and the Government of Example.",
    );
  });

  it("anchors persist in the clean view (spec §6)", async () => {
    const clean = await project(true);
    for (const token of ["{#cc:3}", "{#/cc:3}", "{#cc:7 locked}", "{#cc:10 bound}"]) {
      expect(clean).toContain(token);
    }
    expect(clean).not.toContain("{>>placeholder:");
  });

  it("un-anchored classes emit no tokens", async () => {
    const raw = await project(false);
    for (const ordinal of [6, 11, 12, 13]) {
      expect(raw).not.toContain(`{#cc:${ordinal}}`);
      expect(raw).not.toContain(`{#/cc:${ordinal}}`);
    }
    expect(raw).toContain("Deliverable: Initial report, due 2026-02-01.");
  });

  it("ordinals survive into the projection unchanged (A1.3)", async () => {
    const ids = (t: string) =>
      Array.from(t.matchAll(/\{#cc:(\d+)/g)).map((m) => m[1]);
    const raw = ids(await project(false));
    expect(raw).toEqual(ids(await project(true)));
    expect(raw).toEqual(["2", "3", "4", "5", "7", "9", "10"]);
    expect(raw).toEqual([...raw].sort((a, b) => Number(a) - Number(b)));
  });

  it("block and table controls are not yet anchored", async () => {
    // Scope marker for the rest of CC-1b — NOT an endorsement. Block-level
    // (CC:1), group (CC:8) and the three table controls (CC:14-16) still
    // project transparently; their content is visible and correct, only the
    // anchors are missing. Fails loudly the moment block anchors land, forcing
    // the golden comparison to be updated deliberately.
    //
    // Match on the parsed ordinal, not a token prefix: "{#cc:1" is also a
    // prefix of "{#cc:10 bound}", so a substring check silently passes.
    const raw = await project(false);
    const anchored = new Set(
      Array.from(raw.matchAll(/\{#cc:(\d+)[ }]/g)).map((m) => Number(m[1])),
    );
    for (const ordinal of [1, 8, 14, 15, 16]) {
      expect(
        anchored.has(ordinal),
        `CC:${ordinal} now anchors — update this test and the A1.1 golden`,
      ).toBe(false);
    }
    expect(raw).toContain(
      "The Supplier shall indemnify the Client against all third-party claims.",
    );
    expect(raw).toContain("Role | Contracting Officer");
  });
});
