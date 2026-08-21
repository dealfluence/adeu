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


def p(text):
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def run(text):
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


BODY = []
BODY.append(p("SERVICES AGREEMENT (fixture)"))

# 1. Block-level rich text control, filled, unlocked
BODY.append(
    '<w:sdt><w:sdtPr><w:alias w:val="Indemnity Clause"/><w:tag w:val="indemnity"/>'
    '<w:id w:val="101"/></w:sdtPr><w:sdtContent>'
    + p("The Supplier shall indemnify the Client against all third-party claims.")
    + "</w:sdtContent></w:sdt>"
)

# 2. Inline plain-text control, EMPTY, showing Word's default placeholder
BODY.append(
    "<w:p>" + run("This Agreement is made between ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Client Name"/><w:tag w:val="client_name"/>'
    '<w:id w:val="102"/><w:showingPlcHdr/><w:text/></w:sdtPr><w:sdtContent>'
    '<w:r><w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr>'
    '<w:t>Click or tap here to enter text.</w:t></w:r>'
    "</w:sdtContent></w:sdt>"
    + run(" and the Government of Example.") + "</w:p>"
)

# 3. Inline plain-text control, filled
BODY.append(
    "<w:p>" + run("Counterparty: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Counterparty"/><w:tag w:val="counterparty"/>'
    '<w:id w:val="103"/><w:text/></w:sdtPr><w:sdtContent>'
    + run("ACME Corp") + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
)

# 4. Dropdown, filled with a list value
BODY.append(
    "<w:p>" + run("Governing law: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Governing Law"/><w:tag w:val="governing_law"/>'
    '<w:id w:val="104"/><w:dropDownList w:lastValue="Ontario">'
    '<w:listItem w:displayText="Ontario" w:value="ON"/>'
    '<w:listItem w:displayText="British Columbia" w:value="BC"/>'
    '<w:listItem w:displayText="Federal" w:value="FED"/>'
    "</w:dropDownList></w:sdtPr><w:sdtContent>"
    + run("Ontario") + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
)

# 5. Date picker, filled
BODY.append(
    "<w:p>" + run("Effective date: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Effective Date"/><w:tag w:val="effective_date"/>'
    '<w:id w:val="105"/><w:date w:fullDate="2026-01-15T00:00:00Z">'
    '<w:dateFormat w:val="yyyy-MM-dd"/><w:lid w:val="en-US"/></w:date></w:sdtPr>'
    "<w:sdtContent>" + run("2026-01-15") + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
)

# 6. Checkbox (w14), checked
BODY.append(
    "<w:p>" + run("Confidentiality applies: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Confidential"/><w:tag w:val="confidential"/>'
    '<w:id w:val="106"/><w14:checkbox><w14:checked w14:val="1"/>'
    '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
    '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>'
    '<w:sdtContent><w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" '
    'w:hAnsi="MS Gothic"/></w:rPr><w:t>☒</w:t></w:r></w:sdtContent></w:sdt>'
    + "</w:p>"
)

# 7. Content-locked control (sdtContentLocked = cannot edit contents in Word)
BODY.append(
    "<w:p>" + run("Fixed clause: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Payment Terms"/><w:tag w:val="fixed_clause"/>'
    '<w:id w:val="107"/><w:lock w:val="sdtContentLocked"/><w:text/></w:sdtPr>'
    "<w:sdtContent>" + run("Payment terms are Net 30 days.")
    + "</w:sdtContent></w:sdt>" + "</w:p>"
)

# 8. Group control wrapping boilerplate + one nested editable field
BODY.append(
    '<w:sdt><w:sdtPr><w:alias w:val="Standard Terms"/><w:tag w:val="std_terms"/>'
    '<w:id w:val="108"/><w:lock w:val="sdtLocked"/><w:group/></w:sdtPr><w:sdtContent>'
    + p("These standard terms are approved boilerplate and must not be modified.")
    + "<w:p>" + run("Notices to: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Notice Address"/><w:tag w:val="notice_address"/>'
    '<w:id w:val="109"/><w:text/></w:sdtPr><w:sdtContent>'
    + run("123 Main Street, Ottawa") + "</w:sdtContent></w:sdt>" + "</w:p>"
    + "</w:sdtContent></w:sdt>"
)

# 9. Data-bound control (content mirrors a CustomXML node)
BODY.append(
    "<w:p>" + run("Matter number: ")
    + '<w:sdt><w:sdtPr><w:alias w:val="Matter Number"/><w:tag w:val="matter_number"/>'
    '<w:id w:val="110"/><w:dataBinding w:xpath="/root[1]/matter[1]" '
    'w:storeItemID="{A1B2C3D4-0000-0000-0000-000000000001}"/><w:text/></w:sdtPr>'
    "<w:sdtContent>" + run("M-2026-001") + "</w:sdtContent></w:sdt>" + "</w:p>"
)

# 10. Repeating section with two items
BODY.append(
    '<w:sdt><w:sdtPr><w:alias w:val="Deliverables"/><w:tag w:val="deliverables"/>'
    '<w:id w:val="111"/><w15:repeatingSection/></w:sdtPr><w:sdtContent>'
    '<w:sdt><w:sdtPr><w:id w:val="112"/><w15:repeatingSectionItem/></w:sdtPr>'
    "<w:sdtContent>" + p("Deliverable: Initial report, due 2026-02-01.") + "</w:sdtContent></w:sdt>"
    '<w:sdt><w:sdtPr><w:id w:val="113"/><w15:repeatingSectionItem/></w:sdtPr>'
    "<w:sdtContent>" + p("Deliverable: Final report, due 2026-06-30.") + "</w:sdtContent></w:sdt>"
    "</w:sdtContent></w:sdt>"
)

# 11. Table exercising cell-level / row-level / in-cell-block SDTs
def cell(inner):
    return f'<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>{inner}</w:tc>'

BODY.append(
    "<w:tbl><w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/></w:tblPr>"
    "<w:tblGrid><w:gridCol w:w=\"2000\"/><w:gridCol w:w=\"2000\"/></w:tblGrid>"
    # row 1: plain cell + CELL-LEVEL sdt (sdtContent > w:tc)
    "<w:tr>" + cell(p("Role")) +
    '<w:sdt><w:sdtPr><w:tag w:val="cell_role"/><w:id w:val="201"/><w:text/></w:sdtPr>'
    "<w:sdtContent>" + cell(p("Contracting Officer")) + "</w:sdtContent></w:sdt>"
    "</w:tr>"
    # row 2: ROW-LEVEL sdt (sdtContent > w:tr)
    '<w:sdt><w:sdtPr><w:tag w:val="row_approver"/><w:id w:val="202"/></w:sdtPr><w:sdtContent>'
    "<w:tr>" + cell(p("Approver")) + cell(p("Jane Roe")) + "</w:tr>"
    "</w:sdtContent></w:sdt>"
    # row 3: block sdt INSIDE a cell wrapping the paragraph
    "<w:tr>" + cell(p("Notes")) +
    cell('<w:sdt><w:sdtPr><w:tag w:val="cell_notes"/><w:id w:val="203"/></w:sdtPr>'
         "<w:sdtContent>" + p("Approved without conditions.") + "</w:sdtContent></w:sdt>")
    + "</w:tr>"
    "</w:tbl>"
)

BODY.append(p("Signed by the parties below."))

DOCUMENT = HEADER + "".join(BODY) + FOOTER

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
