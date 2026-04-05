"""
DOCX sanitization transforms.

Each transform mutates a python-docx Document (or its underlying lxml tree)
in place and returns a list of human-readable report lines describing what
was changed.
"""

from typing import Optional

import structlog
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    iter_block_items,
    iter_document_parts,
    normalize_docx,
)

logger = structlog.get_logger(__name__)

# Namespaces used across transforms
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID_NS = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
DC_NS = "http://purl.org/dc/elements/1.1/"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DCTERMS_NS = "http://purl.org/dc/terms/"
EXTENDED_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

# rsid attribute names (all variations)
RSID_ATTRS = [
    qn("w:rsidR"),
    qn("w:rsidRPr"),
    qn("w:rsidRDefault"),
    qn("w:rsidP"),
    qn("w:rsidDel"),
    qn("w:rsidSect"),
    qn("w:rsidTr"),
]

# w14 attributes to strip
W14_ATTRS = [
    f"{{{W14_NS}}}paraId",
    f"{{{W14_NS}}}textId",
]


def strip_rsid(doc: DocumentObject) -> list[str]:
    """Remove all rsid* attributes from every element in the document."""
    count = 0
    for el in doc.element.iter():
        for attr in RSID_ATTRS:
            if attr in el.attrib:
                del el.attrib[attr]
                count += 1
    # Also strip the rsids element itself (lists all session IDs)
    for rsids_el in doc.element.findall(f".//{qn('w:rsids')}"):
        rsids_el.getparent().remove(rsids_el)
        count += 1
    if count:
        return [f"rsid attributes: {count} removed"]
    return []


def strip_para_ids(doc: DocumentObject) -> list[str]:
    """Remove w14:paraId and w14:textId from all elements."""
    count = 0
    for el in doc.element.iter():
        for attr in W14_ATTRS:
            if attr in el.attrib:
                del el.attrib[attr]
                count += 1
    if count:
        return [f"Paragraph/text IDs: {count} removed"]
    return []


def strip_proof_errors(doc: DocumentObject) -> list[str]:
    """Remove w:proofErr spellcheck markers."""
    elements = doc.element.xpath("//w:proofErr")
    count = len(elements)
    for el in elements:
        el.getparent().remove(el)
    if count:
        return [f"Spell check markers: {count} removed"]
    return []


def strip_empty_properties(doc: DocumentObject) -> list[str]:
    """Remove empty w:rPr and w:pPr elements (no children)."""
    count = 0
    for tag in [qn("w:rPr"), qn("w:pPr")]:
        for el in doc.element.iter(tag):
            if len(el) == 0 and el.text is None:
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
                    count += 1
    if count:
        return [f"Empty property elements: {count} removed"]
    return []


def strip_hidden_text(doc: DocumentObject) -> list[str]:
    """Remove runs that have w:vanish or w:webHidden in their rPr."""
    count = 0
    for rpr in doc.element.iter(qn("w:rPr")):
        vanish = rpr.find(qn("w:vanish"))
        web_hidden = rpr.find(qn("w:webHidden"))
        if vanish is not None or web_hidden is not None:
            run = rpr.getparent()
            if run is not None and run.tag == qn("w:r"):
                run_parent = run.getparent()
                if run_parent is not None:
                    run_parent.remove(run)
                    count += 1
    if count:
        return [f"Hidden text runs: {count} removed"]
    return []


def coalesce_runs(doc: DocumentObject) -> list[str]:
    """Coalesce adjacent runs with identical formatting (delegates to existing normalize_docx)."""
    # normalize_docx already handles proofErr + run coalescing,
    # but since we call strip_proof_errors separately, we only need coalescing.
    # The existing function is safe — it respects w:ins/w:del boundaries.
    normalize_docx(doc)
    return []


# ---------------------------------------------------------------------------
# Track change transforms
# ---------------------------------------------------------------------------


def count_tracked_changes(doc: DocumentObject) -> tuple[int, int]:
    """Returns (insertion_count, deletion_count) of unresolved track changes."""
    ins_count = len(doc.element.findall(f".//{qn('w:ins')}"))
    del_count = len(doc.element.findall(f".//{qn('w:del')}"))
    return ins_count, del_count


