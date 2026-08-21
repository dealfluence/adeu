"""Build the standard content-control fixtures (cc_fixture.docx + forms-protected variant).

Canonical definition and goldens: specs/content-controls/acceptance/fixture-standard.md.
This script is a convenience for manual CLI probing — tests build the same XML in-memory
via each engine's fixture idiom. Writes into --outdir (default: current directory).
"""
import argparse
import os
import zipfile

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
    'mc:Ignorable="w14 w15"><w:body>'
)
FOOTER = '<w:sectPr/></w:body></w:document>'


_BODY_XML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "shared",
    "fixtures",
    "cc_fixture.body.xml",
)

# The body children are NOT inlined here. They live in one normative file that
# this script and BOTH engines' test suites read, because the fixture had
# already been transcribed by hand into scripts/ and into each engine's repro
# test — and hand-copied OOXML is exactly how the two engines drift apart
# (see PROGRESS.md 2026-08-21). Canonical listing and goldens:
# specs/content-controls/acceptance/fixture-standard.md.
with open(_BODY_XML, encoding="utf-8") as _f:
    BODY_XML = _f.read().strip()

DOCUMENT = HEADER + BODY_XML + FOOTER

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
    "</Types>"
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)

DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
    "</Relationships>"
)


def settings(protection: str | None) -> str:
    prot = (
        f'<w:documentProtection w:edit="{protection}" w:enforcement="1"/>'
        if protection
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:settings {W}>{prot}</w:settings>"
    )


def build(path: str, protection: str | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", DOCUMENT)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/settings.xml", settings(protection))
    print("wrote", path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outdir", default=".", help="output directory (default: cwd)")
    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    build(os.path.join(args.outdir, "cc_fixture.docx"))
    build(os.path.join(args.outdir, "cc_fixture_forms.docx"), protection="forms")
