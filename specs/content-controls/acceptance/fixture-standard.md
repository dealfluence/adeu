# Standard Fixture: `cc_fixture` (16 controls)

The canonical synthetic document for A1–A4. Engines build it from the `document.xml`
below with their native idioms (python: raw-XML `parse_xml` injection like
`test_repro_qa_customer_assessment_2026_07_23.py`; node: `test-utils.ts` raw OOXML node
injection). A convenience generator is committed at `scripts/make_cc_fixture.py`
(writes `cc_fixture.docx` + a `forms`-protected variant into the CWD for manual CLI
probing — never into the repo).

**The goldens in this file are normative**, including spacing. If an implementation
detail forces a deviation, stop and flag it in PROGRESS.md (README rule 4).

> **Correction, 2026-08-21 (Mikko, in response to a CC-1b flag).** GOLDEN-RAW
> originally omitted the `--- | ---` line from the table. That was a transcription
> error in this document, not an engine defect: both engines emit a GFM header
> divider after the first row of EVERY table (`ingest.py` `extract_table`, and its
> node twin), and have long before this initiative — without it the output is not a
> markdown table, just lines containing pipes. The divider is now present in
> GOLDEN-RAW. It sits after the header row and BEFORE the row-level CC:15 anchor
> line, because CC:15 wraps the second `w:tr` and the divider is synthetic chrome
> belonging to the table, not to any row.

## document.xml (body children, in order)

Root: `w:document` with namespaces `w`, `w14`, `w15`, `mc` (`mc:Ignorable="w14 w15"`).
Minimal package: `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`,
`word/_rels/document.xml.rels`, `word/settings.xml` (empty `w:settings`, or with
`<w:documentProtection w:edit="forms" w:enforcement="1"/>` for the protected variant).

