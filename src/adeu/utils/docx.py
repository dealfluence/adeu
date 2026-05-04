"""
Low-level utilities for manipulating DOCX XML structures.
Contains normalization logic ported from Open-Xml-PowerTools concepts.
"""

from typing import Any, Dict, Iterator, NamedTuple, Optional, Union

import structlog
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run

logger = structlog.get_logger(__name__)


def _get_style_cache(part):
    """
    Parses styles.xml natively and builds an O(1) dictionary cache of styles.
    Bypasses the extreme overhead of python-docx's lazy-loading style wrappers.
    """
    main_part = part.package.main_document_part
    if hasattr(main_part, "_adeu_style_cache"):
        return main_part._adeu_style_cache

    cache: Dict[str, Any] = {}
    default_pstyle = None

    try:
        styles_xml = main_part.styles.element
    except Exception:
        main_part._adeu_style_cache = (cache, None)
        return cache, None

    raw_styles = {}
    # 1. Parse raw XML elements
    for s in styles_xml.findall(qn("w:style")):
        s_id = s.get(qn("w:styleId"))
        if not s_id:
            continue

        s_type = s.get(qn("w:type"))
        is_default = s.get(qn("w:default")) in ("1", "true", "on")

        if s_type == "paragraph" and is_default:
            default_pstyle = s_id

        name_el = s.find(qn("w:name"))
        name = name_el.get(qn("w:val")) if name_el is not None else s_id

        based_on_el = s.find(qn("w:basedOn"))
        based_on = based_on_el.get(qn("w:val")) if based_on_el is not None else None

        outline_lvl = None
        pPr = s.find(qn("w:pPr"))
        if pPr is not None:
            oLvl = pPr.find(qn("w:outlineLvl"))
            if oLvl is not None:
                val = oLvl.get(qn("w:val"))
                if val and val.isdigit():
                    outline_lvl = int(val)

        bold = None
        rPr = s.find(qn("w:rPr"))
        if rPr is not None:
            b = rPr.find(qn("w:b"))
            if b is not None:
                val = b.get(qn("w:val"))
                bold = val not in ("0", "false", "off")

        raw_styles[s_id] = {
            "name": name,
            "based_on": based_on,
            "outline_level": outline_lvl,
            "bold": bold,
        }

    # 2. Recursively resolve inheritance chains
    def resolve_style(s_id, visited):
        if s_id in cache:
            return cache[s_id]
        if s_id in visited or s_id not in raw_styles:
            return {"name": s_id, "outline_level": None, "bold": False}

        visited.add(s_id)
        raw = raw_styles[s_id]
        based_on_id = raw["based_on"]

        if based_on_id:
            parent = resolve_style(based_on_id, visited)
            o_lvl = raw["outline_level"] if raw["outline_level"] is not None else parent["outline_level"]
            bold_val = raw["bold"] if raw["bold"] is not None else parent["bold"]
        else:
            o_lvl = raw["outline_level"]
            bold_val = raw["bold"] if raw["bold"] is not None else False

        resolved = {"name": raw["name"], "outline_level": o_lvl, "bold": bold_val}
        cache[s_id] = resolved
        return resolved

    for s_id in raw_styles:
        resolve_style(s_id, set())

    main_part._adeu_style_cache = (cache, default_pstyle)
    return cache, default_pstyle


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
    parts = instr.split()
    if not parts:
        return False
    return parts[0] in ("PAGE", "NUMPAGES")


def get_paragraph_prefix(paragraph: Paragraph) -> str:
    """
    Returns the Markdown prefix for a paragraph based on its style.
    Uses the Fast XML Cache to avoid python-docx performance penalties.
    """
    cache, default_pstyle = _get_style_cache(paragraph.part)
    p_el = paragraph._element
    pPr = p_el.find(qn("w:pPr"))

    # 1. Check Outline Level on the paragraph itself (Structural Truth)
    if pPr is not None:
        oLvl = pPr.find(qn("w:outlineLvl"))
        if oLvl is not None:
            val = oLvl.get(qn("w:val"))
            if val and val.isdigit():
                lvl = int(val)
                if 0 <= lvl <= 8:
                    return "#" * (lvl + 1) + " "

    # 2. Get Style Name & properties from Cache
    style_id = default_pstyle
    if pPr is not None:
        pStyle = pPr.find(qn("w:pStyle"))
        if pStyle is not None:
            style_id = pStyle.get(qn("w:val")) or default_pstyle

    style_info = cache.get(style_id) if style_id else None

    if style_info:
        lvl = style_info.get("outline_level")
        if lvl is not None and 0 <= lvl <= 8:
            return "#" * (lvl + 1) + " "

    style_name = style_info["name"] if style_info else None

    # Check Style Name explicitly
    if style_name and style_name.startswith("Heading"):
        try:
            level = int(style_name.replace("Heading", "").strip())
            return "#" * level + " "
        except ValueError:
            pass

    if style_name == "Title":
        return "# "

    # 3. Check for List Formatting
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

    # 4. Heuristic for "Normal" style headers
    if style_name is None or style_name == "Normal":
        text = paragraph.text.strip()
        if text and len(text) < 100:
            is_all_caps = text.isupper()

            is_bold = False
            if style_info and style_info.get("bold"):
                is_bold = True
            else:
                # Check if the first visible run is explicitly bold in XML
                runs = p_el.findall(f".//{qn('w:r')}")
                for r in runs:
                    t = r.find(f".//{qn('w:t')}")
                    if t is not None and t.text and t.text.strip():
                        rPr_run = r.find(qn("w:rPr"))
                        if rPr_run is not None:
                            b = rPr_run.find(qn("w:b"))
                            if b is not None:
                                val = b.get(qn("w:val"))
                                if val not in ("0", "false", "off"):
                                    is_bold = True
                        break

            if is_all_caps and is_bold:
                return "## "

    return ""


def get_run_style_markers(run: Run) -> tuple[str, str]:
    """
    Returns markdown prefix/suffix for run formatting (bold/italic).
    Bypasses `run.bold` and `run.italic` attributes for massive OXML performance gains.
    """
    prefix = ""
    suffix = ""

    # 1. Determine if paragraph is a heading (using fast cache)
    para_el = run._element.getparent()
    while para_el is not None and para_el.tag != qn("w:p"):
        para_el = para_el.getparent()

    is_heading = False
    if para_el is not None:
        para_part = getattr(run._parent, "part", None)
        if para_part:
            cache, default_pstyle = _get_style_cache(para_part)

            pPr = para_el.find(qn("w:pPr"))
            style_id = default_pstyle
            if pPr is not None:
                pStyle = pPr.find(qn("w:pStyle"))
                if pStyle is not None:
                    style_id = pStyle.get(qn("w:val")) or default_pstyle

            style_info = cache.get(style_id) if style_id else None
            if style_info:
                name = style_info["name"]
                if name.startswith("Heading") or name == "Title":
                    is_heading = True

    # 2. Check explicitly defined run properties in XML
    rPr = run._element.find(qn("w:rPr"))
    is_bold = False
    is_italic = False

    if rPr is not None:
        b = rPr.find(qn("w:b"))
        if b is not None:
            val = b.get(qn("w:val"))
            if val not in ("0", "false", "off"):
                is_bold = True

        i = rPr.find(qn("w:i"))
        if i is not None:
            val = i.get(qn("w:val"))
            if val not in ("0", "false", "off"):
                is_italic = True

    # Nesting order: Bold outer, Italic inner -> **_text_**
    if is_bold and not is_heading:
        prefix += "**"
        suffix = "**" + suffix

    if is_italic:
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
