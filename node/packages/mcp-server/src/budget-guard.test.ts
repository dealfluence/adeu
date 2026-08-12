// FILE: node/packages/mcp-server/src/budget-guard.test.ts
//
// A3: an unbounded whole-document read over the response budget is REFUSED,
// and the refusal answers with the page count, the bounded-read recipe and the
// L1 outline at <= 800 approx tokens.
//
// Pinned LIVE over stdio because the guard only exists on the real tool path,
// and because it must fire on EXACTLY one path — mode='full', page='all', no
// `search_query`, `force` unset. A false positive on any other path breaks the
// text round-trip artifact (QA 2026-07-17 F1).
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { approxTokens, fixturePath } from "./conformance-utils.js";
import { startTestServer, TestServer } from "./test-rpc.js";

const REFUSAL = "Refused unbounded full document read";

/** The six recipe lines whole_doc_guard_message() emits in full, never sliced. */
const RECIPE = [
  "Recipe to read bounded sections:",
  "  - One page or a page range: --page 3 / --page 1-5 (MCP page=3 / page='1-5')",
  '  - Find a passage: --search-query "text" (MCP search_query=\'text\')',
  "  - Heading map: --mode outline (MCP mode='outline')",
  "  - Tracked changes ledger: --mode changes (MCP mode='changes')",
  "  - Read it all anyway: --force (MCP force=True)",
];

/** The conformance fixture: 89,564 projected chars over the default 76,000. */
const OVERSIZED = fixturePath("long_5pages");

/**
 * The unicode conformance fixture: a 556-char body plus a 374-char structural
 * appendix — 932 chars of raw projection. A budget BETWEEN the two pins which
 * string the guard measures.
 */
const APPENDIX_DOC = fixturePath("unicode");

/** ~1,700 chars of heading-free prose: over a 1,000-char budget, under 76,000. */
const SMALL_DOC = Array.from(
  { length: 20 },
  (_unused, i) =>
    `Clause ${i + 1}: the parties agree to perform the obligations set out in this clause.`,
);

const textOf = (res: any): string => res.content[0].text;

const readAll = (server: TestServer, file_path: string, args = {}) =>
  server.callTool("read_docx", {
    reasoning: "exercise the response-budget guard",
    file_path,
    mode: "full",
    page: "all",
    ...args,
  });

/**
 * A server whose CHILD process sees `ADEU_MAX_RESPONSE_CHARS=value`: the child
 * inherits this process's env at spawn, and response_budget_limit() reads it
 * per call on the child's side. The variable is restored as soon as the spawn
 * has happened, so it cannot leak into another suite sharing the worker.
 */
async function startWithBudget(
  label: string,
  value: string,
): Promise<TestServer> {
  const previous = process.env.ADEU_MAX_RESPONSE_CHARS;
  process.env.ADEU_MAX_RESPONSE_CHARS = value;
  try {
    return await startTestServer(label);
  } finally {
    if (previous === undefined) delete process.env.ADEU_MAX_RESPONSE_CHARS;
    else process.env.ADEU_MAX_RESPONSE_CHARS = previous;
  }
}

