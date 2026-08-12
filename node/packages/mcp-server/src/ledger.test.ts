// FILE: node/packages/mcp-server/src/ledger.test.ts
//
// Unit contract for the changes ledger (mode='changes'), ported from
// python/src/adeu/mcp_components/_response_builders.py:1372-1815. The 13 cases
// below are the plan's acceptance list (docs/plans/2026-08-12-agent-efficiency-node.md
// Task 4); the byte-for-byte parity with the Python goldens is pinned separately
// by conformance.test.ts's `ledger_*` cases.
//
// Fixture-driven cases project the shared conformance fixtures exactly as the
// server does (`_extractTextFromDoc(doc, false, false)`); shape cases use the
// smallest hand-written CriticMarkup body that exercises the rule, because the
// snippet scan looks at the WHOLE preceding body — one fixture per fallback rule
// would otherwise leak wrappers between cases.

import { describe, it, expect } from "vitest";
import { build_changes_response } from "./ledger.js";
import { approxTokens, projectFixture } from "./conformance-utils.js";

const FP = "/fixtures/x.docx";

/** The ledger's LLM text (line 1 is the File Path banner). */
const textOf = (r: { content: { text: string }[] }) => r.content[0].text;

/** The entry lines only — everything after the three-line header block. */
function entryLines(text: string): string[] {
  return text
    .split("\n")
    .filter((l) => l.startsWith("Chg:") || l.startsWith("Com:"));
}

describe("build_changes_response — header", () => {
  it("1. renders exactly three header lines: counts, distribution, authors", async () => {
    const fx = await projectFixture("multi_author");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    const text = textOf(res);

    expect(text.split("\n").slice(0, 5).join("\n")).toBe(
      [
        `> **File Path:** \`${res.structuredContent.file_path}\``,
        "",
        "> **Changes ledger** — 13 change(s), 0 comment(s) across 1 page(s).",
        "> Distribution — p1: 13",
        "> Authors — Acme LLP, Bob Smith, Jane Doe",
      ].join("\n"),
    );
    // structuredContent carries the same body without the path banner.
    expect(res.structuredContent.markdown).toBe(text.split("\n").slice(2).join("\n"));
    expect(res.structuredContent.title).toBe("multi_author.docx");
  });

  it("1b. falls back to `none`/`None` when nothing matches", () => {
    const res = build_changes_response("A clean paragraph with no markup.", FP);
    const text = textOf(res);

    expect(text).toContain("> **Changes ledger** — 0 change(s), 0 comment(s) across 1 page(s).");
    expect(text).toContain("> Distribution — none");
    expect(text).toContain("> Authors — None");
  });

  it("1c. sorts the author roster ascending, unicode included", async () => {
    const fx = await projectFixture("unicode");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    expect(textOf(res)).toContain("> Authors — Åsa Öberg");
  });
});

describe("build_changes_response — entry line shapes", () => {
  it("2. change line: two-space fields, quoted snippet, 48-char clamp, whitespace collapsed", () => {
    const long = "The Provider shall maintain commercial general liability insurance";
    expect(long.length).toBeGreaterThan(48);
    const body =
      `Rate is {--${long}--}{++short++}` +
      "{>>[Chg:12 delete] Jane Doe (pairs with Chg:13)\n[Chg:13 insert] Jane Doe (pairs with Chg:12)<<} USD.";

    const lines = entryLines(textOf(build_changes_response(body, FP)));
    expect(lines).toEqual([
      `Chg:12  del  Jane Doe  p1  "${long.slice(0, 45)}..."  (pairs Chg:13)`,
      'Chg:13  ins  Jane Doe  p1  "short"  (pairs Chg:12)',
    ]);

    const collapsed = build_changes_response(
      "x {--alpha   beta\n\tgamma--}{>>[Chg:1 delete] Jane Doe<<}",
      FP,
    );
    expect(entryLines(textOf(collapsed))).toEqual([
      'Chg:1  del  Jane Doe  p1  "alpha beta gamma"',
    ]);
  });

  it("3. comment line: author, page, quoted text, reply suffix, 120-char clamp", () => {
    const body =
      "The parties {==confer==}{>>[Com:5] Bob Smith (reply to Com:4): text<<} first.";
    expect(entryLines(textOf(build_changes_response(body, FP)))).toEqual([
      'Com:5  Bob Smith  p1  "text"  (reply to Com:4)',
    ]);

    const long = "z".repeat(130);
    const clamped = build_changes_response(
      `a {==b==}{>>[Com:7] Bob Smith @ 2026-01-01T00:00:00Z: ${long}<<}`,
      FP,
    );
    expect(entryLines(textOf(clamped))).toEqual([
      `Com:7  Bob Smith  p1  "${"z".repeat(117)}..."`,
    ]);
  });
});

