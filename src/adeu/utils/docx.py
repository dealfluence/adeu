"""
Low-level utilities for manipulating DOCX XML structures.
Contains normalization logic ported from Open-Xml-PowerTools concepts.
"""

from typing import Iterator, NamedTuple, Optional, Union, cast

import structlog
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run

logger = structlog.get_logger(__name__)


# --- Types ---
class DocxEvent(NamedTuple):
    type: str  # 'start', 'end', 'ref' (for comments); 'ins_start', etc.
    id: str
    author: Optional[str] = None
    date: Optional[str] = None


ParagraphItem = Union[Run, DocxEvent]


class NotesPart:
    def __init__(self, part, note_type="fn"):
        from docx.oxml import parse_xml

        self.part = part
        if not hasattr(part, "_adeu_element"):
            part._adeu_element = parse_xml(part.blob)
        self._element = part._adeu_element
        self.note_type = note_type


class FootnoteItem:
    def __init__(self, element, parent, note_type="fn"):
        self._element = element
        self._parent = parent
        self.part = parent.part
        self.id = element.get(qn("w:id"))
        self.note_type = note_type


def create_element(name: str):
    return OxmlElement(name)


def create_attribute(element, name: str, value: str):
    element.set(qn(name), value)


def _is_page_instr(instr: str) -> bool:
    if not instr:
        return False
    instr = instr.upper().strip()
    # Check for PAGE or NUMPAGES keyword at start of instruction
    parts = instr.split()
    if not parts:
        return False
    return parts[0] in ("PAGE", "NUMPAGES")


def _get_paragraph_style_safe(paragraph: Paragraph):
    try:
        return paragraph.style
    except AttributeError:
        # Handle generic XmlParts (like footnotes.xml) that lack get_style()
        return None


def get_paragraph_prefix(paragraph: Paragraph) -> str:
    """
    Returns the Markdown prefix for a paragraph based on its style.
    e.g. 'Heading 1' -> '# ', 'Heading 2' -> '## '
    """
    # 1. Check Outline Level (Structural Truth)
    try:
        lvl = paragraph.paragraph_format.outline_level
        if lvl is not None and 0 <= lvl <= 8:
            return "#" * (lvl + 1) + " "
    except Exception:
        pass

    # NOTE: Do NOT bail early when paragraph.style is None. python-docx returns
    # None for paragraphs without an explicit <w:pStyle>, even though Word
    # treats them as the default "Normal" style. Bailing here misses the
    # all-caps bold heuristic that catches manually-formatted section titles.
    style_name = None
    style = _get_paragraph_style_safe(paragraph)
    if style is not None:
        style_name = style.name

    # 2. Check Style Name
    if style_name and style_name.startswith("Heading"):
        try:
            level = int(style_name.replace("Heading", "").strip())
            return "#" * level + " "
        except ValueError:
            pass

    if style_name == "Title":
        return "# "

    # 3. Check for List Formatting
    pPr = paragraph._element.find(qn("w:pPr"))
    if pPr is not None:
        numPr = pPr.find(qn("w:numPr"))
        if numPr is not None:
            numId = numPr.find(qn("w:numId"))
            if numId is not None:
                val = numId.get(qn("w:val"))
                if val and val != "0":
                    ilvl_str = "0"
                    ilvl = numPr.find(qn("w:ilvl"))
                    if ilvl is not None:
                        val_attr = ilvl.get(qn("w:val"))
                        if val_attr is not None:
                            ilvl_str = val_attr
                    try:
                        level = int(ilvl_str)
                    except ValueError:
                        level = 0
                    return ("    " * level) + "* "

    # 4. Heuristic for "Normal" style headers (Lazy Lawyer / Manually formatted)
    # If text is short (<100 chars), All Caps, and Bold -> Likely a Header.
    # Treat None-style as Normal (python-docx returns None for paragraphs
    # with no explicit <w:pStyle>, but Word renders them as Normal).
    if style_name is None or style_name == "Normal":
        text = paragraph.text.strip()
        if text and len(text) < 100:
            is_all_caps = text.isupper()

            # Check for Bold (paragraph style OR explicit run formatting)
            is_bold = False
            if style is not None and style.font.bold:
                is_bold = True
            else:
                runs = [r for r in paragraph.runs if r.text.strip()]
                if runs and runs[0].bold:
                    is_bold = True

            if is_all_caps and is_bold:
                return "## "

    return ""