describe("read_docx response-budget guard (A3)", () => {
  let server: TestServer;
  let smallDocPath: string;

  beforeAll(async () => {
    server = await startTestServer("budget_guard");
    smallDocPath = await server.buildDoc(SMALL_DOC);
  }, 30000);

  afterAll(() => server?.stop());

  it("1. refuses an oversized page='all' read with page count, recipe and L1 outline", async () => {
    const res = await readAll(server, OVERSIZED);

    expect(res.isError).toBe(true);
    const text = textOf(res);
    expect(text).toContain(REFUSAL);
    expect(text).toContain("(5 pages)");
    expect(text).toContain("exceeds response budget limit (76,000 chars).");

    const lines = text.split("\n");
    for (const line of RECIPE) expect(lines).toContain(line);

    expect(text).toContain("Outline (L1 Headings):");
    // Byte-exact heading map, INCLUDING the page RANGE on the last article:
    // a heading owns pages up to the one before the next equal-or-higher
    // heading, so Article 5 spans p4-p5 (outline.py:184-194).
    expect(lines.filter((l) => l.startsWith("# Article "))).toEqual([
      "# Article 1 — Definitions (p1)",
      "# Article 2 — Services (p1)",
      "# Article 3 — Fees and Invoicing (p2)",
      "# Article 4 — Confidentiality (p3)",
      "# Article 5 — Term and Termination (p4-p5)",
    ]);
  });

  it("2. keeps the refusal inside the 800 approx-token contract", async () => {
    const res = await readAll(server, OVERSIZED);
    expect(approxTokens(textOf(res))).toBeLessThanOrEqual(800);
  });

  it("3. force=true bypasses the guard and returns the whole body", async () => {
    const res = await readAll(server, OVERSIZED, { force: true });

    expect(res.isError).toBeFalsy();
    const text = textOf(res);
    expect(text).not.toContain(REFUSAL);
    expect(text.length).toBeGreaterThan(76000);
    expect(text).toContain("Article 5");
  });

  it("4. never fires on outline, appendix, changes, or a search query", async () => {
    for (const mode of ["outline", "appendix", "changes"]) {
      const res = await readAll(server, OVERSIZED, { mode });
      expect(textOf(res), `mode='${mode}' was refused`).not.toContain(REFUSAL);
    }

    const searched = await readAll(server, OVERSIZED, {
      search_query: "Confidential Information",
    });
    expect(searched.isError).toBeFalsy();
    expect(textOf(searched)).not.toContain(REFUSAL);
    expect(textOf(searched)).toContain("Search Results");
  });

  it("5. returns the whole body unchanged for a document under the limit", async () => {
    const res = await readAll(server, smallDocPath);

    expect(res.isError).toBeFalsy();
    const text = textOf(res);
    expect(text).not.toContain(REFUSAL);
    expect(text).toContain(SMALL_DOC[0]);
    expect(text).toContain(SMALL_DOC[SMALL_DOC.length - 1]);
    // page='all' stays chrome-free: file-path banner only, no page banner.
    expect(text).not.toContain("synthetic page");
  });

  it("8. refuses a clean_view read with the CLEAN heading map", async () => {
    const res = await readAll(server, OVERSIZED, { clean_view: true });

    expect(res.isError).toBe(true);
    const text = textOf(res);
    expect(text).toContain(REFUSAL);
    // Python fills outline nodes per VIEW (doc_cache.py:155-188), so its
    // clean-view refusal carries a heading map — Node's must too.
    expect(text).toContain("Outline (L1 Headings):");
    expect(
      text.split("\n").filter((l) => l.startsWith("# Article ")).length,
    ).toBe(5);
  });
});

describe("read_docx response-budget guard — ADEU_MAX_RESPONSE_CHARS", () => {
  let tuned: TestServer;
  let smallDocPath: string;

  beforeAll(async () => {
    tuned = await startWithBudget("budget_guard_env", "1000");
    smallDocPath = await tuned.buildDoc(SMALL_DOC);
  }, 30000);

  afterAll(() => tuned?.stop());

  it("6a. a 1,000-char budget trips the guard on a small document", async () => {
    const res = await readAll(tuned, smallDocPath);

    expect(res.isError).toBe(true);
    const text = textOf(res);
    expect(text).toContain(REFUSAL);
    expect(text).toContain("exceeds response budget limit (1,000 chars).");
  });

  it("7. a document with no L1 headings gets no outline section at all", async () => {
    const res = await readAll(tuned, smallDocPath);

    const text = textOf(res);
    expect(text).toContain(REFUSAL);
    expect(text).not.toContain("Outline (L1 Headings):");
    expect(text).not.toContain("(No headings detected)");
    expect(text).not.toContain("No headings");
  });
});

describe("read_docx response-budget guard — structural appendix", () => {
  let tuned: TestServer;

  beforeAll(async () => {
    tuned = await startWithBudget("budget_guard_appendix", "700");
  }, 30000);

  afterAll(() => tuned?.stop());

  it("9. measures the body it returns, not the appendix it withholds", async () => {
    const res = await readAll(tuned, APPENDIX_DOC);

    // 556-char body <= 700 < 932-char raw projection: measuring the raw
    // projection refused a document Python serves, because Python's mode='full'
    // text is projected with include_appendix=False (doc_cache.py:159-164).
    expect(res.isError).toBeFalsy();
    const text = textOf(res);
    expect(text).not.toContain(REFUSAL);
    expect(text).toContain("# Schédule A — Definitions");
    // The payload really is body-only, so the appendix really is not returned.
    expect(text).not.toContain("READONLY_BOUNDARY_START");
  });
});

describe("read_docx response-budget guard — unparseable ADEU_MAX_RESPONSE_CHARS", () => {
  let fallback: TestServer;
  let smallDocPath: string;

  beforeAll(async () => {
    fallback = await startWithBudget("budget_guard_env_bad", "1e3");
    smallDocPath = await fallback.buildDoc(SMALL_DOC);
  }, 30000);

  afterAll(() => fallback?.stop());

  it("6b. an unparseable budget falls back to 76,000 and does not trip", async () => {
    const res = await readAll(fallback, smallDocPath);

    expect(res.isError).toBeFalsy();
    expect(textOf(res)).not.toContain(REFUSAL);
    expect(textOf(res)).toContain(SMALL_DOC[0]);
  });
});