def accept_all_tracked_changes(doc: DocumentObject) -> list[str]:
    """
    Accept all tracked changes: unwrap w:ins (keep content), remove w:del (discard content).
    Also strips w:rPrChange elements.
    Returns report lines listing each change that was accepted.
    """
    lines = []

    # Collect info before mutating
    for ins in doc.element.findall(f".//{qn('w:ins')}"):
        text = _get_element_text(ins)
        if text.strip():
            lines.append(f"  Accepted insertion: \"{_truncate(text, 60)}\"")

    for d in doc.element.findall(f".//{qn('w:del')}"):
        text = _get_element_text(d)
        if text.strip():
            lines.append(f"  Accepted deletion of: \"{_truncate(text, 60)}\"")

    # Now mutate: unwrap insertions
    for ins in doc.element.findall(f".//{qn('w:ins')}"):
        parent = ins.getparent()
        if parent is None:
            continue
        index = parent.index(ins)
        for child in list(ins):
            parent.insert(index, child)
            index += 1
        parent.remove(ins)

    # Remove deletions
    for d in doc.element.findall(f".//{qn('w:del')}"):
        if d.getparent() is not None:
            d.getparent().remove(d)

    # Remove rPrChange (format change tracking)
    for rpc in doc.element.findall(f".//{qn('w:rPrChange')}"):
        if rpc.getparent() is not None:
            rpc.getparent().remove(rpc)

    # Remove pPrChange (paragraph format change tracking)
    for ppc in doc.element.findall(f".//{qn('w:pPrChange')}"):
        if ppc.getparent() is not None:
            ppc.getparent().remove(ppc)

    # Remove sectPrChange (section format change tracking)
    for spc in doc.element.findall(f".//{qn('w:sectPrChange')}"):
        if spc.getparent() is not None:
            spc.getparent().remove(spc)

    ins_count = len([l for l in lines if "insertion" in l])
    del_count = len([l for l in lines if "deletion" in l])
    total = ins_count + del_count

    if total:
        header = [f"Tracked changes auto-accepted: {total} ({ins_count} insertions, {del_count} deletions)"]
        return header + lines
    return []


# ---------------------------------------------------------------------------
# Comment transforms
# ---------------------------------------------------------------------------


def get_comments_summary(doc: DocumentObject) -> dict:
    """
    Returns a summary of comments in the document.
    Keys: 'total', 'open', 'resolved', 'comments' (list of dicts with id, author, text, resolved, heading).
    """
    cm = CommentsManager(doc)
    data = cm.extract_comments_data()

    comments = []
    open_count = 0
    resolved_count = 0

    for c_id, info in data.items():
        is_resolved = info.get("resolved", False)
        if is_resolved:
            resolved_count += 1
        else:
            open_count += 1
        comments.append({
            "id": c_id,
            "author": info.get("author", "Unknown"),
            "text": info.get("text", ""),
            "date": info.get("date", ""),
            "resolved": is_resolved,
        })

    return {
        "total": len(comments),
        "open": open_count,
        "resolved": resolved_count,
        "comments": comments,
    }


def remove_all_comments(doc: DocumentObject) -> list[str]:
    """Remove all comments from the document."""
    cm = CommentsManager(doc)
    data = cm.extract_comments_data()
    if not data:
        return []

    lines = []
    for c_id, info in data.items():
        status = "[Resolved]" if info.get("resolved") else "[Open]"
        lines.append(f"  {status} \"{_truncate(info.get('text', ''), 60)}\" ({info.get('author', 'Unknown')})")

    # Delete all comments
    for c_id in list(data.keys()):
        cm.delete_comment(c_id)

    # Strip any remaining comment range markers from body
    for tag in ["w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"]:
        for el in doc.element.findall(f".//{qn(tag)}"):
            if el.getparent() is not None:
                el.getparent().remove(el)

    header = [f"Comments removed: {len(data)} ({len([c for c in data.values() if c.get('resolved')])} resolved, {len([c for c in data.values() if not c.get('resolved')])} open)"]
    return header + lines