def get_run_style_markers(run: Run) -> tuple[str, str]:
    """
    Returns markdown prefix/suffix for run formatting (bold/italic).
    Only returns markers for explicit formatting to avoid clutter.

    Suppresses ** on runs inside heading paragraphs: heading styles are
    visually bold by default, so emitting ** would produce redundant markup
    (e.g. "# **1. Purpose**"). Equally important: Word strips redundant
    explicit <w:b/> tags from heading runs when serializing via
    doc.WordOpenXML, so without this suppression the same document renders
    differently depending on whether it was read from disk (keeps the tags)
    or from the live canvas (loses them).
    """
    prefix = ""
    suffix = ""

    # Detect heading context for the run's parent paragraph.
    # python-docx types Run._parent as ProvidesStoryPart (the broader
    # story-container protocol, which has no .style attribute). In practice
    # runs emitted by iter_paragraph_content are always constructed with a
    # Paragraph parent, so the cast is truthful at runtime.
    para = cast(Paragraph, run._parent)
    para_style = _get_paragraph_style_safe(para)
    para_style_name = para_style.name if para_style else None

    is_heading = bool(para_style_name and (para_style_name.startswith("Heading") or para_style_name == "Title"))

    # Nesting order: Bold outer, Italic inner -> **_text_**
    if run.bold and not is_heading:
        prefix += "**"
        suffix = "**" + suffix

    if run.italic:
        prefix += "_"
        suffix = "_" + suffix

    return prefix, suffix


def apply_formatting_to_segments(text: str, prefix: str, suffix: str) -> str:
    """
    Applies formatting markers to text, ensuring newlines are excluded from the formatting.
    Example: "**A\nB**" -> "**A**\n**B**"
    """
    if not prefix and not suffix:
        return text
    if not text:
        return ""

    if "\n" not in text:
        return f"{prefix}{text}{suffix}"

    parts = text.split("\n")
    return "\n".join(f"{prefix}{p}{suffix}" if p else "" for p in parts)


