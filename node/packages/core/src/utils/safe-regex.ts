// FILE: src/utils/safe-regex.ts
/**
 * Time-budgeted execution of USER/LLM-supplied regular expressions.
 *
 * `regex: true` on a ModifyText edit and `search_regex` on read_docx hand an
 * LLM-controlled pattern to V8's backtracking engine. A pathological pattern
 * like `(a|a)*$` against a run of repeated characters hangs the event loop
 * indefinitely (QA 2026-07-17 F5 — ReDoS; mirrors the Python fix).
 *
 * Pure JavaScript safety analyzer and execution budget without node:vm dependency,
 * compliant with restricted node runtime environments (n8n Cloud, etc.).
 *
 * Only USER-SUPPLIED patterns belong here. The engine's own generated
 * patterns (fuzzy matchers etc.) are built to be linear-time.
 */

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

/**
 * Static analyzer detecting catastrophic backtracking risk in regular expressions
 * (e.g. nested quantifiers like (a+)+, (a*)*, or quantified alternations like (a|a)*).
 */
export function hasCatastrophicBacktrackingRisk(pattern: string): boolean {
  let inCharClass = false;
  let escaped = false;

  const groupStack: Array<{ hasInnerQuantifierOrPipe: boolean }> = [];

  for (let i = 0; i < pattern.length; i++) {
    const char = pattern[i];

    if (escaped) {
      escaped = false;
      continue;
    }

    if (char === "\\") {
      escaped = true;
      continue;
    }

    if (inCharClass) {
      if (char === "]") inCharClass = false;
      continue;
    }

    if (char === "[") {
      inCharClass = true;
      continue;
    }

    if (char === "(") {
      groupStack.push({ hasInnerQuantifierOrPipe: false });
      continue;
    }

    if (char === ")") {
      if (groupStack.length > 0) {
        const top = groupStack.pop()!;
        const remaining = pattern.substring(i + 1);
        const matchQuant = remaining.match(/^(\*|\+|\{\d+\,\s*\}|\?)/);
        const nextChar = matchQuant ? matchQuant[1] : "";
        const isUnboundedQuantifier =
          nextChar === "*" ||
          nextChar === "+" ||
          /^\{\d+\,\s*\}/.test(remaining);

        if (isUnboundedQuantifier && top.hasInnerQuantifierOrPipe) {
          return true;
        }

        if (
          groupStack.length > 0 &&
          (top.hasInnerQuantifierOrPipe || isUnboundedQuantifier)
        ) {
          groupStack[groupStack.length - 1].hasInnerQuantifierOrPipe = true;
        }
      }
      continue;
    }

    if (groupStack.length > 0) {
      if (char === "|" || char === "*" || char === "+") {
        groupStack[groupStack.length - 1].hasInnerQuantifierOrPipe = true;
      } else if (/^\{\d+\,\s*\}/.test(pattern.substring(i))) {
        groupStack[groupStack.length - 1].hasInnerQuantifierOrPipe = true;
      }
    }
  }

  return false;
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
  if (hasCatastrophicBacktrackingRisk(pattern)) {
    throw new RegexTimeoutError(pattern);
  }

  const normalized = flags.includes("g") ? flags : flags + "g";
  const re = new RegExp(pattern, normalized);

  const startTime = Date.now();
  const out: Array<{ start: number; end: number }> = [];
  let m: RegExpExecArray | null;

  while ((m = re.exec(text)) !== null) {
    if (Date.now() - startTime > USER_PATTERN_TIMEOUT_MS) {
      throw new RegexTimeoutError(pattern);
    }
    out.push({ start: m.index, end: m.index + m[0].length });
    if (m.index === re.lastIndex) re.lastIndex++;
  }

  return out;
}

/** First match of a user pattern under the wall-clock budget, or null. */
export function userSearch(
  pattern: string,
  text: string,
  flags: string = "",
): { start: number; end: number } | null {
  if (hasCatastrophicBacktrackingRisk(pattern)) {
    throw new RegexTimeoutError(pattern);
  }

  const re = new RegExp(pattern, flags.replace("g", ""));
  const startTime = Date.now();
  const m = re.exec(text);

  if (Date.now() - startTime > USER_PATTERN_TIMEOUT_MS) {
    throw new RegexTimeoutError(pattern);
  }

  return m ? { start: m.index, end: m.index + m[0].length } : null;
}