def remove_resolved_comments(doc: DocumentObject) -> list[str]:
    """Remove only resolved comments. Keep open ones."""
    cm = CommentsManager(doc)
    data = cm.extract_comments_data()
    if not data:
        return []

    resolved = {c_id: info for c_id, info in data.items() if info.get("resolved")}
    if not resolved:
        return []

    lines = []
    for c_id, info in resolved.items():
        lines.append(f"  [Resolved] \"{_truncate(info.get('text', ''), 60)}\" ({info.get('author', 'Unknown')})")
        cm.delete_comment(c_id)

    # Clean up orphaned range markers for deleted comments
    remaining_ids = {c_id for c_id in data if c_id not in resolved}
    for tag in ["w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"]:
        for el in doc.element.findall(f".//{qn(tag)}"):
            el_id = el.get(qn("w:id"))
            if el_id and el_id not in remaining_ids:
                if el.getparent() is not None:
                    el.getparent().remove(el)

    return [f"Resolved comments stripped: {len(resolved)}"] + lines


def replace_comment_authors(doc: DocumentObject, new_author: str) -> list[str]:
    """Replace author names on all comments with a single identity."""
    cm = CommentsManager(doc)
    if not cm.comments_part:
        return []

    original_authors = set()
    for c in cm.comments_part.element.findall(qn("w:comment")):
        author = c.get(qn("w:author"))
        if author:
            original_authors.add(author)
            c.set(qn("w:author"), new_author)
        initials = c.get(qn("w:initials"))
        if initials:
            new_initials = "".join(p[0] for p in new_author.split() if p).upper()
            c.set(qn("w:initials"), new_initials)

    if original_authors:
        return [f"Comment authors replaced: {', '.join(sorted(original_authors))} → \"{new_author}\""]
    return []


def replace_change_authors(doc: DocumentObject, new_author: str) -> list[str]:
    """Replace author names on all track changes with a single identity."""
    original_authors = set()
    for tag in [qn("w:ins"), qn("w:del"), qn("w:rPrChange"), qn("w:pPrChange")]:
        for el in doc.element.iter(tag):
            author = el.get(qn("w:author"))
            if author:
                original_authors.add(author)
                el.set(qn("w:author"), new_author)
    if original_authors:
        return [f"Track change authors replaced: {', '.join(sorted(original_authors))} → \"{new_author}\""]
    return []


# ---------------------------------------------------------------------------
# Document property transforms
# ---------------------------------------------------------------------------


def scrub_doc_properties(doc: DocumentObject) -> list[str]:
    """Scrub author/timestamp/volatile metadata from docProps."""
    lines = []

    core = doc.core_properties
    if core.author:
        lines.append(f"Author: {core.author}")
        core.author = ""
    if core.last_modified_by:
        lines.append(f"Last modified by: {core.last_modified_by}")
        core.last_modified_by = ""
    if core.title:
        # Title is often intentional, but can leak. Report it, don't strip.
        pass
    if core.revision:
        core.revision = 1

    # Strip volatile app properties via raw XML
    # python-docx doesn't expose all app.xml fields, so we go to the package
    app_part = _find_part(doc, "/docProps/app.xml")
    if app_part is not None:
        app_el = _parse_part_xml(app_part)
        if app_el is not None:
            for tag_local in ["TotalTime", "Words", "Characters", "Paragraphs",
                              "Lines", "CharactersWithSpaces", "Template",
                              "Manager", "Company"]:
                tag = f"{{{EXTENDED_NS}}}{tag_local}"
                for el in app_el.findall(f".//{tag}"):
                    if el.text:
                        if tag_local == "Template" and el.text:
                            lines.append(f"Template: {el.text}")
                        elif tag_local == "Company" and el.text:
                            lines.append(f"Company: {el.text}")
                        elif tag_local == "Manager" and el.text:
                            lines.append(f"Manager: {el.text}")
                        el.text = ""
            _write_part_xml(app_part, app_el)

    if lines:
        return ["Metadata scrubbed:"] + [f"  {l}" for l in lines]
    return []


def scrub_timestamps(doc: DocumentObject) -> list[str]:
    """Normalize timestamps in core properties."""
    core = doc.core_properties
    modified = False

    # python-docx core_properties exposes created/modified as datetime
    # We set them to None to clear, but python-docx may complain,
    # so we go to raw XML
    core_part = _find_part(doc, "/docProps/core.xml")
    if core_part is not None:
        core_el = _parse_part_xml(core_part)
        if core_el is not None:
            for tag in [f"{{{DCTERMS_NS}}}created", f"{{{DCTERMS_NS}}}modified"]:
                for el in core_el.findall(f".//{tag}"):
                    if el.text:
                        el.text = "1970-01-01T00:00:00Z"
                        modified = True
            _write_part_xml(core_part, core_el)

    if modified:
        return ["Timestamps normalized to epoch"]
    return []


