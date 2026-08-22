# FILE: tests/sdt_fixtures.py
"""Minimal, hand-built DOCX packages containing content controls (`w:sdt`).

Why not python-docx: python-docx has no `w:sdt` API, and the properties that
matter here (`w:lock`, `w:showingPlcHdr`, `w:placeholder/w:docPart`,
`w:dataBinding`, `w14:checkbox`, `w:temporary`) live in `sdtPr` where only raw
XML reaches them. These builders write the OPC package directly.

The packages are deliberately small but *complete enough for Word to open them*
— which cost some debugging:

* A `w:placeholder/w:docPart` reference is inert without a glossary part. Word
  silently shows nothing, and (CC-6 f) will not re-instate a placeholder it
  cannot resolve; it substitutes whitespace instead. `glossary=` writes
  `word/glossary/document.xml` with a real `bbPlcHdr` doc part.
* A `w:dataBinding` is inert without the `customXml` item AND its
  `itemProps` part carrying the matching `ds:itemID`. Without the props part
  Word reports `XMLMapping.IsMapped == False` — which is the *dangling binding*
  case, useful on purpose, but not what a bound-control test wants.
* `w:rStyle w:val="PlaceholderText"` needs the style to exist in styles.xml or
  Word drops it on save, which would make ghost-run assertions lie.

See shared/fixtures/fixture-standard.md for the canonical
full fixture; these are the focused probes behind CC-6.
"""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
    'mc:Ignorable="w14 w15"><w:body>'
)
_FOOTER = "<w:sectPr/></w:body></w:document>"

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)

# The `w:lang` default is not cosmetic. With track changes on, Word stamps a
# proofing language onto every run that lacks one — as a FORMAT revision
# (`w:rPrChange`, wdRevisionProperty). A fixture without docDefaults therefore
# arrives carrying several revisions nobody asked for, before the test has done
# anything, and `Document.Revisions.Count` stops meaning what it looks like it
# means. Worse, it is order-dependent: Word only does it once another document
# has taught the instance a language, so the suite passes alone and fails after
# any other live-Word test. Pin the language and there is nothing to correct.
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:styles {W_NS}>"
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>'
    "</w:rPr></w:rPrDefault></w:docDefaults>"
    '<w:style w:type="character" w:styleId="PlaceholderText">'
    '<w:name w:val="Placeholder Text"/><w:uiPriority w:val="99"/><w:semiHidden/>'
    '<w:rPr><w:color w:val="808080"/></w:rPr></w:style>'
    "</w:styles>"
)

_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml"


def para(text: str) -> str:
    """A `w:p` holding one run of `text`."""
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def run(text: str) -> str:
    """A bare `w:r`, for building inline content around an inline sdt."""
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def make_sdt_xml(
    *,
    sdt_id: str | int = 100,
    alias: str | None = None,
    tag: str | None = None,
    content: str = "",
    sdt_type_xml: str = "<w:text/>",
    lock: str | None = None,
    temporary: bool = False,
    showing_plc_hdr: bool = False,
) -> str:
    """Builds a complete <w:sdt> element string with specified sdtPr properties."""
    parts = ["<w:sdt><w:sdtPr>"]
    if alias:
        parts.append(f'<w:alias w:val="{alias}"/>')
    if tag:
        parts.append(f'<w:tag w:val="{tag}"/>')
    parts.append(f'<w:id w:val="{sdt_id}"/>')
    if lock:
        parts.append(f'<w:lock w:val="{lock}"/>')
    if temporary:
        parts.append("<w:temporary/>")
    if showing_plc_hdr:
        parts.append("<w:showingPlcHdr/>")
    if sdt_type_xml:
        parts.append(sdt_type_xml)
    parts.append(f"</w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>")
    return "".join(parts)


def make_checkbox_sdt_xml(
    sdt_id: str | int,
    checked: bool,
    alias: str | None = None,
    tag: str | None = None,
    glyph: str | None = None,
) -> str:
    """Builds a w14:checkbox SDT element string."""
    val = "1" if checked else "0"
    if glyph is None:
        glyph = "\u2612" if checked else "\u2610"
    content = (
        '<w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr>'
        f"<w:t>{glyph}</w:t></w:r>"
        if glyph
        else ""
    )
    type_xml = (
        f'<w14:checkbox><w14:checked w14:val="{val}"/>'
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
        "</w14:checkbox>"
    )
    return make_sdt_xml(
        sdt_id=sdt_id, alias=alias or f"cb{sdt_id}", tag=tag or f"cb{sdt_id}", content=content, sdt_type_xml=type_xml
    )


