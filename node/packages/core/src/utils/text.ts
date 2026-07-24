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

// CriticMarkup delimiters that must never appear verbatim inside a {>>…<<}
// meta bubble: a comment body containing e.g. "{--del--}" would nest raw
// markup inside the annotation, and its "<<}"/"--}" terminates the outer
// bubble early for every CriticMarkup consumer — including this package's
// own preview/tidy regexes (QA round 3, findings 3.7/3.8).
const CRITIC_TOKENS = ["{++", "++}", "{--", "--}", "{==", "==}", "{>>", "<<}"];

/**
 * Defangs CriticMarkup delimiters in projection-embedded free text (comment
 * bodies) by spacing the brace/marker apart: "{>>x<<}" renders as
 * "{ >>x<< }". The content stays readable while no delimiter sequence
 * survives for a parser to misinterpret. Mirrors Python escape_critic_tokens.
 */
export function escape_critic_tokens(text: string): string {
  if (!text || (!text.includes("{") && !text.includes("}"))) return text;
  for (const token of CRITIC_TOKENS) {
    if (text.includes(token)) {
      const escaped = token.startsWith("{")
        ? "{ " + token.slice(1)
        : token.slice(0, -1) + " }";
      text = text.split(token).join(escaped);
    }
  }
  return text;
}

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
