// FILE: node/packages/mcp-server/src/conformance.test.ts
//
// The cross-engine conformance suite (spec §8.3): for every case, the Node
// builder must emit BYTE-IDENTICAL text to the Python builder's golden, and
// the four spec token budgets must hold on real payloads.
//
// Goldens come from the Python engine (shared/conformance/capture_goldens.py),
// fixtures from shared/conformance/build_fixtures.mjs. Both are committed —
// see shared/conformance/README.md to regenerate.
//
// Which task turns each case green:
//   range_*            → Task 3  (build_page_range_response)
//   ledger_*           → Task 4  (build_changes_response)
//   search_*           → Task 12 (max_matches / match_offset / full_paragraph)
//   guard_long5        → Task 13 (build_budget_guard_message)
//   outline_l1         → Task 17 (outline parity)
//
// GATE: the whole file is skipped unless ADEU_CONFORMANCE is set, so `npm test`
// stays green while the ports land one task at a time. Task 22 REMOVES the
// gate. Run it deliberately with:
//   cd node/packages/mcp-server && ADEU_CONFORMANCE=1 npx vitest run src/conformance.test.ts
//
// Node builders are called below with PYTHON'S positional argument order, with
// Python's keyword arguments in a trailing options object. That is the only
// Node-side coupling in the contract: a port that lands a different shape
// updates its own call site here when it un-gates its case.

import { describe, it, expect } from "vitest";
import { RedlineEngine, shrink_batch_stats, failure_envelope } from "@adeu/core";
import {
  approxTokens,
  golden,
  normalize,
  projectFixture,
  placeholderPath,
} from "./conformance-utils.js";
import * as builders from "./response-builders.js";

/** The task that owns each case, quoted in the failure message. */
const OWNER: Record<string, string> = {
  ledger_multi_author: "Task 4",
  ledger_comments_threads: "Task 4",
  ledger_tables: "Task 4",
  ledger_author_filter: "Task 4",
  ledger_page_filter: "Task 4",
  ledger_dense_offset0: "Task 4",
  ledger_dense_offset300: "Task 4",
  range_2_4: "Task 3",
  range_cap_1_12: "Task 3",
  range_past_end: "Task 3",
  guard_long5: "Task 13",
  search_default: "Task 12",
  search_max2_offset2: "Task 12",
  search_full_paragraph: "Task 12",
  outline_l1: "Task 17",
};

/** The builder, or an assertion failure naming the task that adds it. */
function builder(name: string, caseName: string): (...args: any[]) => any {
  const fn = (builders as Record<string, unknown>)[name];
  if (typeof fn !== "function") {
    expect.fail(
      `${caseName}: ${OWNER[caseName]} — response-builders.ts exports no ${name}() yet`,
    );
  }
  return fn as (...args: any[]) => any;
}

/** Builders return either a ToolResult or (the guard) a bare string. */
function textOf(result: unknown): string {
  if (typeof result === "string") return result;
  const content = (result as { content?: { text?: string }[] }).content;
  const text = content?.[0]?.text;
  if (typeof text !== "string") {
    throw new Error(`builder returned no text content: ${JSON.stringify(result).slice(0, 200)}`);
  }
  return text;
}

/**
 * Compares one case against its golden. A missing golden and a thrown builder
 * are both turned into assertion failures naming the owning task — this suite
 * never skips silently and never reports a crash.
 */
async function expectMatchesGolden(caseName: string, produce: () => unknown): Promise<void> {
  const want = golden(caseName);
  if (want === null) {
    expect.fail(
      `golden missing: shared/conformance/goldens/${caseName}.txt — run ` +
        "`cd python && uv run python ../shared/conformance/capture_goldens.py`",
    );
  }
  let got: string;
  try {
    got = textOf(await produce());
  } catch (err) {
    // A missing builder already failed with its own message (see builder());
    // anything else is a real throw and gets attributed to the owning task.
    if ((err as Error).name === "AssertionError") throw err;
    expect.fail(
      `${caseName}: ${OWNER[caseName]} — the Node builder threw instead of ` +
        `producing a response: ${(err as Error).message}`,
    );
  }
  expect(normalize(got), `${caseName} differs from the Python golden (${OWNER[caseName]})`).toBe(
    want,
  );
}