describe("build_changes_response — classification and pairing", () => {
  it("4. explicit insert|delete|format tags win over the wrapper fallback", () => {
    // A {--del--} wrapper precedes, yet the tag says insert.
    const res = build_changes_response("x {--old--}{>>[Chg:1 insert] Jane Doe<<}", FP);
    expect(entryLines(textOf(res))[0]).toContain("Chg:1  ins  ");

    // Untyped tags fall back to del > fmt > ins, judged on the wrappers seen so
    // far — one body per rule, since the scan spans the whole preceding body.
    const untyped = (body: string) => entryLines(textOf(build_changes_response(body, FP)))[0];
    expect(untyped("x {--old--}{>>[Chg:1] Jane Doe<<}")).toContain("Chg:1  del  ");
    expect(untyped("x {==styled==}{>>[Chg:2] Jane Doe<<}")).toContain("Chg:2  fmt  ");
    expect(untyped("x {++added++}{>>[Chg:3] Jane Doe<<}")).toContain("Chg:3  ins  ");
    expect(untyped("x plain text{>>[Chg:4] Jane Doe<<}")).toContain("Chg:4  ins  ");
  });

  it("5. pairing is symmetric and partners render sorted numerically", () => {
    // Only Chg:12's rest names the pair; Chg:13 must still list it back.
    const res = build_changes_response(
      "x {--old--}{++new++}{>>[Chg:12 delete] Jane Doe (pairs with Chg:13)\n[Chg:13 insert] Jane Doe<<}",
      FP,
    );
    const lines = entryLines(textOf(res));
    expect(lines[0]).toContain("(pairs Chg:13)");
    expect(lines[1]).toContain("(pairs Chg:12)");

    const multi = build_changes_response(
      "x {--old--}{>>[Chg:5 delete] Jane Doe (pairs with Chg:10, Chg:7)<<}",
      FP,
    );
    expect(entryLines(textOf(multi))[0]).toContain("(pairs Chg:7, Chg:10)");
  });
});

describe("build_changes_response — filters", () => {
  const PAIRED_BODY =
    "x {--old--}{++new++}{>>[Chg:1 delete] Jane Doe (pairs with Chg:2)\n[Chg:2 insert] Jane Doe (pairs with Chg:1)<<}";

  it("6. existing_change_ids drops dead ids and materialises live ones", () => {
    const dropped = build_changes_response(PAIRED_BODY, FP, {
      existing_change_ids: ["1"],
    });
    expect(entryLines(textOf(dropped))).toEqual([
      'Chg:1  del  Jane Doe  p1  "old"',
    ]);

    const added = build_changes_response(PAIRED_BODY, FP, {
      existing_change_ids: ["1", "2", "77"],
    });
    const lines = entryLines(textOf(added));
    expect(lines).toHaveLength(3);
    expect(lines[2]).toBe('Chg:77  del  Unknown  p1  ""');
  });

  it("7. author_filter matches case-insensitive substrings and shrinks the header", async () => {
    const fx = await projectFixture("multi_author");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
      author_filter: "bob",
    });
    const text = textOf(res);

    expect(text).toContain("> **Changes ledger** — 2 change(s), 0 comment(s) across 1 page(s).");
    expect(text).toContain("> Distribution — p1: 2");
    expect(text).toContain("> Authors — Bob Smith");
    expect(entryLines(text)).toEqual([
      'Chg:9  del  Bob Smith  p1  "thirtyforty"  (pairs Chg:10)',
      'Chg:10  ins  Bob Smith  p1  "thirty-five"  (pairs Chg:9)',
    ]);
  });

  it("8. page selects a single page, a range, everything, or refuses out of range", async () => {
    const dense = await projectFixture("dense_175");
    const call = (page: number | string | null | undefined) =>
      build_changes_response(dense.text, dense.filePath, {
        comments_data: dense.commentsData,
        existing_change_ids: dense.changeIds,
        bundle: dense.bundle,
        page,
      });

    const single = entryLines(textOf(call(2)));
    expect(single).toHaveLength(42);
    expect(single.every((l) => / p2 /.test(l))).toBe(true);

    const range = entryLines(textOf(call("2-4")));
    expect(range.every((l) => / p[234] /.test(l))).toBe(true);
    expect(range).toHaveLength(42 + 40 + 40);

    expect(entryLines(textOf(call("all")))).toHaveLength(300);
    expect(entryLines(textOf(call(undefined)))).toHaveLength(300);

    const long5 = await projectFixture("long_5pages");
    const outOfRange = (page: number | string) =>
      build_changes_response(long5.text, long5.filePath, { bundle: long5.bundle, page });
    expect(() => outOfRange(9)).toThrow("Page 9 out of range (doc has 5 pages).");
    expect(() => outOfRange("9-10")).toThrow("Page 9 out of range (doc has 5 pages).");
  });
});

