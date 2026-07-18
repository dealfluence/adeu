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

        # Drop unused terms from the SYMBOL TABLE only — that filter is noise
        # reduction for the Defined Terms listing, and must not gate the
        # Semantic Diagnostics: a term defined twice and never used is two
        # drafting errors, not zero (QA 2026-07-17 F6). Surface the orphan
        # definition itself as a diagnostic instead.
        for term in list(definitions.keys()):
            if definitions[term]["count"] == 0:
                del definitions[term]
                if term not in duplicates:
                    diagnostics.append(f"[Warning] Unused Definition: '{term}' is defined but never used.")

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


# FILE: src/adeu/domain.py
def extract_document_settings_warnings(doc) -> List[str]:
    """
    Inspects word/settings.xml for privacy flags that cause Microsoft Word to
    silently strip attribution from tracked changes and comments the next time
    the document is opened and saved.

    The engine itself preserves the attribution it writes — but Word will
    destroy `w:author`, `w:initials`, and/or `w:date` on next save when these
    flags are enabled. We surface this to the agent at read time so it can
    decide how to handle the situation before making edits.

    Returns a list of warning strings (one per enabled flag), without leading
    bullets. Empty list if settings.xml is absent or neither flag is enabled.

    OOXML boolean truthiness:
      - element absent           → disabled
      - element present, no w:val → enabled (OOXML default)
      - element with w:val in {"0","false","off"} (case-insensitive) → disabled
      - element with any other w:val → enabled
    """
    warnings: List[str] = []

    settings_part = None
    for part in doc.part.package.parts:
        if str(part.partname) == "/word/settings.xml":
            settings_part = part
            break
    if settings_part is None:
        return warnings

    # The settings part may be a generic Part or an XmlPart depending on how
    # python-docx loaded it. Parse the blob directly so this is robust either way.
    from docx.oxml import parse_xml

    try:
        root = parse_xml(settings_part.blob)
    except Exception:
        return warnings

    def _is_truthy(el) -> bool:
        # OOXML boolean rule: element present with no w:val attribute defaults to true.
        # w:val of "0", "false", or "off" (case-insensitive) means disabled.
        val = el.get(qn("w:val"))
        if val is None:
            return True
        return val.lower() not in ("0", "false", "off")

    # Use iter() + local name matching for namespace-agnostic lookup, mirroring
    # the TS findDescendantsByLocalName helper. This is robust to settings.xml
    # variants from different Word versions even though w:settings is the
    # canonical namespace.
    def _find_first_by_local_name(local_name: str):
        suffix = "}" + local_name
        for el in root.iter():
            tag = el.tag
            if isinstance(tag, str) and (tag == local_name or tag.endswith(suffix)):
                return el
        return None

    remove_personal = _find_first_by_local_name("removePersonalInformation")
    if remove_personal is not None and _is_truthy(remove_personal):
        warnings.append(
            "[Warning] Privacy flag `removePersonalInformation` is enabled in word/settings.xml. "
            "Microsoft Word will strip the `w:author`, `w:initials`, and `w:date` attributes "
            "from every tracked change and comment the next time this document is opened and saved. "
            "Edits made by this agent will lose attribution, breaking audit trails and any "
            "multi-turn workflow that relies on identifying prior edits."
        )

    remove_date_time = _find_first_by_local_name("removeDateAndTime")
    if remove_date_time is not None and _is_truthy(remove_date_time):
        warnings.append(
            "[Warning] Privacy flag `removeDateAndTime` is enabled in word/settings.xml. "
            "Microsoft Word will strip the `w:date` attribute from every tracked change and "
            "comment the next time this document is opened and saved. Timestamps on this "
            "agent's edits will be lost on the next Word save."
        )

    return warnings


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

        # Symbol-table noise reduction must not gate diagnostics — see the
        # matching comment in extract_definitions_and_diagnostics (QA F6).
        for term in list(definitions.keys()):
            if definitions[term]["count"] == 0:
                del definitions[term]
                if term not in duplicates:
                    diagnostics.append(f"[Warning] Unused Definition: '{term}' is defined but never used.")

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
    settings_warnings = extract_document_settings_warnings(doc)
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

    if settings_warnings:
        has_content = True
        lines.append("\n## Document Settings")
        for warning in settings_warnings:
            lines.append(f"- {warning}")

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
