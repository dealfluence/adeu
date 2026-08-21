"""
Low-level utilities for manipulating DOCX XML structures.
Contains normalization logic ported from Open-Xml-PowerTools concepts.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, NamedTuple, Optional, Tuple, Union, cast

import structlog
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell, _Row
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.utils.content_controls import BALLOT_GLYPHS, BlockSdt, SdtEvent

logger = structlog.get_logger(__name__)


_CUSTOM_HEADING_NAME_RE = re.compile(r"Heading[ ]?([1-6])(?![0-9])")

# Guards against a malformed document whose vMerge="continue" chain loops.
_MAX_VMERGE_DEPTH = 100

# Guards against unbounded recursion on pathologically nested content controls.
_MAX_SDT_NESTING_DEPTH = 100

# Cache qn() strings for massive performance gains in the extraction hot loop
QN_W_P = qn("w:p")
QN_W_R = qn("w:r")
QN_W_T = qn("w:t")
QN_W_DELTEXT = qn("w:delText")
QN_W_TAB = qn("w:tab")
QN_W_BR = qn("w:br")
QN_W_CR = qn("w:cr")
# Rendered run content that used to fall through the projection silently:
#   w:noBreakHyphen is a real hyphen glyph, so dropping it merged the word
#     either side of it ("e-mail" projected as "email").
#   w:ptab is an absolute-position tab; it separates content like w:tab.
# w:softHyphen is deliberately NOT here: it is an optional break hint Word
# renders only when it actually breaks the line, so projecting nothing is
# correct. w:sym is also still dropped - see AI_CONTEXT / CC-1, it needs a
# font-aware decision (symbol fonts map glyphs into the private-use area,
# so the code point alone does not identify the character).
QN_W_NOBREAKHYPHEN = qn("w:noBreakHyphen")
QN_W_PTAB = qn("w:ptab")
QN_W_RPR = qn("w:rPr")
QN_W_RPRCHANGE = qn("w:rPrChange")
QN_W_COMMENTREFERENCE = qn("w:commentReference")
QN_W_FOOTNOTEREFERENCE = qn("w:footnoteReference")
QN_W_ENDNOTEREFERENCE = qn("w:endnoteReference")
QN_W_FLDCHAR = qn("w:fldChar")
QN_W_FLDCHARTYPE = qn("w:fldCharType")
QN_W_INSTRTEXT = qn("w:instrText")
QN_W_INS = qn("w:ins")
QN_W_DEL = qn("w:del")
QN_W_ID = qn("w:id")
QN_W_AUTHOR = qn("w:author")
QN_W_DATE = qn("w:date")
QN_W_COMMENTRANGESTART = qn("w:commentRangeStart")
QN_W_COMMENTRANGEEND = qn("w:commentRangeEnd")
QN_W_HYPERLINK = qn("w:hyperlink")
QN_R_ID = qn("r:id")
QN_W_FLDSIMPLE = qn("w:fldSimple")
QN_W_INSTR = qn("w:instr")
QN_W_BOOKMARKSTART = qn("w:bookmarkStart")
QN_W_NAME = qn("w:name")
QN_W_SDT = qn("w:sdt")
QN_W_SMARTTAG = qn("w:smartTag")
QN_W_SDTCONTENT = qn("w:sdtContent")
QN_W_B = qn("w:b")
QN_W_I = qn("w:i")
QN_W_VAL = qn("w:val")
QN_W_TYPE = qn("w:type")
QN_W_PPR = qn("w:pPr")
QN_W_PSTYLE = qn("w:pStyle")
QN_W_OUTLINELVL = qn("w:outlineLvl")
QN_W_NUMPR = qn("w:numPr")
QN_W_NUMID = qn("w:numId")
QN_W_ILVL = qn("w:ilvl")
QN_W_DRAWING = qn("w:drawing")
QN_W_TC = qn("w:tc")
QN_W_TR = qn("w:tr")
QN_W_OBJECT = qn("w:object")
QN_W_PICT = qn("w:pict")
QN_WP_DOCPR = qn("wp:docPr")
QN_V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
QN_O_TITLE = "{urn:schemas-microsoft-com:office:office}title"
QN_W_TXBXCONTENT = qn("w:txbxContent")
# python-docx's nsmap has no 'mc' prefix; use literal Clark notation like the
# VML/office names above.
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
QN_MC_ALTERNATECONTENT = f"{{{_MC_NS}}}AlternateContent"
QN_MC_CHOICE = f"{{{_MC_NS}}}Choice"
QN_MC_FALLBACK = f"{{{_MC_NS}}}Fallback"

# Disclosure cap for text projected out of a floating text box marker — long
# boxed content is truncated, the marker is a disclosure, not a full view.
_TEXTBOX_DISCLOSURE_CAP = 300

# Toggle-property "off" values: <w:b w:val="0|false|off"/> means NOT bold.
# Any other value (including a missing w:val) means the toggle is on.
_OFF_VALS = ("0", "false", "off")
# A page break projects as U+000C FORM FEED — the conventional plain-text page
# separator — so that pagination can find manual breaks without putting markup
# in the character stream an LLM reads. Kept identical in the Node engine
# (utils/docx.ts get_run_text) and consumed by pagination._tokenize_into_atomic_blocks.
#
# It was literal `<w:br w:type="page"/>` markup until 2026-08-21 (CC-10): 22 characters
# of XML in the projection, and a silent parity break, since Node emitted "\n".
_PAGE_BREAK_TOKEN = "\f"
# Public alias: pagination imports this rather than re-spelling the character,
# so the producer and the consumer of the signal cannot drift apart.
PAGE_BREAK_TOKEN = _PAGE_BREAK_TOKEN


def _textbox_text(graphic_el) -> str:
    """
    Concatenated, whitespace-normalized visible text of every w:txbxContent
    under `graphic_el` (a w:drawing / w:object / w:pict), truncated to
    _TEXTBOX_DISCLOSURE_CAP chars. Empty string when the shape holds no text
    (plain images, watermark textpaths).
    """
    parts: list[str] = []
    for tc in graphic_el.findall(f".//{QN_W_TXBXCONTENT}"):
        for t in tc.iter(QN_W_T):
            if t.text:
                parts.append(t.text)
    text = " ".join(" ".join(parts).split())
    if len(text) > _TEXTBOX_DISCLOSURE_CAP:
        text = text[:_TEXTBOX_DISCLOSURE_CAP].rstrip() + "…"
    return text


def _graphic_marker_events(child):
    """
    Projects a read-only marker for a graphic run child (w:drawing, w:object
    or w:pict), rendered downstream as ![alt](docx-image:id).

    Floating text boxes get their text quoted in the alt ("Text box: …") so
    boxed obligations are DISCLOSED instead of silently invisible
    (QA 2026-07-23 customer C4). Textpath-only VML shapes (watermarks) still
    project nothing — sanitize's watermark audit reports those.
    """
    tag = child.tag
    if tag == QN_W_DRAWING or tag == QN_W_OBJECT:
        # Inline image/object: project a read-only marker so the agent
        # can see that a material element exists here (QA 2026-07-18 M5).
        doc_pr = child.find(f".//{QN_WP_DOCPR}")
        alt = ""
        img_id = "0"
        if doc_pr is not None:
            alt = doc_pr.get("descr") or doc_pr.get("title") or ""
            img_id = doc_pr.get("id") or "0"
        boxed = _textbox_text(child)
        if boxed:
            alt = f"Text box ({alt}): {boxed}" if alt else f"Text box: {boxed}"
        yield DocxEvent("image", img_id, date=alt)
    elif tag == QN_W_PICT:
        # Legacy VML. Actual images (v:imagedata) and text boxes get a
        # marker; textpath-only shapes (watermarks) stay silent.
        imagedata = child.find(f".//{QN_V_IMAGEDATA}")
        alt = (imagedata.get(QN_O_TITLE) or "") if imagedata is not None else ""
        boxed = _textbox_text(child)
        if boxed:
            alt = f"Text box ({alt}): {boxed}" if alt else f"Text box: {boxed}"
        if boxed or imagedata is not None:
            yield DocxEvent("image", "vml", date=alt)


def prefix_is_heading(prefix: str) -> bool:
    """The heading test, expressed over an already-computed paragraph prefix.

    Kept as its own function so the rule lives in exactly ONE place: both
    Virtual Text twins must agree on what counts as a heading, and callers
    that already hold the prefix reuse this instead of re-deriving it.
    """
    if not prefix:
        return False
    stripped = prefix.rstrip()
    return bool(stripped) and stripped == "#" * len(stripped)


def is_heading_paragraph(
    paragraph: Paragraph,
    style_cache: Optional[dict] = None,
    default_pstyle: Optional[str] = None,
    prefix: Optional[str] = None,
) -> bool:
    """
    Returns True iff `paragraph` projects with a Markdown heading prefix
    ('# ' through '###### '). Used by ingest and the mapper to decide
    whether to strip leading whitespace-only runs (which would otherwise
    project as '## \nText' instead of '## Text').

    `prefix` lets a caller that ALREADY computed get_paragraph_prefix for
    this paragraph pass it in. Both projection twins do exactly that (they
    need the prefix to emit it), and re-deriving it here cost a second
    full get_paragraph_prefix per paragraph — 41,190 redundant calls on the
    VVBIG stress document. Pass the UNDECORATED style prefix: callers that
    prepend a footnote label ("[^fn-1]: ") must pass the value before
    decoration, since the heading test is about the paragraph's own style.
    """
    if prefix is None:
        prefix = get_paragraph_prefix(paragraph, style_cache, default_pstyle)
    return prefix_is_heading(prefix)


def paragraph_mark_is_deleted(p_element) -> bool:
    """True when the paragraph's own break is a pending tracked deletion
    (<w:del> inside pPr/rPr) — accepting it removes the paragraph container.

    Shared by both Virtual Text twins: ingest._extract_blocks drops such a
    paragraph from the clean view when nothing visible survives inside it,
    and DocumentMapper._map_blocks must drop it identically. Keeping one
    predicate is what keeps the twins byte-identical (see
    tests/test_twin_projection_parity.py).
    """
    pPr = p_element.find(QN_W_PPR)
    if pPr is None:
        return False
    rPr = pPr.find(QN_W_RPR)
    return rPr is not None and rPr.find(QN_W_DEL) is not None


def _get_part_safely(obj: Any) -> Any:
    if obj is None:
        return None
    try:
        return obj.part
    except Exception:
        return None


def is_native_heading(
    paragraph: Any,
    style_cache: Optional[dict] = None,
    default_pstyle: Optional[str] = None,
) -> bool:
    """
    Returns True if the paragraph is a native heading (Outline Level 0-8 or Heading style),
    excluding heuristic headings.
    """
    if style_cache is None:
        style_cache, default_pstyle = _get_style_cache(_get_part_safely(paragraph))
    p_el = paragraph._element if hasattr(paragraph, "_element") else paragraph
    pPr = p_el.find(QN_W_PPR)

    # 1. Outline Level
    if pPr is not None:
        oLvl = pPr.find(QN_W_OUTLINELVL)
        if oLvl is not None:
            val = oLvl.get(QN_W_VAL)
            if val and val.isdigit():
                lvl = int(val)
                if 0 <= lvl <= 8:
                    return True

    # 2. Style Name
    style_id = default_pstyle
    if pPr is not None:
        pStyle = pPr.find(QN_W_PSTYLE)
        if pStyle is not None:
            style_id = pStyle.get(QN_W_VAL) or default_pstyle

    style_info = style_cache.get(style_id) if style_cache and style_id else None

    if style_info:
        lvl = style_info.get("outline_level")
        if lvl is not None and 0 <= lvl <= 8:
            return True

    style_name = style_info["name"] if style_info else None

    if style_name and style_name.startswith("Heading"):
        return True
    if style_name == "Title":
        return True
    if style_name and style_name != "Normal":
        if _detect_heading_level_from_name(style_name) is not None:
            return True

    return False


def _detect_heading_level_from_name(name: str) -> Optional[int]:
    """
    Last-resort fallback: returns 1..6 if the style name contains a
    'Heading N' token (case-sensitive, anchored on a non-digit boundary
    after N). Used when a custom style was generated by Word's
    'Save Selection as a New Quick Style' flow without a basedOn link
    or explicit outlineLvl.

    Examples:
      'StyleHeading2NotItalicBefore0pt' -> 2
      'MyHeading3'                      -> 3
      'Heading10Custom'                 -> None  (digit boundary blocks it)
      'SubHeadingCallout'               -> None  (no digit after Heading)
      'heading 2'                       -> None  (we leave the regular
                                                  startswith('Heading')
                                                  path to handle this —
                                                  it's the canonical
                                                  python-docx name)
    """
    if not name:
        return None
    m = _CUSTOM_HEADING_NAME_RE.search(name)
    if not m:
        return None
    return int(m.group(1))


def _get_style_cache(part):
    """
    Parses styles.xml natively and builds an O(1) dictionary cache of styles.
    Bypasses the extreme overhead of python-docx's lazy-loading style wrappers.
    """
    if part is None:
        return {}, None
    if hasattr(part, "part"):
        part = part.part
    if not hasattr(part, "package"):
        return {}, None
    pkg = part.package
    if hasattr(pkg, "_adeu_style_cache"):
        return pkg._adeu_style_cache

    cache: Dict[str, Any] = {}
    default_pstyle = None
    main_part = pkg.main_document_part

    try:
        styles_xml = main_part.styles.element
    except Exception:
        pkg._adeu_style_cache = (cache, None)
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
        num_id = None
        num_ilvl = None
        pPr = s.find(qn("w:pPr"))
        if pPr is not None:
            oLvl = pPr.find(qn("w:outlineLvl"))
            if oLvl is not None:
                val = oLvl.get(qn("w:val"))
                if val and val.isdigit():
                    outline_lvl = int(val)
            # Style-level list binding: Word's built-in "List Bullet" /
            # "List Number" styles carry <w:numPr> in styles.xml, not on the
            # paragraph. Without this, style-based lists project as plain
            # paragraphs and the agent loses ordered-vs-unordered semantics
            # (QA 2026-07-18 M4).
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                numId_el = numPr.find(qn("w:numId"))
                if numId_el is not None:
                    n_val = numId_el.get(qn("w:val"))
                    if n_val and n_val != "0":
                        num_id = n_val
                ilvl_el = numPr.find(qn("w:ilvl"))
                if ilvl_el is not None:
                    i_val = ilvl_el.get(qn("w:val"))
                    if i_val and i_val.isdigit():
                        num_ilvl = int(i_val)

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
            "num_id": num_id,
            "num_ilvl": num_ilvl,
        }

    # 2. Recursively resolve inheritance chains
    def resolve_style(s_id, visited):
        if s_id in cache:
            return cache[s_id]
        if s_id in visited or s_id not in raw_styles:
            return {"name": s_id, "outline_level": None, "bold": False, "num_id": None, "num_ilvl": None}

        visited.add(s_id)
        raw = raw_styles[s_id]
        based_on_id = raw["based_on"]

        if based_on_id:
            parent = resolve_style(based_on_id, visited)
            o_lvl = raw["outline_level"] if raw["outline_level"] is not None else parent["outline_level"]
            bold_val = raw["bold"] if raw["bold"] is not None else parent["bold"]
            n_id = raw["num_id"] if raw["num_id"] is not None else parent.get("num_id")
            n_ilvl = raw["num_ilvl"] if raw["num_ilvl"] is not None else parent.get("num_ilvl")
        else:
            o_lvl = raw["outline_level"]
            bold_val = raw["bold"] if raw["bold"] is not None else False
            n_id = raw["num_id"]
            n_ilvl = raw["num_ilvl"]

        resolved = {
            "name": raw["name"],
            "outline_level": o_lvl,
            "bold": bold_val,
            "num_id": n_id,
            "num_ilvl": n_ilvl,
        }
        cache[s_id] = resolved
        return resolved

    for s_id in raw_styles:
        resolve_style(s_id, set())

    pkg._adeu_style_cache = (cache, default_pstyle)
    return cache, default_pstyle


def _get_numbering_cache(part) -> Dict[str, Dict[int, str]]:
    """
    Parses word/numbering.xml once per package into
    {numId: {ilvl: numFmt}} (e.g. {"5": {0: "decimal", 1: "lowerLetter"}}).

    Used to distinguish bullet lists from ordered lists in the projection
    (QA 2026-07-18 M4). Missing part / malformed XML yields an empty cache,
    which projects every list with the bullet marker (the historical default).
    """
    if part is None:
        return {}
    if hasattr(part, "part"):
        part = part.part
    if not hasattr(part, "package"):
        return {}
    pkg = part.package
    if hasattr(pkg, "_adeu_numbering_cache"):
        return pkg._adeu_numbering_cache

    cache: Dict[str, Dict[int, str]] = {}
    numbering_root = None
    try:
        for p in pkg.parts:
            if str(p.partname).endswith("/numbering.xml"):
                if hasattr(p, "element"):
                    numbering_root = p.element
                else:
                    from docx.oxml import parse_xml

                    numbering_root = parse_xml(p.blob)
                break
    except Exception:
        numbering_root = None

    if numbering_root is not None:
        abstract_fmts: Dict[str, Dict[int, str]] = {}
        for abstract in numbering_root.findall(qn("w:abstractNum")):
            a_id = abstract.get(qn("w:abstractNumId"))
            if a_id is None:
                continue
            lvl_map: Dict[int, str] = {}
            for lvl in abstract.findall(qn("w:lvl")):
                ilvl_val = lvl.get(qn("w:ilvl"))
                fmt_el = lvl.find(qn("w:numFmt"))
                if ilvl_val is not None and ilvl_val.lstrip("-").isdigit() and fmt_el is not None:
                    fmt = fmt_el.get(qn("w:val"))
                    if fmt:
                        lvl_map[int(ilvl_val)] = fmt
            abstract_fmts[a_id] = lvl_map

        for num in numbering_root.findall(qn("w:num")):
            n_id = num.get(qn("w:numId"))
            a_ref = num.find(qn("w:abstractNumId"))
            if n_id is None or a_ref is None:
                continue
            a_id = a_ref.get(qn("w:val"))
            if a_id in abstract_fmts:
                cache[n_id] = abstract_fmts[a_id]

    pkg._adeu_numbering_cache = cache
    return cache


def get_list_marker(paragraph_part, num_id: Optional[str], ilvl: int) -> str:
    """
    Markdown marker for a list paragraph: '* ' for bullets, '1. ' for every
    numbered format (Markdown renderers renumber sequentially). Unknown
    numbering (no numbering.xml entry) keeps the historical '* '.
    """
    fmt = None
    if num_id is not None:
        lvl_map = _get_numbering_cache(paragraph_part).get(num_id)
        if lvl_map:
            fmt = lvl_map.get(ilvl)
            if fmt is None and lvl_map:
                # Fall back to the nearest defined level at or below ilvl.
                for lookup in range(ilvl, -1, -1):
                    if lookup in lvl_map:
                        fmt = lvl_map[lookup]
                        break
    if fmt is not None and fmt != "bullet":
        return "1. "
    return "* "


# --- Types ---
class DocxEvent(NamedTuple):
    type: str  # 'start', 'end', 'ref' (for comments); 'ins_start', etc.
    id: str
    author: Optional[str] = None
    date: Optional[str] = None


@dataclass(slots=True)
class ProjectedRun:
    """A run whose projected text and emphasis flags were computed during the
    SINGLE child walk `iter_paragraph_content` already performs.

    Why this exists: both Virtual Text twins used to walk every run's children
    a second time (once for `get_run_text`, effectively again for the style
    markers) after the event stream had already walked them to find drawings,
    fields and references. On the VVBIG stress document that second walk cost
    ~1.95 s of every projection and of every mapper rebuild (559 K runs, of
    which 97.9 % produce no event at all).

    It is a standalone slotted dataclass, avoiding python-docx `Run.__init__`
    wrapper allocation while exposing `_element`, `proj_text`, `proj_bold`, and
    `proj_italic` directly (along with `.text`, `.bold`, `.italic`, and `._parent`).
    """

    _element: Any
    proj_text: str
    proj_bold: bool
    proj_italic: bool
    #: The content controls enclosing this run, outermost first, or `()`.
    #:
    #: Carried on the run rather than derived later because the traversal is
    #: the only place that knows it cheaply - it already walks into every
    #: `w:sdt`, so maintaining a stack costs one push and one pop per control,
    #: whereas recovering the same fact afterwards means an ancestor walk per
    #: run and there are 559 K of them on the stress document.
    #:
    #: This deliberately tracks EVERY control, not just the anchored ones that
    #: project `{#cc:N}` tokens. The write gates (CC-4) must see picture,
    #: repeating and building-block controls too - a lock on one of those is
    #: just as real - and those emit no `sdt_start`/`sdt_end` events at all.
    #: Structure and projection are separate concerns; this is the structure.
    sdt_stack: Tuple[Any, ...] = ()

    @property
    def text(self) -> str:
        return self.proj_text

    @text.setter
    def text(self, value: str) -> None:
        self.proj_text = value
        Run(self._element, cast(Any, None)).text = value

    @property
    def bold(self) -> bool:
        return self.proj_bold

    @property
    def italic(self) -> bool:
        return self.proj_italic

    @property
    def _r(self):
        return self._element

    @property
    def _parent(self):
        p = self._element.getparent()
        while p is not None and p.tag != QN_W_P:
            p = p.getparent()
        if p is not None:
            return Paragraph(p, cast(Any, None))
        return None


ParagraphItem = Union[ProjectedRun, DocxEvent]


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


def get_paragraph_prefix(
    paragraph: Any,
    style_cache: Optional[dict] = None,
    default_pstyle: Optional[str] = None,
    part: Any = None,
) -> str:
    """
    Returns the Markdown prefix for a paragraph based on its style.
    Uses the Fast XML Cache to avoid python-docx performance penalties.
    """
    if part is None:
        part = _get_part_safely(paragraph)
    if style_cache is None:
        style_cache, default_pstyle = _get_style_cache(part)
    cache = style_cache
    p_el = paragraph._element if hasattr(paragraph, "_element") else paragraph
    pPr = p_el.find(QN_W_PPR)

    # 1. Check Outline Level on the paragraph itself (Structural Truth)
    if pPr is not None:
        oLvl = pPr.find(QN_W_OUTLINELVL)
        if oLvl is not None:
            val = oLvl.get(QN_W_VAL)
            if val and val.isdigit():
                lvl = int(val)
                if 0 <= lvl <= 8:
                    return "#" * (lvl + 1) + " "

    # 2. Get Style Name & properties from Cache
    style_id = default_pstyle
    if pPr is not None:
        pStyle = pPr.find(QN_W_PSTYLE)
        if pStyle is not None:
            style_id = pStyle.get(QN_W_VAL) or default_pstyle

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

    # 3. Check for List Formatting (direct paragraph numPr first, then the
    # style chain — Word's built-in List Bullet/List Number styles keep their
    # numPr in styles.xml, QA 2026-07-18 M4).
    list_num_id = None
    list_ilvl = None
    numbering_disabled = False
    if pPr is not None:
        numPr = pPr.find(QN_W_NUMPR)
        if numPr is not None:
            numId = numPr.find(QN_W_NUMID)
            if numId is not None:
                val = numId.get(QN_W_VAL)
                if val == "0":
                    # ECMA-376 §17.9.15: a direct numId of 0 REMOVES the
                    # numbering a style would otherwise apply.
                    numbering_disabled = True
                elif val:
                    list_num_id = val
                    ilvl = numPr.find(QN_W_ILVL)
                    if ilvl is not None:
                        val_attr = ilvl.get(QN_W_VAL)
                        if val_attr is not None and val_attr.isdigit():
                            list_ilvl = int(val_attr)
    if list_num_id is None and not numbering_disabled and style_info:
        style_num_id = style_info.get("num_id")
        if style_num_id:
            list_num_id = style_num_id
            if list_ilvl is None:
                list_ilvl = style_info.get("num_ilvl")
    if list_num_id is not None:
        level = list_ilvl if list_ilvl is not None else 0
        if part is None:
            part = _get_part_safely(paragraph)
        marker = get_list_marker(part, list_num_id, level)
        return ("    " * level) + marker

    # 4. Custom heading style name fallback.
    if style_name and style_name != "Normal":
        custom_level = _detect_heading_level_from_name(style_name)
        if custom_level is not None:
            return "#" * custom_level + " "

    # 5. Heuristic for "Normal" style headers
    if style_name is None or style_name == "Normal":
        # Table cells must not be classified as heuristic headings
        ancestor = p_el.getparent()
        is_inside_table = False
        while ancestor is not None:
            if ancestor.tag == QN_W_TC:
                is_inside_table = True
                break
            ancestor = ancestor.getparent()

        if not is_inside_table:
            text = paragraph.text.strip()
            if text and len(text) < 100:
                is_all_caps = text.isupper()

                is_bold = False
                if style_info and style_info.get("bold"):
                    is_bold = True
                else:
                    # Check if the first visible run is explicitly bold in XML
                    runs = p_el.findall(f".//{QN_W_R}")
                    for r in runs:
                        t = r.find(f".//{QN_W_T}")
                        if t is not None and t.text and t.text.strip():
                            rPr_run = r.find(QN_W_RPR)
                            if rPr_run is not None:
                                b = rPr_run.find(QN_W_B)
                                if b is not None:
                                    val = b.get(QN_W_VAL)
                                    if val not in ("0", "false", "off"):
                                        is_bold = True
                            break

                if is_all_caps and is_bold:
                    return "## "

    return ""


def get_run_style_markers(run: Run, is_heading: Optional[bool] = None) -> tuple[str, str]:
    """
    Returns markdown prefix/suffix for run formatting (bold/italic).
    Bypasses `run.bold` and `run.italic` attributes for massive OXML performance gains.
    """
    prefix = ""
    suffix = ""

    # Check explicitly defined run properties in XML
    rPr = run._element.find(QN_W_RPR)
    is_bold = False
    is_italic = False

    if rPr is not None:
        b = rPr.find(QN_W_B)
        if b is not None:
            val = b.get(QN_W_VAL)
            if val not in ("0", "false", "off"):
                is_bold = True

        i = rPr.find(QN_W_I)
        if i is not None:
            val = i.get(QN_W_VAL)
            if val not in ("0", "false", "off"):
                is_italic = True

    if is_heading is None:
        parent = run._parent
        is_heading = is_native_heading(parent) if isinstance(parent, Paragraph) else False

    # Nesting order: Bold outer, Italic inner -> **_text_**
    if is_bold and not is_heading:
        prefix += "**"
        suffix = "**" + suffix

    if is_italic:
        prefix += "_"
        suffix = "_" + suffix

    return prefix, suffix


def split_boundary_whitespace(text: str) -> tuple[str, str, str]:
    """
    Splits `text` into (leading_ws, core, trailing_ws). Emphasis markers must
    wrap only the core: `**The Supplier **` (a bold run with a trailing space)
    is malformed Markdown — CommonMark requires the closing delimiter to hug
    non-whitespace — and it poisons every downstream CriticMarkup consumer
    (QA 2026-07-19 F-03/F-10). A fully-whitespace text yields ("", "", text)
    so callers skip the markers entirely.
    """
    core = text.strip()
    if not core:
        return "", "", text
    lead_len = len(text) - len(text.lstrip())
    return text[:lead_len], text[lead_len : lead_len + len(core)], text[lead_len + len(core) :]


def compute_change_pair_map(states_list) -> dict:
    """
    Maps each tracked-change id in a merged meta bubble to the OTHER ids of
    its resolution group, rendered ready for the bubble line suffix
    (`uid -> "Chg:2"` / `uid -> "Chg:2, Chg:3"`).

    A replacement is stored as a contiguous same-author <w:del> + <w:ins>
    pair with two distinct w:id values, but both engines resolve such a group
    as ONE unit — accepting or rejecting either side decides the whole
    replacement. Projecting the two ids side by side with no linkage implied
    they were independently resolvable (QA 2026-07-19 ADEU-QA-004); every
    meta-block builder (Python/Node ingest + mapper) uses this map to
    annotate grouped lines as `(pairs with Chg:N)`.

    Grouping mirrors the engines' `_get_paired_nodes` walk: consecutive
    ins/del ids in bubble order group while the author stays the same; any
    state carrying NO active ins/del (a comment- or format-only run between
    changes — physical text separating the elements) breaks the group.
    `states_list` entries are (ins_map, del_map, comments_set, fmt_map)
    snapshots in document order, as accumulated by the ingest/mapper state
    machines.
    """
    groups: list = []
    current: list = []
    seen_ids: set = set()

    for ins_map, del_map, _comments_set, _fmt_map in states_list:
        if not ins_map and not del_map:
            # A run with only comment/format meta sits between the tracked
            # elements: they are not siblings, the engine will not group them.
            if current:
                groups.append(current)
                current = []
            continue
        state_new = []
        for uid, meta in ins_map.items():
            if uid not in seen_ids:
                state_new.append((uid, getattr(meta, "author", None) or "Unknown"))
        for uid, meta in del_map.items():
            if uid not in seen_ids:
                state_new.append((uid, getattr(meta, "author", None) or "Unknown"))
        for uid, author in state_new:
            seen_ids.add(uid)
            if current and current[-1][1] != author:
                groups.append(current)
                current = []
            current.append((uid, author))
    if current:
        groups.append(current)

    pair_map: dict = {}
    for group in groups:
        if len(group) < 2:
            continue
        for uid, _author in group:
            others = ", ".join(f"Chg:{u}" for u, _a in group if u != uid)
            pair_map[uid] = others
    return pair_map


def apply_formatting_to_segments(text: str, prefix: str, suffix: str) -> str:
    """
    Applies formatting markers to text, ensuring newlines are excluded from the
    formatting and boundary whitespace stays OUTSIDE the markers.
    Examples: "**A\nB**" -> "**A**\n**B**";  bold "The Supplier " ->
    "**The Supplier** " (never "**The Supplier **").
    """
    if not prefix and not suffix:
        return text
    if not text:
        return ""

    def wrap(segment: str) -> str:
        lead, core, trail = split_boundary_whitespace(segment)
        if not core:
            return segment
        return f"{lead}{prefix}{core}{suffix}{trail}"

    if "\n" not in text:
        return wrap(text)

    parts = text.split("\n")
    return "\n".join(wrap(p) if p else "" for p in parts)


def _revision_ballot_mark(r_element, text: str) -> Optional[str]:
    """The mark for a ballot run that sits inside a tracked revision.

    `None` when the run is not inside one, which is the ordinary case and
    keeps `w14:checked` authoritative.
    """
    node = r_element.getparent()
    while node is not None:
        if node.tag in (QN_W_INS, QN_W_DEL):
            return "x" if text in ("\u2611", "\u2612") else " "
        node = node.getparent()
    return None


def _has_ballot_run(sdt_el) -> bool:
    """Does this control contain a ballot-glyph run to hang the mark on?"""
    content = sdt_el.find(QN_W_SDTCONTENT)
    if content is None:
        return False
    for r in content.iter(QN_W_R):
        if "".join(t.text or "" for t in r.iter(QN_W_T)) in BALLOT_GLYPHS:
            return True
    return False


def _enclosing_checkbox(r_element, text: str, sdt_infos: Optional[dict]):
    """The checkbox control this run is the glyph of, or ``None``.

    Gated on the run's text being a ballot glyph FIRST, which is what keeps
    this affordable: the walk runs for roughly 7,700 runs in the largest
    corpus document rather than for all 559,000 of them.

    The gate is also the correctness boundary. `odot_uic_drywell` carries two
    bare ``U+2610`` runs in ordinary prose, outside any control, and the
    nearest enclosing ``w:sdt`` decides their fate: no control, or a control
    that is not a checkbox, means the glyph is prose and stays a glyph.
    Substituting on the character alone would fabricate two checkboxes in a
    document that has 19 real ones for them to hide among.
    """
    if not sdt_infos or text not in BALLOT_GLYPHS:
        return None
    node = r_element.getparent()
    while node is not None:
        if node.tag == QN_W_SDT:
            info = sdt_infos.get(id(node))
            return info if info is not None and info.cls == "checkbox" else None
        node = node.getparent()
    return None


def iter_paragraph_content(
    paragraph: Any, part: Any = None, sdt_infos: Optional[dict] = None
) -> Iterator[ParagraphItem]:
    """
    Iterates over the content of a paragraph, yielding both Runs and Comment events.
    This allows reconstruction of text with inline comments using CriticMarkup.

    ``sdt_infos`` is the ``id(element) -> SdtInfo`` map from
    ``content_controls.assign_ordinals``. Supplying it turns inline content
    controls from transparent wrappers into ``SdtEvent`` boundaries; omitting it
    preserves the historical behaviour exactly, which is what callers like
    outline and sanitize want (they must not grow anchor tokens).
    """
    doc_part = part if part is not None else _get_part_safely(paragraph)
    # The content controls currently open around the walk position, outermost
    # first. Stamped onto every `ProjectedRun` so consumers can answer "which
    # controls enclose this text" without an ancestor walk per run. Tracks
    # every control, anchored or not - see `ProjectedRun.sdt_stack`.
    sdt_stack: list = []
    # State for complex fields (w:fldChar)
    in_complex_field = False
    current_instr = ""
    hide_result = False

    def process_run_element(r_element):
        nonlocal in_complex_field, current_instr, hide_result

        c_id = None
        # Check for inline Tracked Formatting (w:rPrChange)
        rPr = r_element.find(QN_W_RPR)
        if rPr is not None:
            rPrChange = rPr.find(QN_W_RPRCHANGE)
            if rPrChange is not None:
                c_id = rPrChange.get(QN_W_ID)
                c_auth = rPrChange.get(QN_W_AUTHOR)
                c_date = rPrChange.get(QN_W_DATE)
                yield DocxEvent("fmt_start", c_id, c_auth, c_date)

        # Projected text + emphasis flags are accumulated in THIS loop rather
        # than by a second walk in each consumer — see ProjectedRun. These
        # branches must stay identical to utils.docx.run_text_and_flags; the
        # duplication is pinned by tests/test_run_fusion_equivalence.py.
        text_parts: list[str] = []
        is_bold = False
        is_italic = False

        # Iterate children once to handle references, fields, and text
        for child in r_element:
            tag = child.tag
            if tag == QN_W_T or tag == QN_W_DELTEXT:
                raw = child.text
                if raw:
                    # Normalize literal tabs to spaces to match w:tab behavior.
                    text_parts.append(raw.replace("\t", " ") if "\t" in raw else raw)
            elif tag == QN_W_RPR:
                b = child.find(QN_W_B)
                if b is not None and b.get(QN_W_VAL) not in _OFF_VALS:
                    is_bold = True
                i = child.find(QN_W_I)
                if i is not None and i.get(QN_W_VAL) not in _OFF_VALS:
                    is_italic = True
            elif tag == QN_W_TAB or tag == QN_W_PTAB:
                text_parts.append(" ")
            elif tag == QN_W_NOBREAKHYPHEN:
                text_parts.append("-")
            elif tag == QN_W_CR:
                text_parts.append("\n")
            elif tag == QN_W_BR:
                text_parts.append(_PAGE_BREAK_TOKEN if child.get(QN_W_TYPE) == "page" else "\n")
            elif tag == QN_W_DRAWING or tag == QN_W_OBJECT or tag == QN_W_PICT:
                # Read-only graphic marker, rendered as ![alt](docx-image:id);
                # floating text boxes disclose their text in the alt
                # (QA 2026-07-18 M5, QA 2026-07-23 customer C4).
                yield from _graphic_marker_events(child)
            elif tag == QN_MC_ALTERNATECONTENT:
                # Modern floating shapes (Word 2010+ text boxes, images)
                # arrive wrapped in mc:AlternateContent; without this branch
                # they project NOTHING — not even a marker (QA 2026-07-23
                # customer C4). Prefer the mc:Choice payload, fall back to
                # mc:Fallback, and project exactly one marker through the
                # same path as bare drawings/picts.
                payload = None
                for wrapper_tag in (QN_MC_CHOICE, QN_MC_FALLBACK):
                    for wrapper in child.findall(wrapper_tag):
                        payload = next(
                            (g for g in wrapper if g.tag in (QN_W_DRAWING, QN_W_OBJECT, QN_W_PICT)),
                            None,
                        )
                        if payload is not None:
                            break
                    if payload is not None:
                        break
                if payload is not None:
                    yield from _graphic_marker_events(payload)
            elif tag == QN_W_COMMENTREFERENCE:
                ref_id = child.get(QN_W_ID)
                if ref_id:
                    yield DocxEvent("ref", ref_id)
            elif tag == QN_W_FOOTNOTEREFERENCE:
                f_id = child.get(QN_W_ID)
                if f_id:
                    yield DocxEvent("footnote", f_id)
            elif tag == QN_W_ENDNOTEREFERENCE:
                e_id = child.get(QN_W_ID)
                if e_id:
                    yield DocxEvent("endnote", e_id)
            elif tag == QN_W_FLDCHAR:
                fld_type = child.get(QN_W_FLDCHARTYPE)
                if fld_type == "begin":
                    in_complex_field = True
                    current_instr = ""
                elif fld_type == "separate":
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
            elif tag == QN_W_INSTRTEXT and in_complex_field and not hide_result:
                if child.text:
                    current_instr += child.text

        # Yield Run (if not hidden), carrying the text/flags computed above so
        # consumers need not re-walk the run (see ProjectedRun).
        if not hide_result:
            if not text_parts:
                text = ""
            elif len(text_parts) == 1:
                text = text_parts[0]
            else:
                text = "".join(text_parts)
            cb_info = _enclosing_checkbox(r_element, text, sdt_infos)
            if cb_info is not None:
                # Spec §4. `[` and `]` are virtual chrome; the mark is a REAL
                # run-backed span, which is what "virtual + real span mix"
                # means. The substitution is one character for one character
                # (U+2612 -> `x`), so no offset arithmetic anywhere has to
                # learn about a width difference: the mapper already builds
                # spans from `proj_text`, so a run projecting `x` while its
                # `w:t` holds the glyph needs no new invariant.
                #
                # Done HERE, at run emission, rather than in the `w:sdt`
                # branch, because a checkbox control is not always inline. In
                # the corpus, 11 of `odot_uic_drywell`'s 19 checkboxes wrap a
                # whole `w:tc` (a checkbox column in a form table), and that
                # path never passes through the sdt branch. Substituting where
                # the run is emitted covers every path by construction.
                #
                # Emphasis is forced off: the mark is chrome, and a bold glyph
                # run would otherwise project `[**x**]` for the marker-
                # stripping passes to mangle (the QA F4/F22b class).
                #
                # The mark normally comes from `w14:checked` (the settled
                # value - see SdtInfo.checkbox_mark), but a run inside a
                # revision projects ITS OWN glyph instead. A tracked toggle
                # writes two glyph runs, an inserted new state and a deleted
                # old one, and the attribute can only describe one of them:
                # taking it for both rendered `{++[ ]++}{--[ ]--}`, a toggle
                # that appears to change nothing. Reading each revision run's
                # own glyph makes the pending change legible as
                # `{++[ ]++}{--[x]--}`, and the clean view keeps exactly one
                # box because the deleted half never reaches it (A4.6).
                mark = _revision_ballot_mark(r_element, text)
                if mark is None:
                    mark = cb_info.checkbox_mark
                yield SdtEvent("checkbox_start", cb_info)
                yield ProjectedRun(r_element, mark, False, False, tuple(sdt_stack))
                yield SdtEvent("checkbox_end", cb_info)
            else:
                yield ProjectedRun(r_element, text, is_bold, is_italic, tuple(sdt_stack))

        if c_id is not None:
            yield DocxEvent("fmt_end", c_id)

    def traverse_node(node):
        for child in node:
            tag = child.tag
            if tag == QN_W_R:
                # Standard run
                yield from process_run_element(child)
            elif tag == QN_W_INS:
                i_id = child.get(QN_W_ID)
                i_auth = child.get(QN_W_AUTHOR)
                i_date = child.get(QN_W_DATE)
                yield DocxEvent("ins_start", i_id, i_auth, i_date)
                yield from traverse_node(child)
                yield DocxEvent("ins_end", i_id)
            elif tag == QN_W_DEL:
                d_id = child.get(QN_W_ID)
                d_auth = child.get(QN_W_AUTHOR)
                d_date = child.get(QN_W_DATE)
                yield DocxEvent("del_start", d_id, d_auth, d_date)
                yield from traverse_node(child)
                yield DocxEvent("del_end", d_id)
            elif tag == QN_W_COMMENTRANGESTART:
                c_id = child.get(QN_W_ID)
                yield DocxEvent("start", c_id)
            elif tag == QN_W_COMMENTRANGEEND:
                c_id = child.get(QN_W_ID)
                yield DocxEvent("end", c_id)
            elif tag == QN_W_COMMENTREFERENCE:
                # Reference directly in paragraph
                pass
            elif tag == QN_W_HYPERLINK:
                rId = child.get(QN_R_ID)
                url = ""
                rels = getattr(doc_part, "rels", None) if doc_part is not None else None
                if rId and rels is not None:
                    try:
                        rel = rels[rId]
                        if rel.is_external:
                            url = rel.target_ref
                    except KeyError:
                        pass
                if url:
                    yield DocxEvent("hyperlink_start", rId, date=url)  # reuse date field for url
                yield from traverse_node(child)
                if url:
                    yield DocxEvent("hyperlink_end", rId, date=url)
            elif tag == QN_W_FLDSIMPLE:
                instr = child.get(QN_W_INSTR, "")
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
            elif tag == QN_W_BOOKMARKSTART:
                b_name = child.get(QN_W_NAME)
                if b_name and (not b_name.startswith("_") or b_name.startswith("_Ref")):
                    yield DocxEvent("bookmark", b_name)
            elif tag in (QN_W_SDT, QN_W_SMARTTAG, QN_W_SDTCONTENT):
                # Content controls were historically transparent here: the
                # boundary was erased and only the contents projected. When the
                # caller supplies the ordinal map (ingest and the mapper do;
                # outline/sanitize deliberately do not) the boundary becomes
                # visible as a pair of events, and an ANCHORED control's
                # contents are bracketed by them.
                info = sdt_infos.get(id(child)) if sdt_infos is not None else None
                # Track the control regardless of whether it ANCHORS. Anchoring
                # decides whether a `{#cc:N}` token projects; enclosure decides
                # which gates apply, and the two are not the same question. A
                # `sdtContentLocked` picture control projects no token and is
                # still locked.
                pushed = info is not None and tag == QN_W_SDT
                if pushed:
                    sdt_stack.append(info)
                try:
                    if info is not None and info.cls == "checkbox" and not _has_ballot_run(child):
                        # Degenerate control: Word always writes the glyph run,
                        # but a generator might not. Emit the whole token
                        # virtually so it stays three characters wide instead
                        # of collapsing to `[]`, which no edit surface expects.
                        #
                        # The NORMAL case is deliberately absent from this
                        # branch: a checkbox's glyph run substitutes itself
                        # where runs are emitted, so it works on every path
                        # that reaches a run — inline, in a cell, or wrapping
                        # one. See the run branch.
                        yield SdtEvent("checkbox_start", info)
                        yield SdtEvent("checkbox_mark", info)
                        yield SdtEvent("checkbox_end", info)
                    elif info is None or not info.anchored:
                        yield from traverse_node(child)
                    else:
                        yield SdtEvent("sdt_start", info)
                        if not info.showing_placeholder:
                            yield from traverse_node(child)
                        # Ghost text NEVER projects as body text (spec §3,
                        # A1.4). The placeholder run lives in sdtContent like
                        # any other run, so descending would emit "Click or tap
                        # here to enter text." as if the user had typed it. The
                        # bubble that replaces it is chrome, added by the
                        # consumer, because only the consumer knows whether
                        # this is the clean view.
                        yield SdtEvent("sdt_end", info)
                finally:
                    if pushed:
                        sdt_stack.pop()

    p_el = paragraph._element if hasattr(paragraph, "_element") else paragraph
    yield from traverse_node(p_el)


def get_visible_runs(paragraph: Paragraph):
    """
    Iterates over runs in a paragraph, including those inside <w:ins> tags.
    Effectively returns the 'Accepted Changes' view of the runs.
    Filters out dynamic page number fields ({PAGE}, {NUMPAGES}).
    """
    return [item for item in iter_paragraph_content(paragraph) if isinstance(item, ProjectedRun)]


def get_run_text_and_markers(r_element, is_heading: bool) -> tuple[str, str, str]:
    """Fused per-run projection step: returns (text, prefix, suffix) in ONE
    pass over the run element's children.

    Exactly equivalent to `get_run_text(run)` paired with
    `get_run_style_markers(run, is_heading)` — those two walked the run
    separately (a child loop plus rPr lookups), and both Virtual Text twins
    called both, once per run. On the VVBIG stress document (560K runs) the
    pair costs 5.21 us/run against 3.44 us/run fused, i.e. ~1.0 s of every
    projection and every mapper rebuild.

    Takes the raw lxml element rather than a python-docx `Run`, so callers on
    the hot path need not construct a wrapper just to read the run.

    Equivalence against the two original functions is pinned for every branch
    by tests/test_run_fusion_equivalence.py — keep that test passing rather
    than "fixing" this in isolation, since the twins' byte-identical output
    is a contract with downstream agents.

    NOTE: a variant adding a fast path for the dominant shape ([rPr, w:t] with
    no b/i — 95.2% of runs on VVBIG) measured NO faster (3.48 us/run): the
    `list(r_element)` it needs costs what the skipped branches save. Left out
    deliberately.
    """
    text, is_bold, is_italic = run_text_and_flags(r_element)
    prefix, suffix = markers_from_flags(is_bold, is_italic, is_heading)
    return text, prefix, suffix


def markers_from_flags(is_bold: bool, is_italic: bool, is_heading: bool) -> tuple[str, str]:
    """Markdown emphasis markers for a run, from its already-known flags.

    Split out from the run walk so the projection can carry cheap BOOLEANS
    through the event stream and derive the marker strings at the point of use
    — the stream cannot know `is_heading`, which is a property of the enclosing
    paragraph, and threading it in would force a second code path.

    Nesting order: bold outer, italic inner -> **_text_**. Headings suppress
    bold (the '## ' prefix already carries the emphasis).
    """
    prefix = ""
    suffix = ""
    if is_bold and not is_heading:
        prefix = "**"
        suffix = "**"
    if is_italic:
        prefix += "_"
        suffix = "_" + suffix
    return prefix, suffix


def run_text_and_flags(r_element) -> tuple[str, bool, bool]:
    """One walk of a run's children -> (text, is_bold, is_italic).

    NOTE: `process_run_element` deliberately INLINES these same branches into
    its own child loop, because the entire point of that fusion is to walk each
    run's children exactly once (it must `yield` events from the same loop, so
    it cannot delegate here without re-walking). That duplication is pinned by
    tests/test_run_fusion_equivalence.py, which cross-checks every value the
    event stream produces against this function. Change both, or neither.
    """
    is_bold = False
    is_italic = False
    parts: list[str] = []

    for child in r_element:
        tag = child.tag
        if tag == QN_W_T or tag == QN_W_DELTEXT:
            raw = child.text
            if raw:
                # Normalize literal tabs to spaces to match w:tab behavior.
                parts.append(raw.replace("\t", " ") if "\t" in raw else raw)
        elif tag == QN_W_RPR:
            b = child.find(QN_W_B)
            if b is not None and b.get(QN_W_VAL) not in _OFF_VALS:
                is_bold = True
            i = child.find(QN_W_I)
            if i is not None and i.get(QN_W_VAL) not in _OFF_VALS:
                is_italic = True
        elif tag == QN_W_TAB or tag == QN_W_PTAB:
            parts.append(" ")
        elif tag == QN_W_NOBREAKHYPHEN:
            parts.append("-")
        elif tag == QN_W_BR:
            parts.append(_PAGE_BREAK_TOKEN if child.get(QN_W_TYPE) == "page" else "\n")
        elif tag == QN_W_CR:
            parts.append("\n")

    if not parts:
        return "", is_bold, is_italic
    if len(parts) == 1:
        return parts[0], is_bold, is_italic
    return "".join(parts), is_bold, is_italic


def get_run_text(run: Run) -> str:
    """
    Extracts text from a run, converting <w:tab/> to spaces and <w:br/> to newlines.
    Standard run.text ignores these.
    """
    text = ""
    for child in run._element:
        if child.tag == QN_W_T or child.tag == QN_W_DELTEXT:
            # Fix 5.1: Normalize literal tabs to spaces to match w:tab behavior
            raw = child.text or ""
            text += raw.replace("\t", " ")
        elif child.tag == QN_W_TAB or child.tag == QN_W_PTAB:
            text += " "  # Convert tab to space
        elif child.tag == QN_W_NOBREAKHYPHEN:
            text += "-"
        elif child.tag == QN_W_BR:
            text += _PAGE_BREAK_TOKEN if child.get(QN_W_TYPE) == "page" else "\n"
        elif child.tag == QN_W_CR:
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
                    # Find the trailing text node of the current run to merge
                    # into. Text may only be concatenated into a node that is
                    # still the LAST content in document order: a w:tab/w:br
                    # between two text nodes is rendered content, and merging
                    # across it reorders the text ("Confidential<TAB>Page"
                    # became "ConfidentialPage<TAB>", QA round 3 finding 1.2).
                    last_t = None
                    for c in curr:
                        if c.tag in (qn("w:t"), qn("w:delText")):
                            last_t = c
                        elif c.tag != qn("w:rPr"):
                            last_t = None

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
                            last_t = child if child.tag in (qn("w:t"), qn("w:delText")) else None
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
    for container, _kind in iter_document_parts_with_kind(doc):
        yield container


def iter_document_parts_with_kind(doc: DocumentObject):
    """
    Like iter_document_parts, but yields (container, kind) where kind is one
    of "header" / "body" / "footer" / "footnotes" / "endnotes".

    The kind sequence defines the document's structural part layout. The
    projection flattens all parts into one string, so diff/apply need these
    kinds to refuse (or correctly re-anchor) edits that would otherwise cross
    an OPC part boundary — the QA 2026-07-18 C1 failure wrote a final body
    paragraph into word/footer1.xml.
    """

    # Resolved ONCE for the whole iteration: the python-docx `doc.settings`
    # property does a linear scan of the document part's relationships on
    # every access, and this document-level flag cannot change mid-iteration.
    # Evaluating it per section made part iteration O(sections × rels) — 14M
    # relationship probes / ~2.4s on a 1,772-section document.
    odd_and_even_pages = doc.settings.odd_and_even_pages_header_footer

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
        if odd_and_even_pages:
            even = getattr(section, f"even_page_{part_type_attr}")
            if not even.is_linked_to_previous:
                yield even

    # 1. Headers
    for section in doc.sections:
        for part in _iter_section_parts(section, "header"):
            yield part, "header"

    # 2. Main Body (The Document object itself acts as the container)
    yield doc, "body"

    # 3. Footers
    for section in doc.sections:
        for part in _iter_section_parts(section, "footer"):
            yield part, "footer"

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
        yield NotesPart(fn_part, "fn"), "footnotes"
    if en_part:
        yield NotesPart(en_part, "en"), "endnotes"


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
    for row in iter_table_rows(table):
        for cell in iter_row_cells(row):
            for item in iter_block_items(cell):
                if isinstance(item, Paragraph):
                    _coalesce_runs_in_paragraph(item)
                elif isinstance(item, Table):
                    _normalize_table(item)


def _iter_sdt_transparent_children(parent_elm, tag: Union[str, tuple], _depth: int = 0) -> Iterator[Any]:
    """
    Yields the direct children of `parent_elm` whose tag is (or is in) `tag`,
    descending transparently through any number of w:sdt / w:sdtContent
    wrapper levels.

    Word wraps table structure in structured document tags (content controls)
    whenever a template uses them, producing

        <w:tbl><w:sdt><w:sdtContent><w:tr>...          (row-level control)
        <w:tr><w:sdt><w:sdtContent><w:tc>...           (cell-level control)

    and, for repeating sections, an extra nesting level

        <w:sdt w15:repeatingSection>
          <w:sdtContent>
            <w:sdt w15:repeatingSectionItem><w:sdtContent><w:tr>...

    python-docx resolves Table.rows / _Row.cells with direct-child lookups
    (CT_Tbl.tr_lst = "./w:tr", CT_Row.tc_lst = "./w:tc"), so every one of these
    shapes was invisible to the Python projection while the Node engine
    traversed them. Descent stops at `tag`: a nested w:tbl inside a w:tc keeps
    its own rows to itself.

    Mirrors findChildrenSdtTransparent() in node/packages/core/src/docx/dom.ts.
    """
    if _depth > _MAX_SDT_NESTING_DEPTH:
        # Defensive: real content controls nest a couple of levels deep
        # (repeating sections). Anything past this is malformed or hostile,
        # and we must not blow the interpreter stack on untrusted input.
        return
    wanted = (tag,) if isinstance(tag, str) else tag
    for child in parent_elm.iterchildren():
        child_tag = child.tag
        if child_tag in wanted:
            yield child
        elif child_tag == QN_W_SDT:
            for content in child.iterchildren(QN_W_SDTCONTENT):
                yield from _iter_sdt_transparent_children(content, tag, _depth + 1)
        elif child_tag == QN_W_SDTCONTENT:
            # Defensive: a bare w:sdtContent without its w:sdt parent.
            yield from _iter_sdt_transparent_children(child, tag, _depth + 1)


def iter_table_row_elements(tbl_elem) -> Iterator[Any]:
    """
    `w:tr` children of `tbl_elem` in document order, including rows wrapped in
    content controls. Drop-in replacement for `tbl_elem.iterchildren(w:tr)`.

    CRITICAL: ingest.extract_table, DocumentMapper._map_table and the outline
    offset replays must all enumerate rows through this helper (or its
    object-level twin below) or their offset arithmetic drifts apart
    (Virtual Text contract).
    """
    return _iter_sdt_transparent_children(tbl_elem, QN_W_TR)


def iter_row_cell_elements(tr_elem) -> Iterator[Any]:
    """
    `w:tc` children of `tr_elem` in document order, including cells wrapped in
    content controls. Drop-in replacement for `tr_elem.iterchildren(w:tc)`.

    Unlike `iter_row_cells` this does NOT expand the python-docx layout grid
    (no gridSpan duplication, no vMerge resolution): it is the element-level
    projection path, matching the Node engine's one-entry-per-w:tc walk.
    """
    return _iter_sdt_transparent_children(tr_elem, QN_W_TC)


def iter_table_rows(table: Table) -> list:
    """
    Rows of `table` in document order, including rows wrapped in content
    controls. Drop-in replacement for `table.rows`.

    Object-level twin of `iter_table_row_elements`, for the callers that still
    work with python-docx wrappers (outline replay, normalization).
    """
    tbl = table._tbl
    if tbl.find(QN_W_SDT) is None:
        # Fast path — no content controls, defer to python-docx verbatim.
        return list(table.rows)
    return [_Row(tr, table) for tr in _iter_sdt_transparent_children(tbl, QN_W_TR)]


def iter_row_cells(row: _Row) -> list:
    """
    Cells of `row` in document order, including cells wrapped in content
    controls. Drop-in replacement for `row.cells`.

    Mirrors python-docx `_Row.cells` semantics exactly for the layout grid:
    a horizontally spanned cell is yielded once per grid column it covers, and
    a vertically merged continuation cell resolves to the content-bearing cell
    above it. Callers rely on that (they de-duplicate by cell identity).
    """
    tr = row._tr
    if tr.find(QN_W_SDT) is None:
        # Fast path — no content controls, defer to python-docx verbatim.
        return list(row.cells)

    table = row.table
    cells: list = []

    def _emit(tc, depth: int = 0) -> None:
        if tc.vMerge == "continue" and depth < _MAX_VMERGE_DEPTH:
            try:
                above = tc._tc_above
            except (ValueError, IndexError):
                # python-docx resolves the cell above via
                # "preceding-sibling::w:tr", which cannot see a row behind an
                # sdt wrapper. Rather than lose the cell (the very bug this
                # helper exists to fix), fall through and project it directly.
                above = None
            if above is not None:
                _emit(above, depth + 1)
                return
        cell = _Cell(tc, table)
        for _ in range(tc.grid_span):
            cells.append(cell)

    for tc in _iter_sdt_transparent_children(tr, QN_W_TC):
        _emit(tc)
    return cells


def iter_block_items(parent, emit_sdt: bool = False) -> Iterator[Union[Paragraph, Table, FootnoteItem, "BlockSdt"]]:
    """
    Yields Paragraph or Table objects in the order they appear in the XML.
    Supports Document, Header, Footer, and Cell objects.
    Recursion is left to the caller.

    With ``emit_sdt`` a block-level content control arrives as a
    :class:`BlockSdt` instead of being flattened into its contents, so the
    caller can bracket it. Opt-in for the reason given on
    ``_iter_block_children``.
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
            # Word reserves non-positive note ids (-1 separator, 0
            # continuation separator). Some generators omit the w:type
            # attribute on them, so filter by id as well — they must never
            # surface as user footnotes like "[^fn--1]:" (QA 2026-07-18 M6).
            note_id = child.get(qn("w:id"))
            try:
                if note_id is not None and int(note_id) <= 0:
                    continue
            except ValueError:
                pass
            yield FootnoteItem(child, parent, parent.note_type)
        return
    elif type(parent).__name__ == "FootnoteItem":
        parent_elm = parent._element
    else:
        # Header/Footer usually expose ._element or can be iterated
        if hasattr(parent, "_element"):
            parent_elm = parent._element
        else:
            parent_elm = parent

    for kind, child_elm in _iter_block_children(parent_elm, emit_sdt):
        if kind == "p":
            yield Paragraph(child_elm, parent)
        elif kind == "tbl":
            yield Table(child_elm, parent)
        elif kind == "sdt":
            # A block-level control, undescended. Callers that do not opt in
            # never see this (emit_sdt defaults False), so outline, domain,
            # sanitize and _normalize_table keep the historical transparent
            # behaviour.
            yield BlockSdt(child_elm)


