import { DocumentObject } from './docx/bridge.js';
import { Paragraph, Run } from './docx/primitives.js';
import { iter_block_items, get_run_text } from './utils/docx.js';
import { findAllDescendants } from './docx/dom.js';

function boundedLevenshtein(a: string, b: string, maxDist: number = 2): number {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > maxDist) return maxDist + 1;
  if (a.length === 0) return b.length <= maxDist ? b.length : maxDist + 1;
  if (b.length === 0) return a.length <= maxDist ? a.length : maxDist + 1;

  if (a.length > b.length) {
    const temp = a;
    a = b;
    b = temp;
  }

  let row = Array.from({ length: a.length + 1 }, (_, i) => i);

  for (let i = 1; i <= b.length; i++) {
    const newRow = [i];
    let minInRow = i;
    for (let j = 1; j <= a.length; j++) {
      const cost = a[j - 1] === b[i - 1] ? 0 : 1;
      const val = Math.min(
        row[j] + 1,
        newRow[j - 1] + 1,
        row[j - 1] + cost
      );
      newRow.push(val);
      if (val < minInRow) minInRow = val;
    }
    if (minInRow > maxDist) return maxDist + 1;
    row = newRow;
  }
  return row[a.length] <= maxDist ? row[a.length] : maxDist + 1;
}

function _get_paragraph_text(p: Paragraph): string {
  let text = '';
  const runs = findAllDescendants(p._element, 'w:r');
  for (const r of runs) {
    text += get_run_text(new Run(r, p));
  }
  return text;
}