describe("build_changes_response — paging and budget", () => {
  it("9. caps a page at 300 entries and names the next offset", async () => {
    const fx = await projectFixture("dense_175");
    const call = (options: Record<string, unknown>) =>
      textOf(
        build_changes_response(fx.text, fx.filePath, {
          comments_data: fx.commentsData,
          existing_change_ids: fx.changeIds,
          bundle: fx.bundle,
          ...options,
        }),
      );

    const first = call({ offset: 0 });
    expect(entryLines(first)).toHaveLength(300);
    expect(first).toContain(
      '> **Showing entries 1-300 of 350.** Continue with `read_docx(file_path="/fixtures/dense_175.docx", ' +
        'mode="changes", changes_offset=300)`.',
    );

    // A negative offset is clamped to 0, not wrapped around.
    expect(call({ offset: -5 })).toBe(first);

    const second = call({ offset: 300 });
    expect(entryLines(second)).toHaveLength(50);
    expect(second).not.toContain("Showing entries");

    // Argument order: file_path, mode, changes_author, page, changes_offset.
    expect(call({ offset: 0, author_filter: "Reviewer", page: "all" })).toContain(
      '> **Showing entries 1-300 of 350.** Continue with `read_docx(file_path="/fixtures/dense_175.docx", ' +
        'mode="changes", changes_author="Reviewer", page="all", changes_offset=300)`.',
    );
  });

  it("10. table-cell revisions are ordinary entries carrying the cell's page", async () => {
    const fx = await projectFixture("tables_cells");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    expect(entryLines(textOf(res))).toEqual([
      'Chg:1  del  Jane Doe  p1  "40,000"  (pairs Chg:2)',
      'Chg:2  ins  Jane Doe  p1  "36,500"  (pairs Chg:1)',
    ]);
  });

  it("11. costs at most 18 tokens per change on dense_175", async () => {
    const fx = await projectFixture("dense_175");
    const text = textOf(
      build_changes_response(fx.text, fx.filePath, {
        comments_data: fx.commentsData,
        existing_change_ids: fx.changeIds,
        bundle: fx.bundle,
      }),
    );
    const changes = entryLines(text).filter((l) => l.startsWith("Chg:")).length;
    expect(changes).toBe(300);
    expect(approxTokens(text) / changes).toBeLessThanOrEqual(18);
  });
});

describe("build_changes_response — ordering and no_chrome", () => {
  it("12. sorts by position, then changes before comments, then numeric id", async () => {
    const fx = await projectFixture("multi_author");
    const res = build_changes_response(fx.text, fx.filePath, {
      comments_data: fx.commentsData,
      existing_change_ids: fx.changeIds,
      bundle: fx.bundle,
    });
    expect(entryLines(textOf(res)).map((l) => l.split("  ")[0])).toEqual([
      "Chg:1",
      "Chg:2",
      "Chg:9",
      "Chg:10",
      "Chg:3",
      "Chg:4",
      "Chg:11",
      "Chg:12",
      "Chg:5",
      "Chg:6",
      "Chg:7",
      "Chg:8",
      "Chg:901",
    ]);

    // Same bubble, so same position: "chg" < "com" breaks the tie.
    const mixed = build_changes_response(
      "x {--old--}{>>[Chg:9 delete] Jane Doe\n[Com:2] Bob Smith: note<<}",
      FP,
    );
    expect(entryLines(textOf(mixed)).map((l) => l.split("  ")[0])).toEqual(["Chg:9", "Com:2"]);
  });

  it("13. no_chrome emits entry lines only, and bare counts when there are none", () => {
    const body =
      "x {--old--}{++new++}{>>[Chg:1 delete] Jane Doe (pairs with Chg:2)\n[Chg:2 insert] Jane Doe (pairs with Chg:1)<<}";

    const stripped = textOf(build_changes_response(body, FP, { no_chrome: true }));
    expect(stripped).toBe(
      ['Chg:1  del  Jane Doe  p1  "old"  (pairs Chg:2)', 'Chg:2  ins  Jane Doe  p1  "new"  (pairs Chg:1)'].join("\n"),
    );
    expect(stripped).not.toContain("> **");

    // Nothing to list: the counts are the whole answer, never "".
    expect(textOf(build_changes_response("clean text", FP, { no_chrome: true }))).toBe(
      "0 change(s), 0 comment(s)",
    );
    expect(
      textOf(build_changes_response(body, FP, { no_chrome: true, author_filter: "nobody" })),
    ).toBe("0 change(s), 0 comment(s)");
  });
});

