import re
from typing import Any, Dict, List, Tuple

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from adeu.utils.docx import get_run_text, iter_block_items


def _get_paragraph_text(p: Paragraph) -> str:
    return "".join(get_run_text(r) for r in p.runs)


def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row: List[int] = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def extract_definitions_and_diagnostics(doc, base_text: str) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """
    Heuristically extracts terms wrapped in quotes (Glossary & Inline)
    and generates semantic diagnostics (Unresolved, Unused, Duplicate, Typo).
    """
    definitions = {}
    duplicates = set()

    # Language-Agnostic Typographic Extractors
    # 1. Paragraph-Leading: Matches `"Term"`, `1. "Term"`, `(a) "Term"`
    leading_re = re.compile(r"^(?:[\d\.\-\(\)a-zA-Z]+\s*)?[\"“]([A-Z][A-Za-z0-9\s\-&\'’]{1,60})[\"”]")

    # 2. Parenthetical Inline: Matches `(the "Term")`, `(jäljempänä "Term")`
    inline_re = re.compile(r'\([^)]*?["“]([A-Z][A-Za-z0-9\s\-&\'’]{1,60})["”][^)]*?\)')

    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            text = _get_paragraph_text(item).strip()
            if not text:
                continue

            extracted_terms = []

            leading_match = leading_re.match(text)
            if leading_match:
                extracted_terms.append(leading_match.group(1).strip())

            for m in inline_re.finditer(text):
                extracted_terms.append(m.group(1).strip())

            for term in extracted_terms:
                if term in definitions:
                    duplicates.add(term)
                else:
                    definitions[term] = {"count": 0}

    diagnostics = []

    for term in list(definitions.keys()):
        escaped_term = re.escape(term)
        pattern = rf'(?<!["“])\b{escaped_term}\b(?!["”])'
        usages = len(re.findall(pattern, base_text))

        if usages == 0:
            # Rule: Must be used to be a term. Drop dead code or phantom matches.
            del definitions[term]
            if term in duplicates:
                duplicates.remove(term)
        else:
            definitions[term]["count"] = usages

    for term in duplicates:
        diagnostics.append(f"[Error] Duplicate Definition: '{term}' is defined multiple times.")

    stop_words = {
        "The",
        "This",
        "That",
        "Such",
        "A",
        "An",
        "Any",
        "All",
        "Some",
        "No",
        "Every",
        "Each",
        "As",
        "In",
        "Of",
        "For",
        "To",
        "On",
        "By",
        "With",
    }

    all_cap_pattern = r"\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*\b"
    all_caps = set(re.findall(all_cap_pattern, base_text))

    valid_terms = set(definitions.keys())
    candidates_by_term: Dict[str, List[str]] = {}

    for candidate in all_caps:
        candidate = candidate.strip()
        words = candidate.split()
        # Strip leading stop words (e.g., "As I" -> "I")
        while words and words[0].title() in stop_words:
            words = words[1:]
        candidate = " ".join(words)

        if len(candidate) < 4:
            continue
        if candidate in valid_terms:
            continue

        for term in valid_terms:
            if abs(len(candidate) - len(term)) > 2:
                continue

            # Ignore simple plurals/singulars to reduce false positives (e.g. GPUs vs GPU)
            if candidate == term + "s" or candidate == term + "es":
                continue
            if term == candidate + "s" or term == candidate + "es":
                continue

            dist = levenshtein_distance(candidate, term)

            if dist == 0 or dist > 2:
                continue

            # Stricter rules for short words to prevent coincidental acronym matches
            if len(term) <= 5:
                if dist > 1:
                    continue
                if candidate[0].lower() != term[0].lower():
                    continue

            if term not in candidates_by_term:
                candidates_by_term[term] = []
            if candidate not in candidates_by_term[term]:
                candidates_by_term[term].append(candidate)

    for term, candidates in candidates_by_term.items():
        c_str = ", ".join(f"'{c}'" for c in sorted(candidates))
        diagnostics.append(f"[Info] Possible Typos for '{term}': Found {c_str}")

    def diag_sort_key(msg):
        if msg.startswith("[Error]"):
            return 0
        if msg.startswith("[Warning]"):
            return 1
        return 2

    diagnostics.sort(key=lambda x: (diag_sort_key(x), x))

    return definitions, diagnostics


def extract_anchors(doc) -> Dict[str, Dict[str, Any]]:
    """
    Deterministically builds a dependency map of Bookmarks and Cross-References.
    """
    anchors: Dict[str, Dict[str, Any]] = {}

    # Pass 1: Find bookmarks
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            for node in item._element.iter():
                if node.tag == qn("w:bookmarkStart"):
                    b_name = node.get(qn("w:name"))
                    if b_name and (not b_name.startswith("_") or b_name.startswith("_Ref")):
                        if b_name not in anchors:
                            text = _get_paragraph_text(item).strip()
                            anchors[b_name] = {
                                "anchored_to": text[:60] + ("..." if len(text) > 60 else ""),
                                "referenced_from": [],
                            }

    # Pass 2: Find references
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            p_text = _get_paragraph_text(item).strip()
            for node in item._element.iter():
                target = None
                if node.tag == qn("w:fldSimple"):
                    instr = node.get(qn("w:instr"), "")
                    parts = instr.strip().split()
                    if parts and parts[0] == "REF" and len(parts) > 1:
                        target = parts[1]
                elif node.tag == qn("w:instrText"):
                    instr = node.text or ""
                    parts = instr.strip().split()
                    if parts and parts[0] == "REF" and len(parts) > 1:
                        target = parts[1]

                if target and target in anchors:
                    anchors[target]["referenced_from"].append(p_text[:60] + ("..." if len(p_text) > 60 else ""))

    return anchors


def build_structural_appendix(doc, base_text: str) -> str:
    """
    Compiles the Read-Only Structural Appendix block for the agent.
    Returns an empty string if no relevant domain metadata is found.
    """
    defs, diagnostics = extract_definitions_and_diagnostics(doc, base_text)
    anchors = extract_anchors(doc)

    lines: List[str] = [
        "\n\n---",
        "",
        "<!-- READONLY_BOUNDARY_START -->",
        "# Document Structure (Read-Only)",
        (
            "The content below is metadata describing the document's reference structure. "
            "Do not include this section in any tracked changes or edits — it is for your "
            "context only and will be discarded on write."
        ),
    ]

    has_content = False

    if defs:
        has_content = True
        lines.append("\n## Defined Terms")
        for term, data in defs.items():
            lines.append(f'- "{term}" — used {data["count"]} times.')

    if diagnostics:
        has_content = True
        lines.append("\n## Semantic Diagnostics")
        for diag in diagnostics:
            lines.append(f"- {diag}")

    if anchors:
        has_content = True
        lines.append("\n## Named Anchors")
        for b_name, data in anchors.items():
            lines.append(f'- {b_name} → Anchored to: "{data["anchored_to"]}"')
            for ref in data["referenced_from"]:
                lines.append(f'  - Referenced from: "{ref}"')

    if has_content:
        return "\n".join(lines)
    return ""