export function extract_all_domain_metadata(
  doc: DocumentObject,
  base_text: string
): [Record<string, { count: number }>, string[], Record<string, { anchored_to: string; referenced_from: string[] }>] {
  const definitions: Record<string, { count: number }> = {};
  const duplicates = new Set<string>();
  const raw_anchors: Record<string, { anchored_to: string; referenced_from: string[] }> = {};
  const raw_references: [string, string][] = [];

  const leading_re = /^(?:[\d.\-()a-zA-Z]+\s*)?["“]([A-Z][A-Za-z0-9\s\-&'’]{1,60})["”]/;
  const inline_re = /\([^)]*?["“]([A-Z][A-Za-z0-9\s\-&'’]{1,60})["”][^)]*?\)/g;

  for (const item of iter_block_items(doc)) {
    if (!(item instanceof Paragraph)) continue;

    const text = _get_paragraph_text(item).trim();
    if (!text) continue;

    const extracted_terms: string[] = [];
    const leading_match = text.match(leading_re);
    if (leading_match) extracted_terms.push(leading_match[1].trim());

    const inline_matches = text.matchAll(inline_re);
    for (const m of inline_matches) {
      extracted_terms.push(m[1].trim());
    }

    for (const term of extracted_terms) {
      if (definitions[term]) duplicates.add(term);
      else definitions[term] = { count: 0 };
    }

    const short_text = text.length > 60 ? text.substring(0, 60) + '...' : text;

    const nodes = findAllDescendants(item._element, '*');
    for (const node of nodes) {
      if (node.tagName === 'w:bookmarkStart') {
        const b_name = node.getAttribute('w:name');
        if (b_name && (!b_name.startsWith('_') || b_name.startsWith('_Ref'))) {
          if (!raw_anchors[b_name]) {
            raw_anchors[b_name] = { anchored_to: short_text, referenced_from: [] };
          }
        }
      }

      let target: string | null = null;
      if (node.tagName === 'w:fldSimple') {
        const instr = node.getAttribute('w:instr') || '';
        const parts = instr.trim().split(/\s+/);
        if (parts.length > 1 && parts[0] === 'REF') target = parts[1];
      } else if (node.tagName === 'w:instrText') {
        const instr = node.textContent || '';
        const parts = instr.trim().split(/\s+/);
        if (parts.length > 1 && parts[0] === 'REF') target = parts[1];
      }

      if (target) raw_references.push([target, short_text]);
    }
  }

  for (const [target, ref_text] of raw_references) {
    if (raw_anchors[target]) {
      raw_anchors[target].referenced_from.push(ref_text);
    }
  }

  const diagnostics: string[] = [];

  const def_keys = Object.keys(definitions);
  if (def_keys.length > 0) {
    const sorted_terms = def_keys.sort((a, b) => b.length - a.length);
    const escapeRegExp = (str: string) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const alt = sorted_terms.map(escapeRegExp).join('|');
    const usage_pattern = new RegExp(`(?<!["“])\\b(${alt})\\b(?![”"])`, 'g');

    for (const m of base_text.matchAll(usage_pattern)) {
      const matched_term = m[1];
      if (definitions[matched_term]) definitions[matched_term].count++;
    }

    for (const term of def_keys) {
      if (definitions[term].count === 0) {
        delete definitions[term];
        duplicates.delete(term);
      }
    }
  }

  for (const term of duplicates) {
    diagnostics.push(`[Error] Duplicate Definition: '${term}' is defined multiple times.`);
  }

  const stop_words = new Set([
    "The", "This", "That", "Such", "A", "An", "Any", "All", "Some", "No",
    "Every", "Each", "As", "In", "Of", "For", "To", "On", "By", "With"
  ]);

  const all_cap_pattern = /\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*\b/g;
  const all_caps = new Set(base_text.match(all_cap_pattern) || []);

  const valid_terms = new Set(Object.keys(definitions));
  const terms_by_first_letter: Record<string, string[]> = {};
  for (const term of valid_terms) {
    const fl = term[0].toLowerCase();
    if (!terms_by_first_letter[fl]) terms_by_first_letter[fl] = [];
    terms_by_first_letter[fl].push(term);
  }

  const candidates_by_term: Record<string, string[]> = {};

  for (const raw_candidate of all_caps) {
    let candidate = raw_candidate.trim();
    const words = candidate.split(/\s+/);
    while (words.length > 0) {
      const first = words[0];
      const title = first.charAt(0).toUpperCase() + first.slice(1).toLowerCase();
      if (stop_words.has(title)) words.shift();
      else break;
    }
    candidate = words.join(' ');

    if (candidate.length < 4) continue;
    if (valid_terms.has(candidate)) continue;

    const first_letter = candidate[0].toLowerCase();
    let candidate_terms = terms_by_first_letter[first_letter] || [];

    if (candidate.length > 5) {
      for (const [k, v] of Object.entries(terms_by_first_letter)) {
        if (k !== first_letter) candidate_terms = candidate_terms.concat(v);
      }
    }

    for (const term of candidate_terms) {
      if (Math.abs(candidate.length - term.length) > 2) continue;
      if (candidate === term + 's' || candidate === term + 'es') continue;
      if (term === candidate + 's' || term === candidate + 'es') continue;

      const dist = boundedLevenshtein(candidate, term, 2);
      if (dist === 0 || dist > 2) continue;

      if (term.length <= 5) {
        if (dist > 1) continue;
        if (candidate[0].toLowerCase() !== term[0].toLowerCase()) continue;
      }

      if (!candidates_by_term[term]) candidates_by_term[term] = [];
      if (!candidates_by_term[term].includes(candidate)) candidates_by_term[term].push(candidate);
    }
  }

  for (const [term, candidates] of Object.entries(candidates_by_term)) {
    candidates.sort();
    const c_str = candidates.map(c => `'${c}'`).join(', ');
    diagnostics.push(`[Info] Possible Typos for '${term}': Found ${c_str}`);
  }

  function diag_sort_key(msg: string) {
    if (msg.startsWith('[Error]')) return 0;
    if (msg.startsWith('[Warning]')) return 1;
    return 2;
  }

  diagnostics.sort((a, b) => {
    const keyA = diag_sort_key(a);
    const keyB = diag_sort_key(b);
    if (keyA !== keyB) return keyA - keyB;
    return a.localeCompare(b);
  });

  return [definitions, diagnostics, raw_anchors];
}

export function build_structural_appendix(doc: DocumentObject, base_text: string): string {
  const [defs, diagnostics, anchors] = extract_all_domain_metadata(doc, base_text);

  const lines: string[] = [
    "\n\n---",
    "",
    "<!-- READONLY_BOUNDARY_START -->",
    "# Document Structure (Read-Only)",
    "The content below is metadata describing the document's reference structure. Do not include this section in any tracked changes or edits \u2014 it is for your context only and will be discarded on write."
  ];

  let has_content = false;

  if (Object.keys(defs).length > 0) {
    has_content = true;
    lines.push("\n## Defined Terms");
    for (const [term, data] of Object.entries(defs)) {
      lines.push(`- "${term}" \u2014 used ${data.count} times.`);
    }
  }

  if (diagnostics.length > 0) {
    has_content = true;
    lines.push("\n## Semantic Diagnostics");
    for (const diag of diagnostics) {
      lines.push(`- ${diag}`);
    }
  }

  if (Object.keys(anchors).length > 0) {
    has_content = true;
    lines.push("\n## Named Anchors");
    for (const [b_name, data] of Object.entries(anchors)) {
      lines.push(`- ${b_name} \u2192 Anchored to: "${data.anchored_to}"`);
      for (const ref of data.referenced_from) {
        lines.push(`  - Referenced from: "${ref}"`);
      }
    }
  }

  if (has_content) {
    return lines.join('\n');
  }
  return "";
}