// FILE: src/utils/safe-regex.ts
/**
 * Time-budgeted execution of USER/LLM-supplied regular expressions.
 *
 * `regex: true` on a ModifyText edit and `search_regex` on read_docx hand an
 * LLM-controlled pattern to V8's backtracking engine. A pathological pattern
 * like `(a|a)*$` against a run of repeated characters hangs the event loop
 * indefinitely (QA 2026-07-17 F5 — ReDoS; mirrors the Python fix).
 *
 * JS regexes cannot be interrupted in-thread, but V8 CAN interrupt regex
 * execution running inside a `vm` context with a `timeout` — that is the
 * mechanism used here. `node:vm` is a builtin, preserving the zero-dependency
 * bundle constraint.
 *
 * Only USER-SUPPLIED patterns belong here. The engine's own generated
 * patterns (fuzzy matchers etc.) are built to be linear-time.
 */

import * as vm from "node:vm";

export const USER_PATTERN_TIMEOUT_MS = 2000;

export class RegexTimeoutError extends Error {
  public pattern: string;
  constructor(pattern: string) {
    super(
      `Regular expression exceeded the ${USER_PATTERN_TIMEOUT_MS / 1000}s matching time budget ` +
        `(catastrophic backtracking). Simplify the pattern — nested quantifiers like (a+)+ ` +
        `are the usual cause — or use a literal target instead.`,
    );
    this.name = "RegexTimeoutError";
    this.pattern = pattern;
  }
}

function runBudgeted<T>(pattern: string, script: string, sandbox: object): T {
  try {
    return vm.runInNewContext(script, sandbox, {
      timeout: USER_PATTERN_TIMEOUT_MS,
    }) as T;
  } catch (e: any) {
    if (e && e.code === "ERR_SCRIPT_EXECUTION_TIMEOUT") {
      throw new RegexTimeoutError(pattern);
    }
    throw e;
  }
}

/**
 * All non-overlapping matches of a user pattern, under the wall-clock budget.
 * Invalid patterns throw SyntaxError exactly like `new RegExp(...)` would.
 * The budget covers the ENTIRE scan, not just the first match.
 */
export function userFindAllMatches(
  pattern: string,
  text: string,
  flags: string = "",
): Array<{ start: number; end: number }> {
  const normalized = flags.includes("g") ? flags : flags + "g";
  const re = new RegExp(pattern, normalized); // throws SyntaxError on bad pattern
  const raw = runBudgeted<Array<{ start: number; end: number }>>(
    pattern,
    `{
      const out = [];
      let m;
      while ((m = re.exec(text)) !== null) {
        out.push({ start: m.index, end: m.index + m[0].length });
        if (m.index === re.lastIndex) re.lastIndex++;
      }
      out;
    }`,
    { re, text },
  );
  // Re-materialize in this realm (vm objects come from another context).
  return raw.map((r) => ({ start: r.start, end: r.end }));
}

/** First match of a user pattern under the wall-clock budget, or null. */
export function userSearch(pattern: string, text: string, flags: string = ""): { start: number; end: number } | null {
  const re = new RegExp(pattern, flags.replace("g", ""));
  const raw = runBudgeted<{ start: number; end: number } | null>(
    pattern,
    `{
      const m = re.exec(text);
      m ? { start: m.index, end: m.index + m[0].length } : null;
    }`,
    { re, text },
  );
  return raw ? { start: raw.start, end: raw.end } : null;
}