describe.skipIf(!process.env.ADEU_CONFORMANCE)("conformance: Node builders match the Python goldens", () => {
  // --- ledger (mode='changes') --------------------------------------------

  const ledgerCase = (
    caseName: string,
    fixture: string,
    options: Record<string, unknown> = {},
  ) =>
    it(caseName, async () => {
      const fx = await projectFixture(fixture);
      await expectMatchesGolden(caseName, () =>
        builder("build_changes_response", caseName)(fx.text, fx.filePath, {
          comments_data: fx.commentsData,
          existing_change_ids: fx.changeIds,
          bundle: fx.bundle,
          ...options,
        }),
      );
    });

  ledgerCase("ledger_multi_author", "multi_author");
  ledgerCase("ledger_comments_threads", "comments_threads");
  ledgerCase("ledger_tables", "tables_cells");
  ledgerCase("ledger_author_filter", "multi_author", { author_filter: "Bob Smith" });
  ledgerCase("ledger_page_filter", "dense_175", { page: 2 });
  ledgerCase("ledger_dense_offset0", "dense_175", { offset: 0 });
  ledgerCase("ledger_dense_offset300", "dense_175", { offset: 300 });

  // --- native page ranges (A6) --------------------------------------------

  const rangeCase = (caseName: string, fixture: string, start: number, end: number) =>
    it(caseName, async () => {
      const fx = await projectFixture(fixture);
      await expectMatchesGolden(caseName, () =>
        builder("build_page_range_response", caseName)(
          fx.text,
          start,
          end,
          fx.filePath,
          fx.bundle,
        ),
      );
    });

  rangeCase("range_2_4", "long_5pages", 2, 4);
  rangeCase("range_cap_1_12", "dense_175", 1, 12);
  rangeCase("range_past_end", "long_5pages", 4, 9);

  // --- whole-document budget guard (A3) -----------------------------------

  it("guard_long5", async () => {
    const fx = await projectFixture("long_5pages");
    await expectMatchesGolden("guard_long5", () =>
      builder("build_budget_guard_message", "guard_long5")(fx.text, fx.filePath, {
        doc: fx.doc,
        bundle: fx.bundle,
      }),
    );
  });

  // --- search (B4/B5) -----------------------------------------------------

  // The query seeded into long_5pages by build_fixtures.mjs (101 hits).
  const SEARCH_QUERY = "Confidential Information";

  const searchCase = (caseName: string, options: Record<string, unknown> = {}) =>
    it(caseName, async () => {
      const fx = await projectFixture("long_5pages");
      await expectMatchesGolden(caseName, () =>
        builder("build_search_response", caseName)(
          fx.text,
          SEARCH_QUERY,
          false, // search_regex
          true, // search_case_sensitive
          undefined, // page — search the whole document
          fx.filePath,
          fx.bundle,
          options,
        ),
      );
    });

  searchCase("search_default");
  searchCase("search_max2_offset2", { max_matches: 2, match_offset: 2 });
  searchCase("search_full_paragraph", { max_matches: 3, full_paragraph: true });

  // --- outline ------------------------------------------------------------

  it("outline_l1", async () => {
    const fx = await projectFixture("long_5pages");
    await expectMatchesGolden("outline_l1", () =>
      builder("build_outline_response", "outline_l1")(fx.doc, fx.text, fx.filePath, 1),
    );
  });
});

// ---------------------------------------------------------------------------
// Token budgets (spec §8.3). These hold on the PYTHON goldens and on real Node
// payloads, so they are enforceable before the builders land: a golden
// recapture that blows a budget fails here, and so does a Node port that
// matches a blown golden.
// ---------------------------------------------------------------------------

describe.skipIf(!process.env.ADEU_CONFORMANCE)("conformance: token budgets", () => {
  it("ledger costs <= 18 tokens per change on dense_175", () => {
    const ledger = golden("ledger_dense_offset0");
    if (ledger === null) {
      expect.fail("golden missing: ledger_dense_offset0.txt — run capture_goldens.py");
    }
    // dense_175 carries 350 changes; the first ledger page renders 300.
    const rendered = ledger.split("\n").filter((l) => l.startsWith("Chg:")).length;
    expect(rendered, "ledger_dense_offset0 rendered no entries").toBe(300);
    expect(approxTokens(ledger) / rendered).toBeLessThanOrEqual(18);
  });

  it("the minimal report costs <= 40 tokens per applied edit on a real batch", async () => {
    const fx = await projectFixture("multi_author");
    const engine = new RedlineEngine(fx.doc, "Budget Reviewer");
    const stats = engine.process_batch([
      { type: "modify", target_text: "invoice date", new_text: "invoice issue date" },
      { type: "modify", target_text: "written notice", new_text: "written notice to the other party" },
    ] as any);

    const minimal = shrink_batch_stats(stats);
    expect(Array.isArray(minimal.edits) && minimal.edits.length > 0).toBe(true);
    for (const edit of minimal.edits) {
      // `error` is exempt from the budget (payloads.ts), as in Python.
      const { error: _error, ...budgeted } = edit as Record<string, unknown>;
      const dumped = JSON.stringify(budgeted);
      expect(approxTokens(dumped), `edit over budget (${dumped.length} chars): ${dumped}`).toBeLessThanOrEqual(40);
    }
  });

  it("a real batch failure costs <= 500 tokens", async () => {
    const fx = await projectFixture("multi_author");
    const engine = new RedlineEngine(fx.doc, "Budget Reviewer");
    let errors: string[] = [];
    try {
      engine.process_batch([
        { type: "modify", target_text: "text that is not in this document", new_text: "x" },
        { type: "modify", target_text: "also absent from the document", new_text: "y" },
      ] as any);
      expect.fail("the batch was expected to fail validation");
    } catch (err) {
      errors = ((err as { errors?: string[] }).errors ?? [(err as Error).message]).slice();
    }
    const envelope = failure_envelope(
      "batch_validation_failed",
      errors.map((reason, index) => [index, reason] as [number, string]),
      (errors[0] ?? "").slice(0, 200),
      errors,
    );
    const dumped = JSON.stringify(envelope);
    expect(approxTokens(dumped), `batch failure over budget: ${dumped.length} chars`).toBeLessThanOrEqual(500);
  });

  it("the guard refusal costs <= 800 tokens", () => {
    const guard = golden("guard_long5");
    if (guard === null) {
      expect.fail("golden missing: guard_long5.txt — run capture_goldens.py");
    }
    expect(approxTokens(guard)).toBeLessThanOrEqual(800);
  });
});

// The placeholder path is the single largest source of spurious golden diffs:
// a builder that resolves it to an absolute path fails every case at once, so
// pin it here where the message is unambiguous.
describe("conformance harness", () => {
  it("addresses fixtures by stable placeholder path, never a real one", () => {
    expect(placeholderPath("multi_author")).toBe("/fixtures/multi_author.docx");
  });
});