def build_sdt_docx(
    path: Path,
    body: str,
    *,
    protection: str | None = None,
    protection_enforcement: str | None = "1",
    custom_xml: str | None = None,
    store_item_id: str | None = None,
    glossary: dict[str, str] | None = None,
    main_content_type: str | None = None,
    core_properties: dict[str, str] | None = None,
) -> Path:
    """Write a Word-openable package whose `w:body` is `body`.

    `protection` is a `w:documentProtection w:edit` value ("forms",
    "trackedChanges", "readOnly", "comments"); `protection_enforcement` is its
    `w:enforcement` attribute, where `None` omits the attribute (which OOXML
    reads as true) and `"0"` writes a configured-but-switched-off restriction.
    `custom_xml` + `store_item_id`
    wire a data store for `w:dataBinding`; pass `custom_xml=None` while leaving
    a `w:storeItemID` in the body to get a DANGLING binding. `glossary` maps
    doc-part name -> placeholder prose.

    `main_content_type` overrides the declared content type of
    `word/document.xml`, which is the ONLY thing distinguishing a `.dotx`
    template or a `.docm` macro-enabled document from a plain `.docx` (CC-11).
    Defaults to the plain-document type.

    `core_properties` writes `docProps/core.xml` — the package core properties,
    keyed by unprefixed local name (`title`, `subject`, `creator`, `keywords`,
    `description`). Word binds controls to these through a WELL-KNOWN store item
    id rather than a `customXml` item, which is the whole of CC-20.
    """
    doc_rels = [
        f'<Relationship Id="rId1" Type="{_REL_TYPE}/settings" Target="settings.xml"/>',
        f'<Relationship Id="rId2" Type="{_REL_TYPE}/styles" Target="styles.xml"/>',
    ]
    content_types = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        f'<Override PartName="/word/document.xml" ContentType="{main_content_type or f"{_CT}.document.main+xml"}"/>',
        f'<Override PartName="/word/settings.xml" ContentType="{_CT}.settings+xml"/>',
        f'<Override PartName="/word/styles.xml" ContentType="{_CT}.styles+xml"/>',
    ]

    if custom_xml is not None:
        doc_rels.append(f'<Relationship Id="rId3" Type="{_REL_TYPE}/customXml" Target="../customXml/item1.xml"/>')
        content_types.append(
            '<Override PartName="/customXml/itemProps1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.customXmlProperties+xml"/>'
        )
    root_rels_extra = ""
    if core_properties is not None:
        content_types.append(
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
            'openxmlformats-package.core-properties+xml"/>'
        )
        # Core properties hang off the PACKAGE root, not off word/document.xml.
        root_rels_extra = (
            '<Relationship Id="rIdCore" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
            'core-properties" Target="docProps/core.xml"/>'
        )
    if glossary:
        doc_rels.append(f'<Relationship Id="rId4" Type="{_REL_TYPE}/glossaryDocument" Target="glossary/document.xml"/>')
        content_types.append(
            f'<Override PartName="/word/glossary/document.xml" ContentType="{_CT}.document.glossary+xml"/>'
        )

    # `protection_enforcement=None` omits the attribute entirely, which is a
    # real Word shape and NOT the same as "0": the OOXML boolean rule makes an
    # absent attribute mean true. CC-4's reader is tested against all three.
    if protection:
        enforcement_attr = f' w:enforcement="{protection_enforcement}"' if protection_enforcement is not None else ""
        prot = f'<w:documentProtection w:edit="{protection}"{enforcement_attr}/>'
    else:
        prot = ""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(content_types)
            + "</Types>",
        )
        z.writestr("_rels/.rels", _ROOT_RELS.replace("</Relationships>", root_rels_extra + "</Relationships>"))
        z.writestr("word/document.xml", _HEADER + body + _FOOTER)
        z.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(doc_rels)
            + "</Relationships>",
        )
        # A UNIQUE w14:docId per package, and it is not decoration. Word's
        # identity for a document across saves is its docId, so packages that
        # share one are ONE document to Word: a live-Word test then inherits the
        # previous test's revision state and counts revisions that belong to
        # somebody else. (Same lesson as word_com.py's `_stage`, which can only
        # randomise a docId that is already there.)
        z.writestr(
            "word/settings.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:settings {W_NS} "
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'mc:Ignorable="w14 w15">'
            f"{prot}"
            # ST_LongHexNumber: keep the high bit clear or the id is out of range.
            f'<w14:docId w14:val="{uuid.uuid4().int % 0x7FFFFFFF + 1:08X}"/>'
            f'<w15:docId w15:val="{{{uuid.uuid4()}}}"/>'
            "</w:settings>",
        )
        z.writestr("word/styles.xml", _STYLES)

        if glossary:
            parts = "".join(
                "<w:docPart><w:docPartPr>"
                f'<w:name w:val="{name}"/>'
                '<w:category><w:name w:val="General"/><w:gallery w:val="placeholder"/></w:category>'
                '<w:types><w:type w:val="bbPlcHdr"/></w:types>'
                f'<w:guid w:val="{{00000000-0000-0000-0000-{index:012d}}}"/>'
                "</w:docPartPr><w:docPartBody>"
                f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
                "</w:docPartBody></w:docPart>"
                for index, (name, text) in enumerate(glossary.items(), start=1)
            )
            z.writestr(
                "word/glossary/document.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<w:glossaryDocument {W_NS}><w:docParts>{parts}</w:docParts></w:glossaryDocument>",
            )

        if core_properties is not None:
            # `dc:` for the Dublin Core terms, `cp:` for the package's own.
            dc = ("title", "subject", "creator", "description", "language")
            props = "".join(
                f"<{'dc' if k in dc else 'cp'}:{k}>{v}</{'dc' if k in dc else 'cp'}:{k}>"
                for k, v in core_properties.items()
            )
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
                'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + props + "</cp:coreProperties>",
            )

        if custom_xml is not None:
            z.writestr("customXml/item1.xml", custom_xml)
            z.writestr(
                "customXml/_rels/item1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{_REL_TYPE}/customXmlProps" Target="itemProps1.xml"/>'
                "</Relationships>",
            )
            z.writestr(
                "customXml/itemProps1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<ds:datastoreItem xmlns:ds="http://schemas.openxmlformats.org/officeDocument/'
                f'2006/customXml" ds:itemID="{store_item_id}"><ds:schemaRefs/></ds:datastoreItem>',
            )
    return path


def document_xml(path: Path) -> str:
    """`word/document.xml` of `path`, decoded."""
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


def custom_xml(path: Path) -> str | None:
    """The first `customXml/itemN.xml` of `path`, or None when there is none."""
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if name.startswith("customXml/item") and name.endswith(".xml") and "Props" not in name:
                return z.read(name).decode("utf-8")
    return None