def _iter_block_children(parent_elm, emit_sdt: bool = False) -> Iterator[Tuple[str, Any]]:
    """
    Yields (kind, child_elem) tuples among `parent_elm`'s children, descending
    into block-level w:sdt content controls.
    kind is "p", "tbl", or — only when `emit_sdt` — "sdt" for a block-level
    content control, yielded UNDESCENDED so the caller can wrap it.

    The boundary is opt-in because every other consumer of this iterator
    (outline, domain, sanitize, _normalize_table) treats block children as a
    flat list of paragraphs and tables; handing them a third kind unannounced
    would silently drop content in whichever branch fell through.
    """
    for child in parent_elm.iterchildren():
        tag = child.tag
        if tag == QN_W_P:
            yield ("p", child)
        elif tag == qn("w:tbl"):
            yield ("tbl", child)
        elif tag == qn("w:sdt"):
            sdt_content = child.find(qn("w:sdtContent"))
            if sdt_content is not None:
                if emit_sdt:
                    # Yield the control as ONE unit and do NOT descend: the
                    # consumer recurses into sdtContent itself, exactly as it
                    # already does for a Table. That is what makes a
                    # block-level control a single block that can be wrapped in
                    # its token lines — paired boundary events would instead
                    # have forced every consumer to grow a nesting stack and to
                    # re-derive the block separators inside it.
                    yield ("sdt", child)
                else:
                    yield from _iter_block_children(sdt_content, emit_sdt)


