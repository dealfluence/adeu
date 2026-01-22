import io

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream


def test_repro_split_insertion_coalescing():
    """
    Scenario: Word stores "rapala" and " " as two separate runs inside ONE w:ins tag.

    XML:
      <w:ins w:id="415">
        <w:r><w:t>rapala</w:t></w:r>
        <w:r><w:t> </w:t></w:r>
      </w:ins>

    Current Behavior: {++rapala++}{++ ++}
    Desired Behavior: {++rapala ++}
    """
    doc = Document()
    p = doc.add_paragraph()

    # Manually construct the split insertion XML
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "415")
    ins.set(qn("w:author"), "Mikko")

    # Run 1: "rapala"
    r1 = OxmlElement("w:r")
    t1 = OxmlElement("w:t")
    t1.text = "rapala"
    r1.append(t1)
    ins.append(r1)

    # Run 2: " "
    r2 = OxmlElement("w:r")
    t2 = OxmlElement("w:t")
    t2.text = " "
    t2.set(qn("xml:space"), "preserve")
    r2.append(t2)
    ins.append(r2)

    p._element.append(ins)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # Act
    text = extract_text_from_stream(stream)

    # Assert
    # We want them merged
    assert "{++rapala ++}" in text
    # We do NOT want them split
    assert "{++rapala++}" not in text
