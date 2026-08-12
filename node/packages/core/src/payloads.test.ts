// FILE: src/payloads.test.ts
// Golden tests for the ported payload builders. Mirrors
// python/tests/test_report_minimal.py and
// python/tests/test_response_budget_guard.py: every string these functions
// emit is a contract with an LLM caller, so the assertions pin the text, not
// just its shape.
import { describe, it, expect, afterEach } from "vitest";
import {
  BATCH_ERROR_CODES,
  BATCH_RECOVERY_PROTOCOL,
  FAILED_TARGET_STUB_CAP,
  FUSED_JSON_HINT,
  GUARD_EMITTED_MAX_CHARS,
  MINIMAL_EDIT_TOKEN_BUDGET,
  failure_envelope,
  has_fused_json_marker,
  response_budget_limit,
  shrink_batch_stats,
  whole_doc_guard_message,
} from "./index.js";

/** The report budget unit: approx tokens over serialized JSON. */
const approx_tokens = (text: string): number => Math.floor(text.length / 4);

/** An edit as the budget measures it: every field except the exempt `error`. */
function budgeted_json(edit: Record<string, unknown>): string {
  const rest: Record<string, unknown> = { ...edit };
  delete rest.error;
  return JSON.stringify(rest);
}

// Independent oracle for the balance invariant — deliberately NOT the
// implementation's own regex.
const BUBBLE_RE =
  /\{--[\s\S]*?--\}|\{\+\+[\s\S]*?\+\+\}|\{==[\s\S]*?==\}|\{>>[\s\S]*?<<\}/g;
const DELIMITERS = ["{--", "--}", "{++", "++}", "{==", "==}", "{>>", "<<}"];

function assert_balanced_critic_markup(markup: string): void {
  const outside = markup.replace(BUBBLE_RE, "");
  for (const delim of DELIMITERS) {
    expect(
      outside.includes(delim),
      `orphaned ${delim} in preview: ${markup}`,
    ).toBe(false);
  }
}

/** Every string anywhere in a shrunk payload must respect the invariant. */
function assert_all_strings_balanced(value: unknown): void {
  if (typeof value === "string") {
    assert_balanced_critic_markup(value);
  } else if (Array.isArray(value)) {
    for (const item of value) assert_all_strings_balanced(item);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) assert_all_strings_balanced(item);
  }
}

// A real engine advisory: ~260 chars of prose, over six times the whole
// per-edit budget (the fattest field an applied edit can carry).
const BACKREF_WARNING =
  "new_text contains '$1', which this engine does not expand as a capture-group " +
  "backreference — the literal text '$1' was written into the document. Re-run the " +
  "edit with the intended replacement spelled out, or use a non-regex target_text " +
  "so no expansion is attempted at all.";

const DEEP_HEADING =
  "Article IV Representations and Warranties > Section 4.2 Liability";

/** A match_mode="all" fan-out preview: ten bubbles with context between them. */
const FANOUT_MARKUP = Array.from(
  { length: 10 },
  (_unused, i) =>
    `Clause ${i + 1}: The {--Consultant--}{++Contractor++} shall deliver the work product.`,
).join(" | ");