def suggest_sibling_docx(path: Union[str, Path], limit: int = 5) -> Tuple[list[str], int]:
    """
    Finds the sibling .docx files in `path.parent` closest to `path.name`.

    Returns `(closest_names, total_sibling_count)`. The names are capped at
    `limit`; the total is uncapped so callers can report how many candidates
    the cap withheld (e.g. the MCP "(+N more in <dir>)" suffix).
    """
    import difflib

    try:
        p = Path(path)
        parent = p.parent
        if not parent.exists() or not parent.is_dir():
            return [], 0
        siblings = sorted(f.name for f in parent.iterdir() if f.is_file() and f.suffix.lower() == ".docx")
        if not siblings:
            return [], 0
        return difflib.get_close_matches(p.name, siblings, n=limit, cutoff=0.0), len(siblings)
    except (OSError, ValueError):
        return [], 0


def strip_bom_from_docx_bytes(data: bytes) -> bytes:
    """
    Returns DOCX zip archive bytes with the UTF-8 BOM (ef bb bf) stripped
    from all XML and .rels files.

    PERF: BOM-free archives (the overwhelmingly common case) are returned
    as the ORIGINAL bytes after a 3-byte probe per XML entry — the archive
    is only decompressed+re-deflated when a BOM is actually present. The
    historical implementation re-zipped unconditionally, which cost seconds
    and a >1 GB RSS spike on large documents for a no-op.

    Validation contract (pinned by test_cli_features error-surface tests) is
    unchanged: bad zip signature, missing [Content_Types].xml, and an
    unparseable main document part raise the same ValueErrors as before.
    """
    import io
    import zipfile

    if not data.startswith(b"PK\x03\x04"):
        raise ValueError("not a valid DOCX file (got bad zip signature)")

    in_stream = io.BytesIO(data)
    if not zipfile.is_zipfile(in_stream):
        raise ValueError("not a valid DOCX file (got bad zip signature)")

    try:
        with zipfile.ZipFile(in_stream, "r") as z_in:
            if "[Content_Types].xml" not in z_in.namelist():
                raise ValueError("not a valid DOCX file (missing required Word parts)")

            # Probe pass: only the first 3 bytes of each XML/.rels entry are
            # decompressed. No BOM anywhere -> the input bytes ARE the result.
            has_bom = False
            for item in z_in.infolist():
                if item.filename.endswith(".xml") or item.filename.endswith(".rels"):
                    with z_in.open(item) as f:
                        if f.read(3) == b"\xef\xbb\xbf":
                            has_bom = True
                            break

            if has_bom:
                out_stream = io.BytesIO()
                with zipfile.ZipFile(out_stream, "w", zipfile.ZIP_DEFLATED) as z_out:
                    for item in z_in.infolist():
                        content = z_in.read(item.filename)
                        if item.filename.endswith(".xml") or item.filename.endswith(".rels"):
                            if content.startswith(b"\xef\xbb\xbf"):
                                content = content[3:]
                        z_out.writestr(item, content)
                sanitized_bytes = out_stream.getvalue()
            else:
                sanitized_bytes = data
    except Exception as e:
        if isinstance(e, ValueError) and "not a valid DOCX file" in str(e):
            raise
        if isinstance(e, zipfile.BadZipFile):
            raise ValueError("not a valid DOCX file (got bad zip signature)") from e
        raise

    _validate_docx_main_part(sanitized_bytes)
    return sanitized_bytes