describe("build_changes_response — document-derived ids", () => {
  it("14. never resolves a comment id through Object.prototype", () => {
    // Python's `comments_data.get("toString")` misses and the bubble is parsed;
    // a plain JS index would hand back `Object.prototype.toString`.
    const proto = build_changes_response("The parties {>>[Com:toString] Bob Smith: hi<<} confer.", FP, {
      comments_data: { "5": { author: "Jane Doe", text: "unrelated" } },
    });
    expect(entryLines(textOf(proto))).toEqual([
      'Com:toString  Bob Smith  p1  "hi"',
      'Com:5  Jane Doe  p1  "unrelated"',
    ]);

    expect(
      entryLines(
        textOf(
          build_changes_response("The parties {>>[Com:__proto__] Bob Smith: hi<<} confer.", FP, {
            comments_data: { "5": { author: "Jane Doe", text: "unrelated" } },
          }),
        ),
      )[0],
    ).toBe('Com:__proto__  Bob Smith  p1  "hi"');

    // A real own entry with that name still wins over the bubble.
    const own = build_changes_response("The parties {>>[Com:toString] Bob Smith: hi<<} confer.", FP, {
      comments_data: { toString: { author: "Jane Doe", text: "from the comments part" } },
    });
    expect(entryLines(textOf(own))).toEqual([
      'Com:toString  Jane Doe  p1  "from the comments part"',
    ]);
  });

  it("15. reads Unicode ids and digits, as Python's `\\w`/`int()` do", () => {
    // Arabic-Indic 12 sorts as 12, so the ASCII 2 declared after it comes first.
    const unicode_digits = build_changes_response(
      "x {--old--}{++new++}{>>[Chg:\u0661\u0662 delete] Jane Doe\n[Chg:2 insert] Jane Doe<<}",
      FP,
    );
    expect(entryLines(textOf(unicode_digits))).toEqual([
      'Chg:2  ins  Jane Doe  p1  "new"',
      'Chg:\u0661\u0662  del  Jane Doe  p1  "old"',
    ]);

    // A tag the regex misses is swallowed into the preceding entry's author.
    const trailing = build_changes_response(
      "x {--old--}{++new++}{>>[Chg:2 insert] Jane Doe\n[Chg:\u0661\u0662 delete] Jane Doe<<}",
      FP,
    );
    expect(entryLines(textOf(trailing))).toEqual([
      'Chg:2  ins  Jane Doe  p1  "new"',
      'Chg:\u0661\u0662  del  Jane Doe  p1  "old"',
    ]);

    // Pair ids, letters, and reply ids are the same `\w` class.
    const paired = build_changes_response(
      "x {--old--}{++new++}{>>[Chg:\u0661\u0662 delete] Jane Doe (pairs with Chg:\u0663)\n[Chg:\u0663 insert] Jane Doe<<}",
      FP,
    );
    expect(entryLines(textOf(paired))).toEqual([
      'Chg:\u0663  ins  Jane Doe  p1  "new"  (pairs Chg:\u0661\u0662)',
      'Chg:\u0661\u0662  del  Jane Doe  p1  "old"  (pairs Chg:\u0663)',
    ]);

    expect(
      entryLines(textOf(build_changes_response("x {--old--}{>>[Chg:Änderung delete] Åsa Öberg<<}", FP))),
    ).toEqual(['Chg:Änderung  del  Åsa Öberg  p1  "old"']);

    expect(
      entryLines(
        textOf(
          build_changes_response(
            "The parties {==confer==}{>>[Com:\u0665] Bob Smith (reply to Com:\u0664): text<<} first.",
            FP,
          ),
        ),
      ),
    ).toEqual(['Com:\u0665  Bob Smith  p1  "text"  (reply to Com:\u0664)']);
  });
});