def iter_paragraph_content(paragraph: Paragraph) -> Iterator[ParagraphItem]:
    """
    Iterates over the content of a paragraph, yielding both Runs and Comment events.
    This allows reconstruction of text with inline comments using CriticMarkup.
    """
    # State for complex fields (w:fldChar)
    in_complex_field = False
    current_instr = ""
    hide_result = False

    def process_run_element(r_element):
        nonlocal in_complex_field, current_instr, hide_result

        # Check for inline Tracked Formatting (w:rPrChange)
        rPr = r_element.find(qn("w:rPr"))
        rPrChange = rPr.find(qn("w:rPrChange")) if rPr is not None else None
        if rPrChange is not None:
            c_id = rPrChange.get(qn("w:id"))
            c_auth = rPrChange.get(qn("w:author"))
            c_date = rPrChange.get(qn("w:date"))
            yield DocxEvent("fmt_start", c_id, c_auth, c_date)

        # Check for inline Tracked Formatting (w:rPrChange) anywhere in the run
        rPrChange = r_element.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPrChange")
        if rPrChange is not None:
            c_id = rPrChange.get(qn("w:id"))
            c_auth = rPrChange.get(qn("w:author"))
            c_date = rPrChange.get(qn("w:date"))
            yield DocxEvent("fmt_start", c_id, c_auth, c_date)

        # Check for inline commentReference (sometimes embedded in run)
        for child in r_element:
            if child.tag == qn("w:commentReference"):
                c_id = child.get(qn("w:id"))
                if c_id:
                    yield DocxEvent("ref", c_id)
            elif child.tag == qn("w:footnoteReference"):
                f_id = child.get(qn("w:id"))
                if f_id:
                    yield DocxEvent("footnote", f_id)
            elif child.tag == qn("w:endnoteReference"):
                e_id = child.get(qn("w:id"))
                if e_id:
                    yield DocxEvent("endnote", e_id)

        # 1. Parse Field Characters (begin/separate/end)
        for fchar in r_element.findall(qn("w:fldChar")):
            fld_type = fchar.get(qn("w:fldCharType"))
            if fld_type == "begin":
                in_complex_field = True
                current_instr = ""
            elif fld_type == "separate":
                # End of instruction, start of visible result
                if _is_page_instr(current_instr):
                    hide_result = True
                else:
                    parts = current_instr.strip().split()
                    if parts and parts[0] == "REF" and len(parts) > 1:
                        yield DocxEvent("xref_start", parts[1])
            elif fld_type == "end":
                if not hide_result:
                    parts = current_instr.strip().split()
                    if parts and parts[0] == "REF" and len(parts) > 1:
                        yield DocxEvent("xref_end", parts[1])
                in_complex_field = False
                current_instr = ""
                hide_result = False

        # 2. Accumulate Instruction Text
        if in_complex_field and not hide_result:
            for instr in r_element.findall(qn("w:instrText")):
                if instr.text:
                    current_instr += instr.text

        # 3. Yield Run (if not hidden)
        if not hide_result:
            yield Run(r_element, paragraph)

        if rPrChange is not None:
            yield DocxEvent("fmt_end", c_id)

    def traverse_node(node):
        for child in node:
            tag = child.tag
            if tag == qn("w:r"):
                # Standard run
                yield from process_run_element(child)
            elif tag == qn("w:ins"):
                i_id = child.get(qn("w:id"))
                i_auth = child.get(qn("w:author"))
                i_date = child.get(qn("w:date"))
                yield DocxEvent("ins_start", i_id, i_auth, i_date)
                yield from traverse_node(child)
                yield DocxEvent("ins_end", i_id)
            elif tag == qn("w:del"):
                d_id = child.get(qn("w:id"))
                d_auth = child.get(qn("w:author"))
                d_date = child.get(qn("w:date"))
                yield DocxEvent("del_start", d_id, d_auth, d_date)
                yield from traverse_node(child)
                yield DocxEvent("del_end", d_id)
            elif tag == qn("w:commentRangeStart"):
                c_id = child.get(qn("w:id"))
                yield DocxEvent("start", c_id)
            elif tag == qn("w:commentRangeEnd"):
                c_id = child.get(qn("w:id"))
                yield DocxEvent("end", c_id)
            elif tag == qn("w:commentReference"):
                # Reference directly in paragraph
                pass
            elif tag == qn("w:hyperlink"):
                rId = child.get(qn("r:id"))
                url = ""
                if rId and paragraph.part:
                    try:
                        rel = paragraph.part.rels[rId]
                        if rel.is_external:
                            url = rel.target_ref
                    except KeyError:
                        pass
                if url:
                    yield DocxEvent("hyperlink_start", rId, date=url)  # reuse date field for url
                yield from traverse_node(child)
                if url:
                    yield DocxEvent("hyperlink_end", rId, date=url)
            elif tag == qn("w:fldSimple"):
                instr = child.get(qn("w:instr"), "")
                target = ""
                if " REF " in instr or instr.startswith("REF "):
                    parts = instr.strip().split()
                    if len(parts) > 1 and parts[0] == "REF":
                        target = parts[1]
                if target:
                    yield DocxEvent("xref_start", target)
                yield from traverse_node(child)
                if target:
                    yield DocxEvent("xref_end", target)
            elif tag == qn("w:bookmarkStart"):
                b_name = child.get(qn("w:name"))
                if b_name and (not b_name.startswith("_") or b_name.startswith("_Ref")):
                    yield DocxEvent("bookmark", b_name)
            elif tag in (qn("w:sdt"), qn("w:smartTag"), qn("w:sdtContent")):
                yield from traverse_node(child)

    yield from traverse_node(paragraph._element)


