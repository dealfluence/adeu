/**
 * CC-1d / A1.6 (search half) — anchor tokens survive snippet rendering.
 *
 * Twin of python/tests/test_cc_anchor_chrome_protection.py. The highlight pass
 * already protected `{#...}` tokens from marker stripping (QA 2026-07-23 F4)
 * and that covers CC-1's `{#cc:N}` anchors unchanged. The gap A1.6 exposed is
 * the snippet WINDOW: `balanceSnippetWindow` balanced CriticMarkup but knew
 * nothing about anchors, so a clamped window sliced through one and shipped
 * `{#cc:` — a plausible-looking target that resolves to nothing.
 *
 * This is reachable in production, not theoretical: the radius ladder clamps
 * snippets whenever a result set exceeds the response budget.
 */
import { describe, it, expect } from "vitest";
import { balanceSnippetWindow, emphasizedSnippet } from "./response-builders.js";

const BODY = "Counterparty: {#cc:3}ACME Corp{#/cc:3}.";
const HIT_START = BODY.indexOf("ACME");
const HIT_END = HIT_START + 4;

/** A dangling `{#` with no closing brace. */
const SPLIT_HEAD_RE = /\{#[^}\n]*$/;
/** A stray `}` with no opener ahead of it. */
const SPLIT_TAIL_RE = /^[^{\n]*\}/;

describe("CC-1d / A1.6 — anchors survive search chrome-stripping", () => {
  it.each([2, 4, 6, 8, 12, 20])(
    "clamped window never splits an anchor (radius=%i)",
    (radius) => {
      const [start, end] = balanceSnippetWindow(
        BODY,
        Math.max(0, HIT_START - radius),
        Math.min(BODY.length, HIT_END + radius),
      );
      const fragment = BODY.slice(start, end);
      expect(
        SPLIT_HEAD_RE.test(fragment),
        `split anchor (dangling opener) in ${JSON.stringify(fragment)}`,
      ).toBe(false);
      expect(
        SPLIT_TAIL_RE.test(fragment),
        `orphan closing brace in ${JSON.stringify(fragment)}`,
      ).toBe(false);
      expect(fragment, "widening must never drop the hit itself").toContain("ACME");
    },
  );

  it("widening snaps to the token edge, not the whole line", () => {
    const [start, end] = balanceSnippetWindow(BODY, HIT_START - 2, HIT_START + 6);
    expect(BODY.slice(start, end)).toBe("{#cc:3}ACME C");
  });

  it("a window already clear of the anchors is left alone", () => {
    const plain = "no anchors here at all, just prose";
    expect(balanceSnippetWindow(plain, 3, 10)).toEqual([3, 10]);
  });

  it("the highlight pass keeps the anchor intact", () => {
    const out = emphasizedSnippet("Counterparty: {#cc:3}", "ACME", " Corp{#/cc:3}.");
    expect(out).toBe("Counterparty: {#cc:3}**ACME** Corp{#/cc:3}.");
  });

  it("the highlight pass does not eat an anchor's underscores", () => {
    // The word-edge `_` rule must not pair with the token's own characters.
    const out = emphasizedSnippet("see {#cc:3}_", "ACME", "_{#/cc:3} now");
    expect(out).toContain("{#cc:3}");
    expect(out).toContain("{#/cc:3}");
  });
});
