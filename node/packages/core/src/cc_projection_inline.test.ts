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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import { DocumentMapper } from "./mapper.js";

const GHOST = "Click or tap here to enter text.";

/**
 * The normative golden block from the frozen acceptance fixture.
 *
 * The CRLF normalisation is load-bearing on Windows and is why this helper
 * cannot use `readFileSync`'s output directly. Git checks the file out with
 * CRLF there, so the fence in `\n```\n` never matches, `exec` returns null and
 * both golden tests die in `m![1]` with a bare TypeError — a failure that looks
 * like a projection bug and is not one. The python twin has no such problem
 * because `Path.read_text()` does universal-newline translation; node's
 * `readFileSync` hands back the bytes as they are. Normalising also keeps the
 * extracted golden comparable with a projection, which is always LF.
 */
function golden(section: string): string {
  const md = readFileSync(
    resolve(__dirname, "../../../../specs/content-controls/acceptance/fixture-standard.md"),
    "utf-8",
  ).replace(/\r\n/g, "\n");
  const m = new RegExp(`## ${section}[\\s\\S]*?\\n\`\`\`\\n([\\s\\S]*?)\`\`\``).exec(md);
  if (!m) throw new Error(`golden section not found: ${section}`);
  return m[1].replace(/\n+$/, "");
}

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
    expect(raw).toEqual(["1","2","3","4","5","7","8","9","10","14","15","16"]);
    expect(raw).toEqual([...raw].sort((a, b) => Number(a) - Number(b)));
  });

  it("A1.1 — raw view matches GOLDEN-RAW", async () => {
    // Now exact for all 16 controls. Until CC-1c this carried a substitution
    // for CC:6, which still projected the raw glyph; the golden always
    // expected the token, and the code caught up.
    expect((await project(false)).replace(/\n+$/, "")).toBe(
      golden("GOLDEN-RAW"),
    );
  });

  it("A1.2 — clean view matches GOLDEN-CLEAN", async () => {
    const expected = golden("GOLDEN-RAW")
      .replace(
        `{#cc:2}{>>placeholder: ${GHOST}<<}{#/cc:2}`,
        "{#cc:2}{#/cc:2}",
      );
    const clean = await project(true);
    expect(clean.replace(/\n+$/, "")).toBe(expected);
    expect(clean).toContain(golden("GOLDEN-CLEAN"));
  });

  it("a block-level control anchors on its own lines (spec §3)", async () => {
    expect(await project(false)).toContain(
      "{#cc:1}\nThe Supplier shall indemnify the Client against all " +
        "third-party claims.\n{#/cc:1}",
    );
  });

  it("a group wraps its blocks and the nested control keeps its anchor", async () => {
    expect(await project(false)).toContain(
      "{#cc:8 group}\n" +
        "These standard terms are approved boilerplate and must not be modified.\n\n" +
        "Notices to: {#cc:9}123 Main Street, Ottawa{#/cc:9}\n" +
        "{#/cc:8}",
    );
  });

  it("table controls anchor inline, never on token lines (spec §3)", async () => {
    // A row is one projected line: token lines would break the "|" grammar and
    // desynchronise the column count.
    const raw = await project(false);
    expect(raw).toContain("Role | {#cc:14}Contracting Officer{#/cc:14}");
    expect(raw).toContain("{#cc:15}Approver | Jane Roe{#/cc:15}");
    // CC:16 is a BLOCK-level control that happens to sit in a cell: inline.
    expect(raw).toContain("Notes | {#cc:16}Approved without conditions.{#/cc:16}");
    expect(raw, "in-cell block control must not emit token lines").not.toContain(
      "{#cc:16}\n",
    );
  });

  it("the GFM divider survives between anchored rows", async () => {
    // Regression for the golden defect corrected on 2026-08-21: GOLDEN-RAW
    // originally omitted this line.
    expect(await project(false)).toContain(
      "Role | {#cc:14}Contracting Officer{#/cc:14}\n--- | ---\n",
    );
  });
});