def get_visible_runs(paragraph: Paragraph):
    """
    Iterates over runs in a paragraph, including those inside <w:ins> tags.
    Effectively returns the 'Accepted Changes' view of the runs.
    Filters out dynamic page number fields ({PAGE}, {NUMPAGES}).
    """
    return [item for item in iter_paragraph_content(paragraph) if isinstance(item, Run)]


def get_run_text(run: Run) -> str:
    """
    Extracts text from a run, converting <w:tab/> to spaces and <w:br/> to newlines.
    Standard run.text ignores these.
    """
    text = ""
    for child in run._element:
        if child.tag == qn("w:t") or child.tag == qn("w:delText"):
            # Fix 5.1: Normalize literal tabs to spaces to match w:tab behavior
            raw = child.text or ""
            text += raw.replace("\t", " ")
        elif child.tag == qn("w:tab"):
            text += " "  # Convert tab to space
        elif child.tag == qn("w:br"):
            text += "\n"
        elif child.tag == qn("w:cr"):
            text += "\n"
    return text


def _are_runs_identical(r1: Run, r2: Run) -> bool:
    """
    Compares two runs to see if they have identical formatting properties.
    """
    rPr1 = r1._r.rPr
    rPr2 = r2._r.rPr

    xml1 = rPr1.xml if rPr1 is not None else ""
    xml2 = rPr2.xml if rPr2 is not None else ""

    return xml1 == xml2


def _has_special_content(run: Run) -> bool:
    """
    Checks if the run contains elements that are not simple text, which would be lost
    during text-only coalescing (e.g. w:commentReference, w:drawing).
    """
    # Safe tags that are captured by run.text or are properties
    SAFE_TAGS = {
        qn("w:t"),
        qn("w:tab"),
        qn("w:br"),
        qn("w:cr"),
        qn("w:delText"),
        qn("w:rPr"),
    }

    for child in run._element:
        if child.tag not in SAFE_TAGS:
            return True
    return False


def _coalesce_runs_in_container(container_element, parent_paragraph):
    children = list(container_element)
    i = 0
    while i < len(children) - 1:
        curr = children[i]
        nxt = children[i + 1]

        if curr.tag == qn("w:r") and nxt.tag == qn("w:r"):
            r1 = Run(curr, parent_paragraph)
            r2 = Run(nxt, parent_paragraph)
            if not _has_special_content(r1) and not _has_special_content(r2):
                if _are_runs_identical(r1, r2):
                    # Find the last text node in the current run to merge into
                    last_t = None
                    for c in curr:
                        if c.tag in (qn("w:t"), qn("w:delText")):
                            last_t = c

                    for child in list(nxt):
                        if child.tag == qn("w:rPr"):
                            continue
                        if child.tag in (qn("w:t"), qn("w:delText")) and last_t is not None and last_t.tag == child.tag:
                            # Concatenate text instead of creating sibling text nodes
                            t1 = last_t.text or ""
                            t2 = child.text or ""
                            combined = t1 + t2
                            last_t.text = combined
                            if combined.strip() != combined:
                                last_t.set(qn("xml:space"), "preserve")
                        else:
                            curr.append(child)
                            if child.tag in (qn("w:t"), qn("w:delText")):
                                last_t = child
                    container_element.remove(nxt)
                    children.pop(i + 1)
                    continue

        if curr.tag in (
            qn("w:ins"),
            qn("w:del"),
            qn("w:hyperlink"),
            qn("w:sdt"),
            qn("w:smartTag"),
            qn("w:fldSimple"),
            qn("w:sdtContent"),
        ):
            _coalesce_runs_in_container(curr, parent_paragraph)

        i += 1

    if children and children[-1].tag in (
        qn("w:ins"),
        qn("w:del"),
        qn("w:hyperlink"),
        qn("w:sdt"),
        qn("w:smartTag"),
        qn("w:fldSimple"),
        qn("w:sdtContent"),
    ):
        _coalesce_runs_in_container(children[-1], parent_paragraph)