```xml
<w:p><w:r><w:t xml:space="preserve">SERVICES AGREEMENT (fixture)</w:t></w:r></w:p>

<!-- CC:1 block richtext, filled -->
<w:sdt><w:sdtPr><w:alias w:val="Indemnity Clause"/><w:tag w:val="indemnity"/><w:id w:val="101"/></w:sdtPr>
  <w:sdtContent><w:p><w:r><w:t xml:space="preserve">The Supplier shall indemnify the Client against all third-party claims.</w:t></w:r></w:p></w:sdtContent></w:sdt>

<!-- CC:2 inline text, EMPTY with stock placeholder -->
<w:p><w:r><w:t xml:space="preserve">This Agreement is made between </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Client Name"/><w:tag w:val="client_name"/><w:id w:val="102"/><w:showingPlcHdr/><w:text/></w:sdtPr>
    <w:sdtContent><w:r><w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr><w:t>Click or tap here to enter text.</w:t></w:r></w:sdtContent></w:sdt>
  <w:r><w:t xml:space="preserve"> and the Government of Example.</w:t></w:r></w:p>

<!-- CC:3 inline text, filled -->
<w:p><w:r><w:t xml:space="preserve">Counterparty: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Counterparty"/><w:tag w:val="counterparty"/><w:id w:val="103"/><w:text/></w:sdtPr>
    <w:sdtContent><w:r><w:t xml:space="preserve">ACME Corp</w:t></w:r></w:sdtContent></w:sdt>
  <w:r><w:t xml:space="preserve">.</w:t></w:r></w:p>

<!-- CC:4 dropdown, filled -->
<w:p><w:r><w:t xml:space="preserve">Governing law: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Governing Law"/><w:tag w:val="governing_law"/><w:id w:val="104"/>
    <w:dropDownList w:lastValue="Ontario"><w:listItem w:displayText="Ontario" w:value="ON"/><w:listItem w:displayText="British Columbia" w:value="BC"/><w:listItem w:displayText="Federal" w:value="FED"/></w:dropDownList></w:sdtPr>
    <w:sdtContent><w:r><w:t xml:space="preserve">Ontario</w:t></w:r></w:sdtContent></w:sdt>
  <w:r><w:t xml:space="preserve">.</w:t></w:r></w:p>

<!-- CC:5 date, filled -->
<w:p><w:r><w:t xml:space="preserve">Effective date: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Effective Date"/><w:tag w:val="effective_date"/><w:id w:val="105"/>
    <w:date w:fullDate="2026-01-15T00:00:00Z"><w:dateFormat w:val="yyyy-MM-dd"/><w:lid w:val="en-US"/></w:date></w:sdtPr>
    <w:sdtContent><w:r><w:t xml:space="preserve">2026-01-15</w:t></w:r></w:sdtContent></w:sdt>
  <w:r><w:t xml:space="preserve">.</w:t></w:r></w:p>

<!-- CC:6 checkbox, checked -->
<w:p><w:r><w:t xml:space="preserve">Confidentiality applies: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Confidential"/><w:tag w:val="confidential"/><w:id w:val="106"/>
    <w14:checkbox><w14:checked w14:val="1"/><w14:checkedState w14:val="2612" w14:font="MS Gothic"/><w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>
    <w:sdtContent><w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr><w:t>☒</w:t></w:r></w:sdtContent></w:sdt></w:p>

<!-- CC:7 inline text, sdtContentLocked -->
<w:p><w:r><w:t xml:space="preserve">Fixed clause: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Payment Terms"/><w:tag w:val="fixed_clause"/><w:id w:val="107"/><w:lock w:val="sdtContentLocked"/><w:text/></w:sdtPr>
    <w:sdtContent><w:r><w:t xml:space="preserve">Payment terms are Net 30 days.</w:t></w:r></w:sdtContent></w:sdt></w:p>

<!-- CC:8 group (sdtLocked) wrapping 2 blocks; CC:9 nested inline text -->
<w:sdt><w:sdtPr><w:alias w:val="Standard Terms"/><w:tag w:val="std_terms"/><w:id w:val="108"/><w:lock w:val="sdtLocked"/><w:group/></w:sdtPr><w:sdtContent>
  <w:p><w:r><w:t xml:space="preserve">These standard terms are approved boilerplate and must not be modified.</w:t></w:r></w:p>
  <w:p><w:r><w:t xml:space="preserve">Notices to: </w:t></w:r>
    <w:sdt><w:sdtPr><w:alias w:val="Notice Address"/><w:tag w:val="notice_address"/><w:id w:val="109"/><w:text/></w:sdtPr>
      <w:sdtContent><w:r><w:t xml:space="preserve">123 Main Street, Ottawa</w:t></w:r></w:sdtContent></w:sdt></w:p>
</w:sdtContent></w:sdt>

<!-- CC:10 inline text, data-bound (store item deliberately ABSENT from the package: dangling binding) -->
<w:p><w:r><w:t xml:space="preserve">Matter number: </w:t></w:r>
  <w:sdt><w:sdtPr><w:alias w:val="Matter Number"/><w:tag w:val="matter_number"/><w:id w:val="110"/>
    <w:dataBinding w:xpath="/root[1]/matter[1]" w:storeItemID="{A1B2C3D4-0000-0000-0000-000000000001}"/><w:text/></w:sdtPr>
    <w:sdtContent><w:r><w:t xml:space="preserve">M-2026-001</w:t></w:r></w:sdtContent></w:sdt></w:p>

<!-- CC:11 repeating section with CC:12 + CC:13 items -->
<w:sdt><w:sdtPr><w:alias w:val="Deliverables"/><w:tag w:val="deliverables"/><w:id w:val="111"/><w15:repeatingSection/></w:sdtPr><w:sdtContent>
  <w:sdt><w:sdtPr><w:id w:val="112"/><w15:repeatingSectionItem/></w:sdtPr><w:sdtContent><w:p><w:r><w:t xml:space="preserve">Deliverable: Initial report, due 2026-02-01.</w:t></w:r></w:p></w:sdtContent></w:sdt>
  <w:sdt><w:sdtPr><w:id w:val="113"/><w15:repeatingSectionItem/></w:sdtPr><w:sdtContent><w:p><w:r><w:t xml:space="preserve">Deliverable: Final report, due 2026-06-30.</w:t></w:r></w:p></w:sdtContent></w:sdt>
</w:sdtContent></w:sdt>

<!-- Table: CC:14 cell-level, CC:15 row-level, CC:16 in-cell block -->
<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
  <w:tr><w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">Role</w:t></w:r></w:p></w:tc>
    <w:sdt><w:sdtPr><w:tag w:val="cell_role"/><w:id w:val="201"/><w:text/></w:sdtPr><w:sdtContent>
      <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">Contracting Officer</w:t></w:r></w:p></w:tc></w:sdtContent></w:sdt></w:tr>
  <w:sdt><w:sdtPr><w:tag w:val="row_approver"/><w:id w:val="202"/></w:sdtPr><w:sdtContent>
    <w:tr><w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">Approver</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">Jane Roe</w:t></w:r></w:p></w:tc></w:tr></w:sdtContent></w:sdt>
  <w:tr><w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">Notes</w:t></w:r></w:p></w:tc>
    <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>
      <w:sdt><w:sdtPr><w:tag w:val="cell_notes"/><w:id w:val="203"/></w:sdtPr><w:sdtContent><w:p><w:r><w:t xml:space="preserve">Approved without conditions.</w:t></w:r></w:p></w:sdtContent></w:sdt></w:tc></w:tr>
</w:tbl>

<w:p><w:r><w:t xml:space="preserve">Signed by the parties below.</w:t></w:r></w:p>
```

