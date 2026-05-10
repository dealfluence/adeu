from docx import Document

from adeu.mcp_components.tools.live_word import _build_mock_docx_stream


def test_missing_customxml_part_is_pruned():
    """
    Replicates the Live Word COM issue where Custom XML parts are declared but empty.
    The resulting DOCX zip shouldn't crash python-docx due to dangling .rels references.
    """
    flat_opc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<pkg:package xmlns:pkg="http://schemas.microsoft.com/office/2006/xmlPackage">\n'
        '  <pkg:part pkg:name="/_rels/.rels" '
        'pkg:contentType="application/vnd.openxmlformats-package.relationships+xml">\n'
        "    <pkg:xmlData>\n"
        '      <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '        <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>\n'
        "      </Relationships>\n"
        "    </pkg:xmlData>\n"
        "  </pkg:part>\n"
        "\n"
        '  <pkg:part pkg:name="/word/document.xml" '
        'pkg:contentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml">\n'
        "    <pkg:xmlData>\n"
        '      <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        "        <w:body><w:p><w:r><w:t>Hello Flat OPC</w:t></w:r></w:p></w:body>\n"
        "      </w:document>\n"
        "    </pkg:xmlData>\n"
        "  </pkg:part>\n"
        "\n"
        "  <!-- The document rels that point to the missing part -->\n"
        '  <pkg:part pkg:name="/word/_rels/document.xml.rels" '
        'pkg:contentType="application/vnd.openxmlformats-package.relationships+xml">\n'
        "    <pkg:xmlData>\n"
        '      <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        "        <!-- This target points to a file that will be dropped -->\n"
        '        <Relationship Id="rIdCustom" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml" '
        'Target="../customXml/itemProps1.xml"/>\n'
        "      </Relationships>\n"
        "    </pkg:xmlData>\n"
        "  </pkg:part>\n"
        "\n"
        "  <!-- The empty/self-closing part exactly as Word COM outputs it sometimes -->\n"
        '  <pkg:part pkg:name="/customXml/itemProps1.xml" '
        'pkg:contentType="application/vnd.openxmlformats-officedocument.customXmlProperties+xml" />\n'
        "</pkg:package>\n"
    )

    # Build the stream
    stream = _build_mock_docx_stream(flat_opc_xml)

    # Before the patch, this line threw:
    # KeyError: "There is no item named 'customXml/itemProps1.xml' in the archive"
    doc = Document(stream)

    # Verify the document actually loads its text
    assert doc.paragraphs[0].text == "Hello Flat OPC"
