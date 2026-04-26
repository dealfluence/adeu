from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.redline.engine import RedlineEngine


def test_suppress_inherited_removes_complex_scripts():
    """
    Bug #12: Suppressing inherited formatting must strip <w:bCs/> and <w:iCs/>
    to prevent visual styling mismatches in modern Word.
    """
    r_elem = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rPr.append(OxmlElement("w:b"))
    rPr.append(OxmlElement("w:bCs"))
    rPr.append(OxmlElement("w:i"))
    rPr.append(OxmlElement("w:iCs"))
    r_elem.append(rPr)

    engine = RedlineEngine.__new__(RedlineEngine)
    engine._apply_run_props(r_elem, props={}, suppress_inherited=True)

    assert len(rPr.findall(qn("w:b"))) == 0, "w:b should be stripped"
    assert len(rPr.findall(qn("w:i"))) == 0, "w:i should be stripped"
    assert len(rPr.findall(qn("w:bCs"))) == 0, "w:bCs should be stripped (Bug #12)"
    assert len(rPr.findall(qn("w:iCs"))) == 0, "w:iCs should be stripped (Bug #12)"