## GOLDEN-RAW — full-document projection, raw view (engine-level body text)

```
SERVICES AGREEMENT (fixture)

{#cc:1}
The Supplier shall indemnify the Client against all third-party claims.
{#/cc:1}

This Agreement is made between {#cc:2}{>>placeholder: Click or tap here to enter text.<<}{#/cc:2} and the Government of Example.

Counterparty: {#cc:3}ACME Corp{#/cc:3}.

Governing law: {#cc:4}Ontario{#/cc:4}.

Effective date: {#cc:5}2026-01-15{#/cc:5}.

Confidentiality applies: [x]

Fixed clause: {#cc:7 locked}Payment terms are Net 30 days.{#/cc:7}

{#cc:8 group}
These standard terms are approved boilerplate and must not be modified.

Notices to: {#cc:9}123 Main Street, Ottawa{#/cc:9}
{#/cc:8}

Matter number: {#cc:10 bound}M-2026-001{#/cc:10}

Deliverable: Initial report, due 2026-02-01.

Deliverable: Final report, due 2026-06-30.

Role | {#cc:14}Contracting Officer{#/cc:14}
--- | ---
{#cc:15}Approver | Jane Roe{#/cc:15}
Notes | {#cc:16}Approved without conditions.{#/cc:16}

Signed by the parties below.
```

## GOLDEN-CLEAN — clean view

Identical to GOLDEN-RAW except the CC:2 line (placeholder bubble dropped):

```
This Agreement is made between {#cc:2}{#/cc:2} and the Government of Example.
```

## GOLDEN-BANNER — surface header line (CLI extract full view / read_docx full mode)

```
> **Protection:** none · **Fields:** 16 content controls — 1 empty · 2 locked · 1 bound
```

(Surfaces append their own fields-mode hint after this — not part of the golden.)

## GOLDEN-LEDGER — `mode="fields"` body

```
# Fields: cc_fixture.docx
Protection: none · 16 content controls — 1 empty · 2 locked · 1 bound

CC:1   richtext  "Indemnity Clause" (tag: indemnity) — p1 — value: "The Supplier shall indemnify the Client against all third-party claims."
CC:2   text  "Client Name" (tag: client_name) — p1 — EMPTY — placeholder: "Click or tap here to enter text."
CC:3   text  "Counterparty" (tag: counterparty) — p1 — value: "ACME Corp"
CC:4   dropdown  "Governing Law" (tag: governing_law) — p1 — value: "Ontario" — options: Ontario | British Columbia | Federal
CC:5   date  "Effective Date" (tag: effective_date) — p1 — value: "2026-01-15" — format: yyyy-MM-dd
CC:6   checkbox  "Confidential" (tag: confidential) — p1 — checked
CC:7   text  "Payment Terms" (tag: fixed_clause) — p1 — LOCKED (contents) — value: "Payment terms are Net 30 days."
CC:8   group  "Standard Terms" (tag: std_terms) — p1 — LOCKED (group) — wraps 2 blocks, 1 nested field
CC:9   text  "Notice Address" (tag: notice_address) — p1 — in CC:8 — value: "123 Main Street, Ottawa"
CC:10  text  "Matter Number" (tag: matter_number) — p1 — BOUND → /root[1]/matter[1] — value: "M-2026-001"
CC:11  repeating  "Deliverables" (tag: deliverables) — p1 — 2 items
CC:12  item — p1 — in CC:11 — wraps 1 block
CC:13  item — p1 — in CC:11 — wraps 1 block
CC:14  text  (tag: cell_role) — p1 — table cell — value: "Contracting Officer"
CC:15  richtext  (tag: row_approver) — p1 — table row — value: "Approver | Jane Roe"
CC:16  richtext  (tag: cell_notes) — p1 — value: "Approved without conditions."
```

Notes: the `CC:` column pads to the widest ordinal (+2 spaces); two spaces between class
and alias; single em-dash (`—`) separators; value previews cap at 80 chars + `…`
(no line here hits the cap — CC:1's value is 72 chars).

## Variant: `cc_fixture_forms` 

Same body; `word/settings.xml` carries
`<w:documentProtection w:edit="forms" w:enforcement="1"/>`. Banner/ledger protection
segment reads `fill-in-forms only (enforced)`. Used throughout A3.