describe("payloads", () => {
  describe("failure_envelope", () => {
    it("builds the envelope and appends the recovery protocol for a batch code", () => {
      expect(
        failure_envelope(
          "batch_validation_failed",
          [[0, "boom"]],
          "Batch rejected.",
        ),
      ).toEqual({
        error: "batch_validation_failed",
        failed: [{ index: 0, reason: "boom" }],
        message: `Batch rejected. ${BATCH_RECOVERY_PROTOCOL}`,
      });
    });

    it("preserves 0-based indices in order for a multi-failure batch", () => {
      const res = failure_envelope(
        "invalid_changes_file",
        [
          [1, "a"],
          [4, "b"],
        ],
        "Nope.",
      );
      expect(res.failed).toEqual([
        { index: 1, reason: "a" },
        { index: 4, reason: "b" },
      ]);
    });

    it("flattens the message: newlines collapse to spaces, empty lines drop", () => {
      const res = failure_envelope(
        "response_budget_exceeded",
        [],
        "First line.\n\n  Second line.  \nThird line.",
      );
      expect(res.message).toBe("First line. Second line. Third line.");
    });

    it("does not append the protocol for a non-batch code", () => {
      const res = failure_envelope(
        "response_budget_exceeded",
        [],
        "Too big.",
      );
      expect(res.message).toBe("Too big.");
      expect(BATCH_ERROR_CODES.has("response_budget_exceeded")).toBe(false);
    });

    it("does not double-suffix a message that already carries the protocol", () => {
      const message = `Batch rejected. ${BATCH_RECOVERY_PROTOCOL}`;
      const res = failure_envelope("batch_validation_failed", [], message);
      expect(res.message).toBe(message);
    });

    it("falls back to the protocol alone when the message is empty", () => {
      const res = failure_envelope("batch_validation_failed", [], "");
      expect(res.message).toBe(BATCH_RECOVERY_PROTOCOL);
    });

    it("includes `errors` only when passed", () => {
      expect("errors" in failure_envelope("x", [], "m")).toBe(false);
      expect(failure_envelope("x", [], "m", ["raw"]).errors).toEqual(["raw"]);
    });

    it("exposes the two batch codes and nothing else", () => {
      expect([...BATCH_ERROR_CODES].sort()).toEqual([
        "batch_validation_failed",
        "invalid_changes_file",
      ]);
    });
  });

  describe("has_fused_json_marker", () => {
    it("detects fused JSON markers", () => {
      expect(has_fused_json_marker("modify}],{comment:")).toBe(true);
      expect(has_fused_json_marker('{"type"')).toBe(true);
      expect(has_fused_json_marker('a":b')).toBe(true);
    });

    it("is false for a clean type string, empty string and non-strings", () => {
      expect(has_fused_json_marker("modify")).toBe(false);
      expect(has_fused_json_marker("")).toBe(false);
      expect(has_fused_json_marker(null)).toBe(false);
      expect(has_fused_json_marker(undefined)).toBe(false);
      expect(has_fused_json_marker(42)).toBe(false);
      expect(has_fused_json_marker({ type: "modify" })).toBe(false);
    });

    it("keeps the hint wording", () => {
      expect(FUSED_JSON_HINT).toContain("fused during generation");
    });
  });

  describe("response_budget_limit", () => {
    const original = process.env.ADEU_MAX_RESPONSE_CHARS;

    afterEach(() => {
      if (original === undefined) delete process.env.ADEU_MAX_RESPONSE_CHARS;
      else process.env.ADEU_MAX_RESPONSE_CHARS = original;
    });

    it("defaults to 76,000 chars", () => {
      delete process.env.ADEU_MAX_RESPONSE_CHARS;
      expect(response_budget_limit()).toBe(76000);
    });

    it("honours ADEU_MAX_RESPONSE_CHARS", () => {
      process.env.ADEU_MAX_RESPONSE_CHARS = "1000";
      expect(response_budget_limit()).toBe(1000);
    });

    it("reads digit-group underscores like Python's int()", () => {
      for (const [raw, expected] of [
        ["1_000", 1000],
        ["+1_0", 10],
        [" 1_000_000 ", 1000000],
      ] as const) {
        process.env.ADEU_MAX_RESPONSE_CHARS = raw;
        expect(response_budget_limit()).toBe(expected);
      }
    });

    it("ignores unparseable and empty values", () => {
      // The underscore forms here are the ones int() rejects too: a separator
      // needs a digit on both sides, and doubling it is an error.
      for (const bad of [
        "",
        "abc",
        "10.5",
        "1e3px",
        "  ",
        "_1",
        "1_",
        "1__0",
        "+_1",
      ]) {
        process.env.ADEU_MAX_RESPONSE_CHARS = bad;
        expect(response_budget_limit()).toBe(76000);
      }
    });
  });

  describe("whole_doc_guard_message", () => {
    const RECIPE = [
      "Recipe to read bounded sections:",
      "  - One page or a page range: --page 3 / --page 1-5 (MCP page=3 / page='1-5')",
      '  - Find a passage: --search-query "text" (MCP search_query=\'text\')',
      "  - Heading map: --mode outline (MCP mode='outline')",
      "  - Tracked changes ledger: --mode changes (MCP mode='changes')",
      "  - Read it all anyway: --force (MCP force=True)",
    ];

    /** The CLI --json emission of a guard message: the largest surface form. */
    const emitted_length = (msg: string): number =>
      JSON.stringify(failure_envelope("response_budget_exceeded", [], msg))
        .length;

    it("renders the head line with thousands separators and estimated tokens", () => {
      const msg = whole_doc_guard_message(
        207000,
        76000,
        "big_document.docx",
        "",
        16,
      );
      expect(msg.split("\n")[0]).toBe(
        "Refused unbounded full document read for 'big_document.docx' (16 pages): " +
          "total size (207,000 chars, ~51,750 tokens) exceeds response budget limit (76,000 chars).",
      );
    });

    it("omits the file and page clauses when not supplied", () => {
      const msg = whole_doc_guard_message(207000, 76000);
      expect(msg.split("\n")[0]).toBe(
        "Refused unbounded full document read: total size (207,000 chars, ~51,750 tokens) " +
          "exceeds response budget limit (76,000 chars).",
      );
    });

    it("carries every recipe line verbatim", () => {
      const msg = whole_doc_guard_message(
        207000,
        76000,
        "big_document.docx",
        "# Heading 1 (p1)",
        16,
      );
      for (const line of RECIPE) {
        expect(msg.split("\n")).toContain(line);
      }
    });

    it("emits no outline section when the document has no L1 headings", () => {
      const msg = whole_doc_guard_message(207000, 76000, "plain.docx", "", 16);
      expect(msg).not.toContain("Outline (L1 Headings):");
      expect(msg).not.toContain("No headings");
    });

    it("keeps the outline section when headings exist", () => {
      const msg = whole_doc_guard_message(
        90000,
        76000,
        "sample.docx",
        "# First L1 Heading (p1)\n# Second L1 Heading (p3)",
        5,
      );
      expect(msg).toContain("Outline (L1 Headings):");
      expect(msg).toContain("# First L1 Heading (p1)");
      expect(msg).toContain("# Second L1 Heading (p3)");
    });

    it("trims whole outline entries to fit the emitted budget and counts the drops", () => {
      const entries = Array.from(
        { length: 400 },
        (_unused, i) =>
          `# Article ${i + 1} — Obligations Of The Parties (p${i + 1})`,
      );
      const msg = whole_doc_guard_message(
        1200000,
        76000,
        "contract.docx",
        entries.join("\n"),
        400,
      );

      expect(emitted_length(msg)).toBeLessThanOrEqual(GUARD_EMITTED_MAX_CHARS);
      const rendered = msg.split("\n").filter((l) => l.startsWith("# "));
      expect(rendered.length).toBeGreaterThan(0);
      // Entries are dropped whole, never sliced mid-line.
      expect(rendered.every((l) => entries.includes(l))).toBe(true);
      expect(msg).toContain(
        `  (${entries.length - rendered.length} more headings: --mode outline / MCP mode='outline')`,
      );
      // The prose and the recipe survive the trim.
      for (const line of RECIPE) expect(msg.split("\n")).toContain(line);
    });

    it("keeps the emitted response in budget for a long smart-quoted path", () => {
      const long_path =
        "C:\\Users\\O’Brien\\" +
        "a_very_long_directory_name\\".repeat(8) +
        "contract_final_v12.docx";
      const entries = Array.from(
        { length: 120 },
        (_unused, i) =>
          `# Article ${i + 1} — Obligations Of The Parties (p${i + 1})`,
      );
      const msg = whole_doc_guard_message(
        1200000,
        76000,
        long_path,
        entries.join("\n"),
        160,
      );
      expect(emitted_length(msg)).toBeLessThanOrEqual(GUARD_EMITTED_MAX_CHARS);
      expect(approx_tokens(msg)).toBeLessThanOrEqual(800);
    });

    it("keeps the tail of an over-long path", () => {
      const long_path = "C:\\" + "d".repeat(300) + "\\contract.docx";
      const msg = whole_doc_guard_message(207000, 76000, long_path, "", 16);
      expect(msg).toContain(`'...${long_path.slice(-160)}'`);
      expect(msg).not.toContain(long_path);
    });

    it("stays inside the 800 approx-token contract", () => {
      const msg = whole_doc_guard_message(
        120000,
        76000,
        "big_document.docx",
        "# Heading 1 (p1)\n# Heading 2 (p5)\n# Heading 3 (p10)",
        12,
      );
      expect(msg.length).toBeLessThanOrEqual(GUARD_EMITTED_MAX_CHARS);
      expect(approx_tokens(msg)).toBeLessThanOrEqual(800);
    });
  });

  describe("shrink_batch_stats", () => {
    it("drops `engine`, keeps `version`, counters and output_path", () => {
      const shrunk = shrink_batch_stats({
        engine: "node",
        version: "2.2.0",
        actions_applied: 1,
        edits_applied: 1,
        output_path: "out.docx",
      });
      expect("engine" in shrunk).toBe(false);
      expect(shrunk.version).toBe("2.2.0");
      expect(shrunk.actions_applied).toBe(1);
      expect(shrunk.edits_applied).toBe(1);
      expect(shrunk.output_path).toBe("out.docx");
    });

    it("drops the caller's echoes and keeps the engine's evidence", () => {
      const shrunk = shrink_batch_stats({
        engine: "node",
        version: "2.2.0",
        edits: [
          {
            status: "applied",
            type: "modify",
            target_text: "old",
            new_text: "new",
            clean_text: "new",
            comment: "Per client instruction 2026-08-03.",
            critic_markup: "{--old--}{++new++}",
            pages: [1, 2],
            heading_path: "Section 1 > Part A",
            occurrences_modified: 1,
            match_mode: "strict",
          },
        ],
      });
      const edit = shrunk.edits[0];
      for (const echo of ["target_text", "new_text", "clean_text", "comment"]) {
        expect(echo in edit).toBe(false);
      }
      expect(edit.status).toBe("applied");
      expect(edit.type).toBe("modify");
      expect(edit.critic_markup).toBe("{--old--}{++new++}");
      expect(edit.pages).toEqual([1, 2]);
      expect(edit.heading_path).toBe("Section 1 > Part A");
      expect(edit.occurrences_modified).toBe(1);
      // strict is the default: not worth a field.
      expect("match_mode" in edit).toBe(false);
      expect(approx_tokens(budgeted_json(edit))).toBeLessThanOrEqual(
        MINIMAL_EDIT_TOKEN_BUDGET,
      );
    });

    it("keeps match_mode only when it is not strict", () => {
      const shrunk = shrink_batch_stats({
        edits: [
          { status: "applied", match_mode: "strict" },
          { status: "applied", match_mode: "first" },
          { status: "applied", match_mode: "all" },
        ],
      });
      expect("match_mode" in shrunk.edits[0]).toBe(false);
      expect(shrunk.edits[1].match_mode).toBe("first");
      expect(shrunk.edits[2].match_mode).toBe("all");
    });

    it("spends the locator before the evidence", () => {
      const report = (markup: string) =>
        shrink_batch_stats({
          edits: [
            {
              status: "applied",
              type: "modify",
              critic_markup: markup,
              pages: [4],
              heading_path: DEEP_HEADING,
              occurrences_modified: 1,
            },
          ],
        }).edits[0];

      // Rung 1: the ancestors go, the deepest heading stays.
      let edit = report("{--old--}{++new++}");
      expect(edit.critic_markup).toBe("{--old--}{++new++}");
      expect(edit.heading_path).toBe("Section 4.2 Liability");
      expect(approx_tokens(budgeted_json(edit))).toBeLessThanOrEqual(
        MINIMAL_EDIT_TOKEN_BUDGET,
      );

      // Rung 2: a longer preview takes the whole locator; `pages` still says
      // where the edit landed and the evidence survives whole.
      edit = report("{--old wording here--}{++new wording here++}");
      expect(edit.critic_markup).toBe("{--old wording here--}{++new wording here++}");
      expect("heading_path" in edit).toBe(false);
      expect(edit.pages).toEqual([4]);
      expect(approx_tokens(budgeted_json(edit))).toBeLessThanOrEqual(
        MINIMAL_EDIT_TOKEN_BUDGET,
      );
    });

    it("drops the surrounding context from a preview before touching a bubble", () => {
      const edit = shrink_batch_stats({
        edits: [
          {
            status: "applied",
            type: "modify",
            critic_markup:
              "The Seller shall {--be liable--}{++not be liable++} for damages under this Agreement.",
            occurrences_modified: 1,
          },
        ],
      }).edits[0];
      expect(edit.critic_markup).toBe("{--be liable--}{++not be liable++}");
    });

    it("clamps an engine advisory instead of dropping it", () => {
      const edit = shrink_batch_stats({
        edits: [
          {
            status: "applied",
            type: "modify",
            target_text: "old",
            new_text: "new",
            critic_markup: "{--old--}{++new++}",
            warning: BACKREF_WARNING,
            occurrences_modified: 1,
          },
        ],
      }).edits[0];
      expect(approx_tokens(budgeted_json(edit))).toBeLessThanOrEqual(
        MINIMAL_EDIT_TOKEN_BUDGET,
      );
      expect(edit.warning).toBeTruthy();
      // Clamped, not rewritten, and it still names the offending token.
      expect(
        BACKREF_WARNING.startsWith(String(edit.warning).replace(/\.+$/, "")),
      ).toBe(true);
      expect(edit.warning).toContain("$1");
    });

    it("keeps a failed edit's full error and a clamped target stub", () => {
      const long_target = "A".repeat(120);
      const full_error =
        "- Edit 1 Failed: Target text 'AAAA...' was not found anywhere in the active document projection.";
      const edit = shrink_batch_stats({
        edits: [
          {
            status: "failed",
            type: "modify",
            target_text: long_target,
            new_text: "replacement",
            clean_text: "clean",
            critic_markup: "{--should not be reported--}",
            error: full_error,
            match_mode: "strict",
          },
        ],
      }).edits[0];
      expect(edit.status).toBe("failed");
      expect(edit.error).toBe(full_error);
      expect(String(edit.target_text).length).toBeLessThanOrEqual(
        FAILED_TARGET_STUB_CAP,
      );
      expect(edit.target_text).toBe("A".repeat(77) + "...");
      expect("new_text" in edit).toBe(false);
      expect("clean_text" in edit).toBe(false);
      // A failed edit changed nothing: it has no preview to show.
      expect("critic_markup" in edit).toBe(false);
    });

    it("passes non-object edit entries through untouched", () => {
      const shrunk = shrink_batch_stats({ edits: ["raw note", null] });
      expect(shrunk.edits).toEqual(["raw note", null]);
    });

    it("never invents a skipped_details key", () => {
      const shrunk = shrink_batch_stats({
        version: "2.2.0",
        edits: [{ status: "applied" }],
      });
      expect("skipped_details" in shrunk).toBe(false);
    });

    it("dedupes skipped_details against the per-edit errors, preserving order", () => {
      const err_msg =
        "- Edit 1 Failed: target text not found\n  Nearest candidate: 'Consultant shall'";
      const shrunk = shrink_batch_stats({
        skipped_details: [
          err_msg,
          "  Nearest candidate: 'Consultant shall'",
          "- Note: comments were preserved",
          "- Note: comments were preserved",
          "Other skipped detail",
        ],
        edits: [{ status: "failed", error: err_msg }],
      });
      expect(shrunk.skipped_details).toEqual([
        "- Note: comments were preserved",
        "Other skipped detail",
      ]);
    });

    describe("per-edit budget over the hard fixtures", () => {
      const fixtures: Record<string, Record<string, unknown>> = {
        plain: {
          status: "applied",
          type: "modify",
          target_text: "old",
          new_text: "new",
          clean_text: "new",
          critic_markup: "{--old--}{++new++}",
          pages: [1],
          heading_path: "Introduction",
          occurrences_modified: 1,
          match_mode: "strict",
        },
        "twelve-page fan-out": {
          status: "applied",
          type: "modify",
          critic_markup: "{--Consultant--}{++Contractor++}",
          pages: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
          heading_path: DEEP_HEADING,
          occurrences_modified: 12,
          match_mode: "all",
        },
        "260-char warning": {
          status: "applied",
          type: "modify",
          critic_markup:
            "Clause 1: The {--Consultant--}{++Contractor++} shall deliver the work product.",
          pages: [3],
          heading_path: DEEP_HEADING,
          occurrences_modified: 1,
          warning: BACKREF_WARNING,
        },
        "ten-bubble fan-out with warning": {
          status: "applied",
          type: "modify",
          critic_markup: FANOUT_MARKUP,
          pages: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
          heading_path: DEEP_HEADING,
          occurrences_modified: 10,
          match_mode: "all",
          warning: BACKREF_WARNING,
        },
      };

      for (const [name, fixture] of Object.entries(fixtures)) {
        it(`fits ${MINIMAL_EDIT_TOKEN_BUDGET} approx-tokens: ${name}`, () => {
          const edit = shrink_batch_stats({ edits: [fixture] }).edits[0];
          const dumped = budgeted_json(edit);
          expect(
            approx_tokens(dumped),
            `${name} exceeded the budget (${dumped.length} chars): ${dumped}`,
          ).toBeLessThanOrEqual(MINIMAL_EDIT_TOKEN_BUDGET);
        });

        it(`emits balanced CriticMarkup or nothing: ${name}`, () => {
          const shrunk = shrink_batch_stats({ edits: [fixture] });
          assert_all_strings_balanced(shrunk);
          const edit = shrunk.edits[0];
          if (edit.critic_markup) {
            expect(BUBBLE_RE.test(edit.critic_markup)).toBe(true);
            BUBBLE_RE.lastIndex = 0;
          }
        });
      }

      it("counts off the spans a fan-out preview had to drop", () => {
        const edit = shrink_batch_stats({
          edits: [
            {
              status: "applied",
              type: "modify",
              critic_markup: FANOUT_MARKUP,
              occurrences_modified: 10,
              match_mode: "all",
            },
          ],
        }).edits[0];
        expect(edit.critic_markup).toContain("more spans)");
        assert_balanced_critic_markup(edit.critic_markup);
      });

      it("drops an unbalanced preview whole rather than shipping a fragment", () => {
        const edit = shrink_batch_stats({
          edits: [
            {
              status: "applied",
              type: "modify",
              // What truncate_middle leaves behind for an over-long echo: a
              // bubble cut open mid-body.
              critic_markup:
                "{--The Seller hereby irrevocably agrees… [420 chars omitted] …agrees to indemnify++}",
              occurrences_modified: 1,
            },
          ],
        }).edits[0];
        expect("critic_markup" in edit).toBe(false);
      });
    });
  });
});
