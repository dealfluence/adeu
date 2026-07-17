// FILE: src/utils/text.ts
// Small text helpers shared by engine output paths. Mirrors the Python
// engine's adeu/utils/text.py so both engines bound their report output
// identically (QA C2).

// Default cap for echoing caller-supplied strings (target_text/new_text) back
// in batch reports and error messages. Reports feed straight into LLM context
// windows via MCP, so an oversized edit value must never be reflected in full
// (QA C2: a 2MB new_text was echoed twice, unbounded, in the apply report).
export const REPORT_ECHO_CAP = 500;

// Tighter cap for the inline redline preview snippets ({--...--}{++...++}),
// which additionally carry surrounding document context.
export const PREVIEW_TEXT_CAP = 200;

/**
 * Bounds `text` to roughly `cap` visible characters, keeping the head and
 * tail and stating how much was omitted. Returns short strings unchanged.
 */
export function truncate_middle(text: string, cap: number): string {
  if (text === null || text === undefined || text.length <= cap) return text;
  const head = Math.max(1, Math.floor((cap * 2) / 3));
  const tail = Math.max(1, cap - head);
  const omitted = text.length - head - tail;
  return `${text.slice(0, head)}… [${omitted.toLocaleString("en-US")} chars omitted] …${text.slice(-tail)}`;
}
