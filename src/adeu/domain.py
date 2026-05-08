import re
from typing import Any, Dict, List, Tuple

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from rapidfuzz.distance import Levenshtein

from adeu.utils.docx import get_run_text, iter_block_items


def _get_paragraph_text(p: Paragraph) -> str:
    return "".join(get_run_text(r) for r in p.runs)


def levenshtein_distance(s1: str, s2: str) -> int:
    """C-backed Levenshtein via rapidfuzz. ~50-100x faster than pure Python."""
    return Levenshtein.distance(s1, s2)


# FILE: src/adeu/domain.py
def extract_definitions_and_diagnostics(doc, base_text: str) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """
    Heuristically extracts terms wrapped in quotes (Glossary & Inline)
    and generates semantic diagnostics (Unresolved, Unused, Duplicate, Typo).
    """
    definitions: Dict[str, Dict[str, Any]] = {}
    duplicates = set()

    leading_re = re.compile(r"^(?:[\d\.\-\(\)a-zA-Z]+\s*)?[\"“]([A-Z][A-Za-z0-9\s\-&\'’]{1,60})[\"”]")
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

    # === Single-pass usage counting ===
    # Build one alternation regex over all terms (sorted longest-first to prefer longer matches)
    # and scan base_text exactly once instead of N times.
    if definitions:
        sorted_terms = sorted(definitions.keys(), key=len, reverse=True)
        # Each term: not preceded by quote, whole word, not followed by quote
        alt = "|".join(re.escape(t) for t in sorted_terms)
        usage_pattern = re.compile(rf'(?<!["“])\b({alt})\b(?!["”])')

        for m in usage_pattern.finditer(base_text):
            matched_term = m.group(1)
            if matched_term in definitions:
                definitions[matched_term]["count"] += 1

        # Drop unused terms (same semantics as before)
        for term in list(definitions.keys()):
            if definitions[term]["count"] == 0:
                del definitions[term]
                duplicates.discard(term)

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
    # Pre-bucket valid terms by first letter (lowercased) for O(1) prune.
    terms_by_first_letter: Dict[str, List[str]] = {}
    for term in valid_terms:
        terms_by_first_letter.setdefault(term[0].lower(), []).append(term)

    candidates_by_term: Dict[str, List[str]] = {}

    for candidate in all_caps:
        candidate = candidate.strip()
        words = candidate.split()
        while words and words[0].title() in stop_words:
            words = words[1:]
        candidate = " ".join(words)

        if len(candidate) < 4:
            continue
        if candidate in valid_terms:
            continue

        # Only check terms that share a first letter (with the original logic, mismatched
        # first letters were only excluded for short acronyms — but in practice the dist
        # check already filters them. This prefilter is a strict performance win when
        # combined with the explicit `dist > 2` skip below.)
        first_letter = candidate[0].lower()
        candidate_terms = terms_by_first_letter.get(first_letter, [])

        # Also include other-first-letter terms ONLY for length 6+ to preserve original
        # behavior for non-acronym typos that change the first character.
        if len(candidate) > 5:
            for k, v in terms_by_first_letter.items():
                if k != first_letter:
                    candidate_terms = candidate_terms + v

        for term in candidate_terms:
            if abs(len(candidate) - len(term)) > 2:
                continue
            if candidate == term + "s" or candidate == term + "es":
                continue
            if term == candidate + "s" or term == candidate + "es":
                continue

            dist = Levenshtein.distance(candidate, term, score_cutoff=2)
            if dist == 0 or dist > 2:
                continue

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


def extract_all_domain_metadata(
    doc, base_text: str
) -> Tuple[Dict[str, Dict[str, Any]], List[str], Dict[str, Dict[str, Any]]]:
    """
    SINGLE-PASS EXTRACTION ENGINE
    Replaces multiple document walks by simultaneously extracting Definitions,
    Bookmarks, and Cross-References in a single O(N) iteration over block items.
    """
    definitions: Dict[str, Dict[str, Any]] = {}
    duplicates = set()
    raw_anchors: Dict[str, Dict[str, Any]] = {}
    raw_references: List[Tuple[str, str]] = []  # (target_bookmark, referencing_text)

    leading_re = re.compile(r"^(?:[\d\.\-\(\)a-zA-Z]+\s*)?[\"“]([A-Z][A-Za-z0-9\s\-&\'’]{1,60})[\"”]")
    inline_re = re.compile(r'\([^)]*?["“]([A-Z][A-Za-z0-9\s\-&\'’]{1,60})["”][^)]*?\)')

    # --- SINGLE DOCUMENT WALK ---
    for item in iter_block_items(doc):
        if not isinstance(item, Paragraph):
            continue

        # Used for definition extraction and anchor previews
        text = _get_paragraph_text(item).strip()
        if not text:
            continue

        # 1. Extract Definitions
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

        # 2. Extract Bookmarks & References simultaneously
        short_text = text[:60] + ("..." if len(text) > 60 else "")

        for node in item._element.iter():
            # Check Bookmarks
            if node.tag == qn("w:bookmarkStart"):
                b_name = node.get(qn("w:name"))
                if b_name and (not b_name.startswith("_") or b_name.startswith("_Ref")):
                    if b_name not in raw_anchors:
                        raw_anchors[b_name] = {
                            "anchored_to": short_text,
                            "referenced_from": [],
                        }

            # Check References
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

            if target:
                raw_references.append((target, short_text))

    # --- POST-PROCESSING: Resolve Relationships ---
    for target, ref_text in raw_references:
        if target in raw_anchors:
            raw_anchors[target]["referenced_from"].append(ref_text)

    # --- POST-PROCESSING: Definitions & Diagnostics ---
    diagnostics = []

    if definitions:
        sorted_terms = sorted(definitions.keys(), key=len, reverse=True)
        alt = "|".join(re.escape(t) for t in sorted_terms)
        usage_pattern = re.compile(rf'(?<!["“])\b({alt})\b(?!["”])')

        for m in usage_pattern.finditer(base_text):
            matched_term = m.group(1)
            if matched_term in definitions:
                definitions[matched_term]["count"] += 1

        for term in list(definitions.keys()):
            if definitions[term]["count"] == 0:
                del definitions[term]
                duplicates.discard(term)

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
    terms_by_first_letter: Dict[str, List[str]] = {}
    for term in valid_terms:
        terms_by_first_letter.setdefault(term[0].lower(), []).append(term)

    candidates_by_term: Dict[str, List[str]] = {}

    for candidate in all_caps:
        candidate = candidate.strip()
        words = candidate.split()
        while words and words[0].title() in stop_words:
            words = words[1:]
        candidate = " ".join(words)

        if len(candidate) < 4:
            continue
        if candidate in valid_terms:
            continue

        first_letter = candidate[0].lower()
        candidate_terms = terms_by_first_letter.get(first_letter, [])

        if len(candidate) > 5:
            for k, v in terms_by_first_letter.items():
                if k != first_letter:
                    candidate_terms = candidate_terms + v

        for term in candidate_terms:
            if abs(len(candidate) - len(term)) > 2:
                continue
            if candidate == term + "s" or candidate == term + "es":
                continue
            if term == candidate + "s" or term == candidate + "es":
                continue

            dist = Levenshtein.distance(candidate, term, score_cutoff=2)
            if dist == 0 or dist > 2:
                continue

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

    return definitions, diagnostics, raw_anchors


def build_structural_appendix(doc, base_text: str) -> str:
    """
    Compiles the Read-Only Structural Appendix block for the agent.
    Returns an empty string if no relevant domain metadata is found.
    """
    defs, diagnostics, anchors = extract_all_domain_metadata(doc, base_text)

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
