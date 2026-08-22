"""Loader for the shared 16-control content-control fixture.

The body XML lives in ONE place — ``shared/fixtures/cc_fixture.body.xml`` — read
by ``scripts/make_cc_fixture.py``, by this module, and by the node twin
(``corpusPath``-style resolution in ``test-utils.ts``). It is not transcribed
into either engine's tests, because hand-copied OOXML is precisely how the two
engines drift apart (PROGRESS.md 2026-08-21: the duplicated table XML in the
two ``repro_sdt_table_row_cell_invisibility`` suites).

Canonical listing and normative goldens:
``shared/fixtures/fixture-standard.md``.
"""

import io
from functools import lru_cache
from pathlib import Path

from docx.oxml import parse_xml

_HEADER = (
    '<w:document xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
    'mc:Ignorable="w14 w15"><w:body>'
)
_FOOTER = "<w:sectPr/></w:body></w:document>"


def cc_fixture_body_xml() -> str:
    """The normative body children, verbatim."""
    root = Path(__file__).resolve().parents[2]
    path = root / "shared" / "fixtures" / "cc_fixture.body.xml"
    if not path.is_file():  # pragma: no cover - repo layout invariant
        raise FileNotFoundError(f"shared content-control fixture missing: {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _document_xml() -> str:
    return _wrap_body(cc_fixture_body_xml())


def _wrap_body(body_xml: str) -> str:
    return _HEADER + body_xml + _FOOTER


def cc_fixture_body_element():
    """The parsed ``w:body`` element — enough for classification tests.

    Returns a FRESH tree per call: the ordinal-stability test needs two
    independent loads, and a cached element would make it assert nothing.
    """
    return parse_xml(_document_xml()).find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")


#: The store item id CC:10's `w:dataBinding` names in the shared body XML.
BOUND_STORE_ITEM_ID = "{A1B2C3D4-0000-0000-0000-000000000001}"

#: The `w:prefixMappings` SharePoint writes alongside a documentManagement
#: binding. `ns2` is a bare GUID, not a URL - that is what SharePoint emits for
#: a site content type, and it is why nothing may assume a URI scheme.
SHAREPOINT_PREFIX_MAPPINGS = (
    "xmlns:ns0='http://schemas.microsoft.com/office/2006/metadata/properties' "
    "xmlns:ns1='http://www.w3.org/2001/XMLSchema-instance' "
    "xmlns:ns2='2f9f1944-3a9b-49e1-93d3-d1cb06258e09'"
)
SHAREPOINT_XPATH = "/ns0:properties[1]/documentManagement[1]/ns2:CaseNum[1]"

#: A body carrying ONE bound control shaped the way Word really writes them.
#: Kept out of the 16-control shared body on purpose: appending there would
#: shift every later ordinal and rewrite dozens of assertions in both engines
#: for no gain, and this shape is only interesting to the binding tests.
SHAREPOINT_BOUND_BODY = (
    '<w:p><w:r><w:t xml:space="preserve">Case number: </w:t></w:r>'
    "<w:sdt><w:sdtPr>"
    '<w:alias w:val="Case Number"/><w:tag w:val="case_num"/><w:id w:val="301"/>'
    f'<w:dataBinding w:xpath="{SHAREPOINT_XPATH}"'
    f' w:storeItemID="{BOUND_STORE_ITEM_ID}"'
    f' w:prefixMappings="{SHAREPOINT_PREFIX_MAPPINGS}"/>'
    "<w:text/>"
    "</w:sdtPr><w:sdtContent>"
    '<w:r><w:t xml:space="preserve">2:24-cv-01234</w:t></w:r>'
    "</w:sdtContent></w:sdt>"
    '<w:r><w:t xml:space="preserve">.</w:t></w:r></w:p>'
)

#: The matching store: root prefixed, the intermediate step in NO namespace,
#: the leaf under the bare-GUID namespace. lxml's XPath CAN resolve this one,
#: but only when handed the prefix mappings - unaided it raises.
SHAREPOINT_STORE = (
    "<p:properties"
    " xmlns:p='http://schemas.microsoft.com/office/2006/metadata/properties'"
    " xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'"
    " xmlns:pc='2f9f1944-3a9b-49e1-93d3-d1cb06258e09'>"
    "<documentManagement><pc:CaseNum>2:24-cv-01234</pc:CaseNum></documentManagement>"
    "</p:properties>"
)

#: Two `CaseNum` children differing only by namespace. Local-name matching
#: alone would take the first and write the wrong column; the binding's
#: `ns2` prefix resolves to the second one's namespace and decides. This is
#: the only thing `prefixMappings` is consulted for.
SHAREPOINT_STORE_AMBIGUOUS = (
    "<p:properties"
    " xmlns:p='http://schemas.microsoft.com/office/2006/metadata/properties'"
    " xmlns:other='urn:some-other-content-type'"
    " xmlns:pc='2f9f1944-3a9b-49e1-93d3-d1cb06258e09'>"
    "<documentManagement>"
    "<other:CaseNum>DO-NOT-TOUCH</other:CaseNum>"
    "<pc:CaseNum>2:24-cv-01234</pc:CaseNum>"
    "</documentManagement></p:properties>"
)

#: The same data with a DEFAULT namespace in force, so the unprefixed
#: `documentManagement[1]` step lands in a namespace. This is the shape XPath
#: 1.0 cannot express - an unprefixed step means "no namespace" to it, and
#: Word does not mean that - so it resolves to nothing even WITH the prefix
#: mappings. It is the whole reason resolution matches on local name (CC-18).
SHAREPOINT_STORE_DEFAULT_NS = (
    "<p:properties"
    " xmlns:p='http://schemas.microsoft.com/office/2006/metadata/properties'"
    " xmlns='http://schemas.microsoft.com/office/2006/metadata/properties'"
    " xmlns:pc='2f9f1944-3a9b-49e1-93d3-d1cb06258e09'>"
    "<documentManagement><pc:CaseNum>2:24-cv-01234</pc:CaseNum></documentManagement>"
    "</p:properties>"
)


def cc_fixture_bytes(
    protection: str | None = None,
    body_xml: str | None = None,
    custom_xml: str | None = None,
    core_properties: dict[str, str] | None = None,
) -> bytes:
    """A complete minimal package, for projection-level tests.

    ``protection`` mirrors the ``cc_fixture_forms`` variant: pass ``"forms"``
    for ``<w:documentProtection w:edit="forms" w:enforcement="1"/>``.

    ``body_xml`` replaces the 16-control body, which A2.2 (a protected document
    with NO controls) and A2.3 (250 controls) both need. Mirrors the ``bodyXml``
    parameter the node twin already accepts.

    ``custom_xml`` adds a CustomXML data store carrying that root element,
    registered under the store item id CC:10's binding names. Without it the
    fixture's binding DANGLES by design (spec-set-field §6 wants both states
    tested), so A4.8's resolving half needs this variant.

    ``core_properties`` writes ``docProps/core.xml`` with those package core
    properties, keyed by unprefixed local name. Word binds controls to these
    through a WELL-KNOWN store item id rather than a customXml item (CC-20).
    """
    import zipfile

    prot = f'<w:documentProtection w:edit="{protection}" w:enforcement="1"/>' if protection else ""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="'
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/settings.xml" ContentType="'
            'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
            + (
                '<Override PartName="/customXml/itemProps1.xml" ContentType="'
                'application/vnd.openxmlformats-officedocument.customXmlProperties+xml"/>'
                if custom_xml is not None
                else ""
            )
            + (
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
                'openxmlformats-package.core-properties+xml"/>'
                if core_properties is not None
                else ""
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="'
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="word/document.xml"/>'
            # Core properties hang off the PACKAGE root, not off the document.
            + (
                '<Relationship Id="rIdCore" Type="'
                "http://schemas.openxmlformats.org/package/2006/relationships/metadata/"
                'core-properties" Target="docProps/core.xml"/>'
                if core_properties is not None
                else ""
            )
            + "</Relationships>",
        )
        if core_properties is not None:
            _dc = ("title", "subject", "creator", "description", "language")
            _props = "".join(
                f"<{'dc' if k in _dc else 'cp'}:{k}>{v}</{'dc' if k in _dc else 'cp'}:{k}>"
                for k, v in core_properties.items()
            )
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
                'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + _props + "</cp:coreProperties>",
            )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + (_document_xml() if body_xml is None else _wrap_body(body_xml)),
        )
        z.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="'
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"'
            ' Target="settings.xml"/>'
            + (
                '<Relationship Id="rId2" Type="'
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
                '" Target="../customXml/item1.xml"/>'
                if custom_xml is not None
                else ""
            )
            + "</Relationships>",
        )
        z.writestr(
            "word/settings.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings {w}>{prot}</w:settings>',
        )
        if custom_xml is not None:
            z.writestr(
                "customXml/item1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + custom_xml,
            )
            z.writestr(
                "customXml/itemProps1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<ds:datastoreItem xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"'
                f' ds:itemID="{BOUND_STORE_ITEM_ID}">'
                "<ds:schemaRefs/></ds:datastoreItem>",
            )
            z.writestr(
                "customXml/_rels/item1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="'
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps"
                '" Target="itemProps1.xml"/>'
                "</Relationships>",
            )
    return buf.getvalue()