def _coalesce_runs_in_paragraph(paragraph: Paragraph):
    """
    Merges adjacent runs with identical formatting.
    This fixes issues where words are split like ["Con", "tract"] due to editing history.
    """
    _coalesce_runs_in_container(paragraph._element, paragraph)


def iter_document_parts(doc: DocumentObject):
    """
    Yields document parts in a linear order for processing:
    1. Unique Headers (Primary, First, Even)
    2. Main Body
    3. Unique Footers (Primary, First, Even)

    Handles 'Link to Previous' to avoid duplication.
    """

    def _iter_section_parts(section, part_type_attr):
        # 1. Primary
        part = getattr(section, part_type_attr)
        if not part.is_linked_to_previous:
            yield part

        # 2. First Page
        if section.different_first_page_header_footer:
            first = getattr(section, f"first_page_{part_type_attr}")
            if not first.is_linked_to_previous:
                yield first

        # 3. Even Page
        if doc.settings.odd_and_even_pages_header_footer:
            even = getattr(section, f"even_page_{part_type_attr}")
            if not even.is_linked_to_previous:
                yield even

    # 1. Headers
    for section in doc.sections:
        yield from _iter_section_parts(section, "header")

    # 2. Main Body (The Document object itself acts as the container)
    yield doc

    # 3. Footers
    for section in doc.sections:
        yield from _iter_section_parts(section, "footer")

    # 4. Footnotes & Endnotes (ordered)
    fn_part = None
    en_part = None
    for part in doc.part.package.parts:
        part_name = str(part.partname)
        if part_name.endswith("footnotes.xml"):
            fn_part = part
        elif part_name.endswith("endnotes.xml"):
            en_part = part

    if fn_part:
        yield NotesPart(fn_part, "fn")
    if en_part:
        yield NotesPart(en_part, "en")


def normalize_docx(doc: DocumentObject):
    """
    Applies normalization to a DOCX document to make text mapping reliable.
    1. Removes proof errors (spellcheck squiggles).
    2. Coalesces adjacent runs.
    """
    logger.info("Normalizing DOCX structure...")

    # Remove proof errors (spelling/grammar tags) via XPath
    for proof_err in doc.element.xpath("//w:proofErr"):
        proof_err.getparent().remove(proof_err)

    # Coalesce all parts (Headers, Body, Footers)
    # AND perform recursive coalescing for tables
    for part in iter_document_parts(doc):
        for item in iter_block_items(part):
            if isinstance(item, Paragraph):
                _coalesce_runs_in_paragraph(item)
            elif isinstance(item, Table):
                _normalize_table(item)


def _normalize_table(table: Table):
    for row in table.rows:
        for cell in row.cells:
            for item in iter_block_items(cell):
                if isinstance(item, Paragraph):
                    _coalesce_runs_in_paragraph(item)
                elif isinstance(item, Table):
                    _normalize_table(item)


def iter_block_items(parent) -> Iterator[Union[Paragraph, Table, FootnoteItem]]:
    """
    Yields Paragraph or Table objects in the order they appear in the XML.
    Supports Document, Header, Footer, and Cell objects.
    Recursion is left to the caller.
    """
    if isinstance(parent, DocumentObject):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    elif type(parent).__name__ == "NotesPart":
        tag = "w:footnote" if parent.note_type == "fn" else "w:endnote"
        for child in parent._element.findall(qn(tag)):
            if child.get(qn("w:type")) in ("separator", "continuationSeparator"):
                continue
            yield FootnoteItem(child, parent, parent.note_type)
        return
    elif type(parent).__name__ == "FootnoteItem":
        parent_elm = parent._element
    else:
        # Header/Footer usually expose ._element or can be iterated
        if hasattr(parent, "_element"):
            parent_elm = parent._element
        else:
            raise ValueError(f"Unsupported parent type for iteration: {type(parent)}")

    for child in parent_elm.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)
