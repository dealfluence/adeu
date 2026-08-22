// FILE: node/packages/mcp-server/src/search-budget.test.ts
//
// A2 (Task 12): search paging, snippet clamping and the response-size budget.
// The Node port of python/tests/test_search_paging.py — every case here is a
// behaviour the Python builder already has and the Node builder must match:
// `max_matches`/`match_offset` windowing with GLOBAL match indices, the
// ±120→±16 snippet radius ladder, the CriticMarkup re-attachment that keeps a
// clamped window readable, and the budget pass that drops trailing HITS rather
// than ship an oversized response.

import { describe, it, expect } from "vitest";
import {
  balanceSnippetWindow,
  build_search_response,
  search_budget_tokens,
  type ToolResult,
} from "./response-builders.js";
import { approxTokens, projectFixture } from "./conformance-utils.js";

const textOf = (res: ToolResult): string => res.content[0].text;
const mdOf = (res: ToolResult): string =>
  res.structuredContent!.markdown as string;
const countEntries = (md: string): number =>
  (md.match(/### Match \d+ \(p\d+\)/g) || []).length;

/** `count` one-line paragraphs, one "Supplier" hit each. */
function haystack(count: number): string {
  const lines: string[] = [];
  for (let i = 1; i <= count; i++) {
    lines.push(`Line ${String(i).padStart(2, "0")} names Supplier here.`);
  }
  return lines.join("\n\n");
}

/** 50 paragraphs of 4,000+ chars, one hit each — the ladder's own fixture. */
function longParagraphs(count: number): string {
  const blocks: string[] = [];
  for (let i = 0; i < count; i++) {
    blocks.push("a".repeat(2000) + ` Supplier target ${i} ` + "b".repeat(2000));
  }
  return blocks.join("\n\n");
}

const STRUCK_CLAUSE =
  "which the parties agreed to strike in full during the second round of review " +
  "after counsel raised concerns about its scope, its enforceability, and the " +
  "indemnity it silently imported from the master agreement";

/** ONE projection line carrying `count` tracked deletions (a redlined row). */
function singleLineRedline(count: number): string {
  const cells: string[] = [];
  for (let i = 0; i < count; i++) {
    cells.push(
      `cell ${i} {--old value ${i} ${STRUCK_CLAUSE}--}{>>[Chg:${i} delete] A. Reviewer<<}`,
    );
  }
  return "Row: " + cells.join(" ");
}

const MARKUP_PAIRS: Array<[string, string]> = [
  ["{>>", "<<}"],
  ["{--", "--}"],
  ["{++", "++}"],
  ["{==", "==}"],
];

/**
 * Walks CriticMarkup delimiters in document order: a closer may never appear
 * before its opener, and nothing may still be open at the end. Counting
 * delimiters per pair is NOT enough — one stray closer on the left plus one
 * stray opener on the right balances arithmetically while reading
 * `l1--}` … `{--del`.
 */
function assertMarkupTerminatedInOrder(md: string): void {
  const closer_of = Object.fromEntries(MARKUP_PAIRS);
  const opener_of = Object.fromEntries(
    MARKUP_PAIRS.map(([o, c]) => [c, o] as [string, string]),
  );
  const depth: Record<string, number> = {
    "{>>": 0,
    "{--": 0,
    "{++": 0,
    "{==": 0,
  };
  const token_re = /\{>>|\{--|\{\+\+|\{==|<<\}|--\}|\+\+\}|==\}/g;
  let tok: RegExpExecArray | null;
  while ((tok = token_re.exec(md)) !== null) {
    const token = tok[0];
    if (token in closer_of) {
      depth[token] += 1;
      continue;
    }
    const opener = opener_of[token];
    expect(
      depth[opener],
      `closer \`${token}\` at ${tok.index} has no open \`${opener}\`: ` +
        JSON.stringify(md.slice(Math.max(0, tok.index - 20), tok.index + 20)),
    ).toBeGreaterThan(0);
    depth[opener] -= 1;
  }
  expect(
    Object.values(depth).filter((v) => v !== 0).length,
    `unterminated CriticMarkup: ${JSON.stringify(depth)}`,
  ).toBe(0);
}

function searchDoc(text: string, options: any, query = "Supplier") {
  return mdOf(
    build_search_response(
      text,
      query,
      false,
      true,
      undefined,
      "doc.docx",
      undefined,
      options,
    ),
  );
}

describe("A2: search paging", () => {
  it("1. honours max_matches and names the offset to continue from", () => {
    const md = searchDoc(haystack(50), { max_matches: 5 });
    expect(countEntries(md)).toBe(5);
    expect(md).toContain(
      "> **Note:** Only 5 matches shown (max_matches=5). Continue with `match_offset=5`.",
    );
  });

  it("2. match_offset windows the hit list and keeps GLOBAL match indices", () => {
    const md = searchDoc(haystack(50), { max_matches: 5, match_offset: 5 });
    expect(countEntries(md)).toBe(5);
    for (const idx of [6, 7, 8, 9, 10]) {
      expect(md).toContain(`### Match ${idx} (p1)`);
    }
    expect(md).not.toContain("### Match 5 (p1)");
    expect(md).not.toContain("### Match 11 (p1)");
    // Global index, not window-local: hit 6 is line 06, not line 01.
    expect(md).toContain("Line 06 names **Supplier** here.");
  });

  it("3. a match_offset past the last hit is a note, not an error", () => {
    const md = searchDoc(haystack(50), { match_offset: 99 });
    expect(countEntries(md)).toBe(0);
    expect(md).toContain(
      "> **Note:** No matches in this window (match_offset=99, total matches=50).",
    );
  });

  it("4. max_matches=0 renders nothing and is never rewritten to 20", () => {
    const md = searchDoc(haystack(50), { max_matches: 0 });
    expect(countEntries(md)).toBe(0);
    expect(md).toContain(
      "> **Note:** No matches shown (max_matches=0, total matches=50). " +
        "Pass `max_matches=N` with N >= 1 to see match snippets.",
    );
    // The query itself still reports its totals.
    expect(md).toContain("Found 50 matches");
  });

  it("5. a negative match_offset is coerced to 0", () => {
    const md = searchDoc(haystack(50), { max_matches: 5, match_offset: -7 });
    expect(countEntries(md)).toBe(5);
    expect(md).toContain("### Match 1 (p1)");
    expect(md).toContain("### Match 5 (p1)");
  });
});

describe("A2: snippet clamping", () => {
  const LONG_ONE_HIT = "A".repeat(2000) + " Supplier target " + "B".repeat(2000);

  it("6. clamps a 4,000-char paragraph to ±120 chars with elision markers", () => {
    const md = mdOf(
      build_search_response(
        LONG_ONE_HIT,
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
      ),
    );
    const snippet = md.split("\n").find((line) => line.includes("**Supplier**"));
    expect(snippet, `no snippet line in:\n${md}`).toBeDefined();
    // "> " + "..." on each edge + the two `**` marker pairs + 2*120 + the hit.
    expect(snippet!.length).toBeLessThanOrEqual(2 * 120 + "Supplier".length + 16);
    expect(snippet!.startsWith("> ...")).toBe(true);
    expect(snippet!.endsWith("...")).toBe(true);
    expect(md).not.toContain("A".repeat(200));
    expect(md).not.toContain("B".repeat(200));
  });

  it("6b. keeps an astral character whole when it straddles the window edge", () => {
    // The ±120 window opens at code unit 101 — the LOW half of the U+1F600 at
    // 100..101. Python clamps in code points and emits the whole emoji, so
    // slicing at the raw index here would ship a lone surrogate (U+FFFD once
    // UTF-8 encoded for the wire).
    const body =
      "x".repeat(100) +
      "\u{1F600}" +
      "y".repeat(118) +
      " Supplier clause " +
      "z".repeat(300);
    const md = mdOf(
      build_search_response(
        body,
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
      ),
    );
    expect(md).toContain("..."); // clamped, or this proves nothing
    expect(
      md.match(
        /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/,
      ),
      `unpaired surrogate in snippet: ${JSON.stringify(md)}`,
    ).toBeNull();
    expect(md).toContain("\u{1F600}" + "y".repeat(118));
  });

  it("7. walks the radius ladder down until the whole response fits", () => {
    const res = build_search_response(
      longParagraphs(50),
      "Supplier",
      false,
      true,
      undefined,
      "doc.docx",
    );
    const md = mdOf(res);
    expect(md).toMatch(
      /> \*\*Note:\*\* Snippets trimmed to ±\d+ chars to fit the response size budget\./,
    );
    const rendered = countEntries(md);
    expect(rendered).toBe(20);
    expect(approxTokens(textOf(res))).toBeLessThanOrEqual(
      search_budget_tokens(20, rendered),
    );
  });

  it("8. says so when not even one ±16 snippet fits the budget", () => {
    // The hit sits just before the closer of a 4,000-char bubble, so every
    // rung of the ladder balances back to the opener and drags the whole
    // bubble in: there is no radius at which this entry is payable.
    const body =
      "Intro paragraph. {>>[Chg:1 delete] " +
      "c".repeat(4000) +
      " Supplier remark<<} tail text.";
    const md = mdOf(
      build_search_response(
        body,
        "Supplier remark",
        false,
        true,
        undefined,
        "doc.docx",
        undefined,
        { max_matches: 1 },
      ),
    );
    expect(countEntries(md)).toBe(0);
    expect(md).toContain(
      "> **Note:** No matches shown in this window: not even one ±16-char snippet " +
        "fits the response size budget (max_matches=1, total matches=1). " +
        "Raise `max_matches`, or pass `full_paragraph=true` to read the matching paragraph in full.",
    );
  });

  it("9. drops trailing HITS and keeps the shown count truthful", () => {
    const res = build_search_response(
      singleLineRedline(20),
      "old value",
      false,
      true,
      undefined,
      "doc.docx",
      undefined,
      { max_matches: 20 },
    );
    const md = mdOf(res);
    expect(md).toMatch(
      /> \*\*Note:\*\* Snippets trimmed to ±\d+ chars and trailing matches dropped to fit the response size budget — continue from the `match_offset` above\./,
    );
    // 20 hits share ONE projection line: the entry cannot be dropped, only
    // its trailing hits can.
    expect(countEntries(md)).toBe(1);
    const shown = md.match(/\(\d+ total, (\d+) shown\)/);
    expect(shown, `header does not report a shown count:\n${md.slice(0, 400)}`).not.toBeNull();
    const shown_n = parseInt(shown![1], 10);
    expect(shown_n).toBeGreaterThan(0);
    expect(shown_n).toBeLessThan(20);
    expect(md).toContain(`match_offset=${shown_n}`);
    expect(approxTokens(textOf(res))).toBeLessThanOrEqual(
      search_budget_tokens(20, shown_n),
    );
    assertMarkupTerminatedInOrder(md);
  });

  it("9b. joins two distant hits in one paragraph with the interior marker", () => {
    // Both hits share a projection line but sit 1,000 chars apart, so the
    // entry renders two windows joined by " ... ", not the text between them.
    const md = mdOf(
      build_search_response(
        `${"A".repeat(300)} Supplier clause one ${"M".repeat(1000)} Supplier clause two ${"Z".repeat(300)}`,
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
      ),
    );
    expect(countEntries(md)).toBe(1);
    expect(md).toContain(" ... ");
    expect(md).not.toContain("M".repeat(500));
    expect(md).toContain("**Supplier** clause one");
    expect(md).toContain("**Supplier** clause two");
  });

  it("10. full_paragraph=true renders whole paragraphs and never trims", () => {
    const md = mdOf(
      build_search_response(
        LONG_ONE_HIT,
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
        undefined,
        { full_paragraph: true },
      ),
    );
    expect(md).toContain("A".repeat(200));
    expect(md).toContain("B".repeat(200));
    expect(md).not.toContain("Snippets trimmed to ±");
  });
});

describe("A2: CriticMarkup safety inside a clamped window", () => {
  it("11a. keeps a multi-line bubble whole, both halves of it", () => {
    const body =
      "Intro paragraph with a bubble {>>[Chg:1 delete] comment bubble\n" +
      "continues on the next line<<} " +
      "K".repeat(30) +
      " Supplier target clause " +
      "L".repeat(200);
    const md = mdOf(
      build_search_response(
        body,
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
      ),
    );
    assertMarkupTerminatedInOrder(md);
    expect(md).toContain("{>>[Chg:1 delete] comment bubble");
    expect(md).toContain("continues on the next line<<}");
    // Widening onto the previous line still elides that line's head.
    expect(md).not.toContain("Intro paragraph");
    expect(md).toContain("...{>>");
  });

  it("11b. re-attaches the tags and change id of an enclosing deletion", () => {
    const body =
      "{--" +
      "D".repeat(400) +
      " Supplier obligations clause " +
      "E".repeat(400) +
      "--}{>>[Chg:7 delete] Reviewer removed the supplier clause<<}";
    const md = mdOf(
      build_search_response(
        body,
        "Supplier obligations",
        false,
        true,
        undefined,
        "doc.docx",
        undefined,
        { max_matches: 5 },
      ),
    );
    expect(md).toContain("..."); // clamped, or this proves nothing
    expect(md).not.toContain("D".repeat(300));
    expect(md).toContain("...{--D");
    expect(md).toContain("--}{>>[Chg:7 delete] ...<<}");
    assertMarkupTerminatedInOrder(md);
  });

  it("11c. balances left only to an opener that fits before the window", () => {
    // Python's `body.rfind(opener, 0, start)` requires the opener to fit
    // ENTIRELY before `start`; JS `lastIndexOf`'s second argument is the
    // START of the match, so the port must subtract the token length.
    expect(balanceSnippetWindow("ab{--cd--}ef", 3, 12)).toEqual([3, 12]);
    expect(balanceSnippetWindow("ab{--cd--}ef", 5, 12)).toEqual([2, 12]);
  });
});

describe("A2: headers, budgets and page validation", () => {
  it("12. a default search on long_5pages costs <= 1500 tokens", async () => {
    const fx = await projectFixture("long_5pages");
    const res = build_search_response(
      fx.text,
      "Confidential Information",
      false,
      true,
      undefined,
      fx.filePath,
      fx.bundle,
      { max_matches: 20 },
    );
    expect(approxTokens(textOf(res))).toBeLessThanOrEqual(1500);
  });

  it("13. header carries (N total, M shown) only when the window is partial", () => {
    const partial = mdOf(
      build_search_response(
        haystack(50),
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
        undefined,
        { max_matches: 5 },
      ),
    );
    expect(partial).toContain(
      "> **Search Results** — Found 50 matches for query `Supplier` in `doc.docx` (50 total, 5 shown).",
    );

    // Whole hit list rendered from offset 0: the plain variant.
    const whole = mdOf(
      build_search_response(
        haystack(3),
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
      ),
    );
    expect(whole).toContain(
      "> **Search Results** — Found 3 matches for query `Supplier` in `doc.docx`.",
    );
    expect(whole).not.toContain("shown)");

    // Tail of the list: nothing is left, but match_offset > 0 still gets the
    // counted variant so the agent can see where it is.
    const tail = mdOf(
      build_search_response(
        haystack(6),
        "Supplier",
        false,
        true,
        undefined,
        "doc.docx",
        undefined,
        { max_matches: 3, match_offset: 3 },
      ),
    );
    expect(tail).toContain(
      "> **Search Results** — Found 6 matches for query `Supplier` in `doc.docx` (6 total, 3 shown).",
    );
    expect(tail).not.toContain("Continue with `match_offset=");
  });

  it("14. rejects a page RANGE string in search mode", () => {
    expect(() =>
      build_search_response(
        haystack(3),
        "Supplier",
        false,
        true,
        "2-4",
        "doc.docx",
      ),
    ).toThrow(
      "Invalid page value: '2-4'. In search mode, `page` must be omitted " +
        "(search all pages), `'all'`, or a positive integer document page number.",
    );
  });
});