# ---------------------------------------------------------------------------
# Package-level transforms
# ---------------------------------------------------------------------------


def strip_custom_xml(doc: DocumentObject) -> list[str]:
    """Remove customXml parts from the package."""
    parts_to_remove = []
    for rel in doc.part.rels.values():
        if rel.is_external:
            continue
        try:
            partname = str(rel.target_part.partname)
        except Exception:
            continue
        if "/customXml/" in partname or partname.startswith("/customXml"):
            parts_to_remove.append((rel, partname))

    if not parts_to_remove:
        return []

    for rel, partname in parts_to_remove:
        try:
            if rel.target_part in doc.part.package.parts:
                doc.part.package.parts.remove(rel.target_part)
        except Exception:
            pass

    return [f"Custom XML parts: {len(parts_to_remove)} removed"]


def audit_hyperlinks(doc: DocumentObject, internal_patterns: Optional[list[str]] = None) -> list[str]:
    """Find hyperlinks targeting internal URLs. Returns warnings (does not modify)."""
    if internal_patterns is None:
        internal_patterns = [
            "sharepoint.com",
            "onedrive.com",
            ".internal",
            "intranet",
            "localhost",
            "10.",
            "192.168.",
            "172.16.",
        ]

    warnings = []
    for rel in doc.part.rels.values():
        if not rel.is_external:
            continue
        url = str(rel.target_ref)
        for pattern in internal_patterns:
            if pattern.lower() in url.lower():
                warnings.append(f"Hyperlink targets internal URL: {_truncate(url, 80)}")
                break
    return warnings


def strip_image_alt_text(doc: DocumentObject) -> list[str]:
    """Remove auto-generated alt text (descr attributes) from images."""
    WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    count = 0
    for el in doc.element.iter(f"{{{WP_NS}}}docPr"):
        descr = el.get("descr")
        if descr:
            # Heuristic: keep if it looks intentional (long, not a filename)
            looks_like_filename = "." in descr and len(descr) < 60
            is_short_auto = len(descr) < 10
            if looks_like_filename or is_short_auto:
                del el.attrib["descr"]
                count += 1
    if count:
        return [f"Image alt text: {count} auto-generated descriptions removed"]
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_element_text(el) -> str:
    """Extract visible text from a w:ins or w:del element."""
    texts = []
    for t in el.iter(qn("w:t")):
        if t.text:
            texts.append(t.text)
    for t in el.iter(qn("w:delText")):
        if t.text:
            texts.append(t.text)
    return "".join(texts)


def _truncate(text: str, max_len: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _find_part(doc: DocumentObject, partname_suffix: str):
    """Find a part in the OPC package by partname suffix."""
    for part in doc.part.package.parts:
        if str(part.partname).endswith(partname_suffix):
            return part
    return None


def _parse_part_xml(part) -> Optional[etree._Element]:
    """Parse a part's blob as XML."""
    try:
        return etree.fromstring(part.blob)
    except Exception:
        return None


def _write_part_xml(part, element: etree._Element):
    """Write modified XML back to a part's blob."""
    part._blob = etree.tostring(element, xml_declaration=True, encoding="UTF-8", standalone=True)


def get_nearest_heading(doc: DocumentObject, target_paragraph_index: int) -> str:
    """Find the nearest heading above a given paragraph index."""
    from adeu.utils.docx import get_paragraph_prefix

    best_heading = None
    para_idx = 0

    for part in iter_document_parts(doc):
        for item in iter_block_items(part):
            if isinstance(item, Paragraph):
                prefix = get_paragraph_prefix(item)
                if prefix:
                    heading_text = item.text.strip()
                    if heading_text:
                        best_heading = heading_text
                if para_idx >= target_paragraph_index:
                    return f"§ {best_heading}" if best_heading else f"¶{target_paragraph_index}"
                para_idx += 1

    return f"§ {best_heading}" if best_heading else f"¶{target_paragraph_index}"