def _validate_docx_main_part(sanitized_bytes: bytes) -> None:
    """
    Structural validation for strip_bom_from_docx_bytes. The historical
    check loaded the ENTIRE package through python-docx just to prove the
    file opens; the tested contract (test_cli_deeply_malformed_docx_errors)
    is that a well-formed zip whose main part is garbage XML raises the
    "corrupted or invalid OOXML structure" ValueError. An lxml parse of the
    main part alone proves the same thing at a fraction of the cost.
    Packages without a conventional word/document.xml (e.g. Flat OPC
    conversions with a different main-part name) fall back to the full
    python-docx load so valid-but-unconventional files are not rejected.
    """
    import io
    import zipfile

    from lxml import etree

    try:
        with zipfile.ZipFile(io.BytesIO(sanitized_bytes), "r") as z:
            if "word/document.xml" in z.namelist():
                etree.fromstring(z.read("word/document.xml"))
                return
    except etree.XMLSyntaxError as e:
        raise ValueError("not a valid DOCX file (corrupted or invalid OOXML structure)") from e
    except zipfile.BadZipFile as e:
        raise ValueError("not a valid DOCX file (got bad zip signature)") from e

    try:
        from adeu.utils.opc import load_document

        load_document(io.BytesIO(sanitized_bytes))
    except Exception as e:
        if isinstance(e, ValueError) and "not a valid DOCX file" in str(e):
            raise
        raise ValueError("not a valid DOCX file (corrupted or invalid OOXML structure)") from e
