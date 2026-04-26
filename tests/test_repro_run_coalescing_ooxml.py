from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from adeu.utils.docx import _coalesce_runs_in_paragraph


def test_run_coalescing_ooxml_compliance():
    """
    Bug #5: _coalesce_runs_in_paragraph should concatenate <w:t> texts
    rather than appending multiple <w:t> siblings into a single <w:r>.
    """
    p_elem = OxmlElement("w:p")

    # Run 1: "Con"
    r1 = OxmlElement("w:r")
    t1 = OxmlElement("w:t")
    t1.text = "Con"
    r1.append(t1)
    p_elem.append(r1)

    # Run 2: "tract"
    r2 = OxmlElement("w:r")
    t2 = OxmlElement("w:t")
    t2.text = "tract"
    r2.append(t2)
    p_elem.append(r2)

    p = Paragraph(p_elem, None)

    _coalesce_runs_in_paragraph(p)

    runs = p_elem.findall(qn("w:r"))
    assert len(runs) == 1, "Should be coalesced into 1 run"

    t_nodes = runs[0].findall(qn("w:t"))
    assert len(t_nodes) == 1, f"Expected 1 <w:t> node, got {len(t_nodes)}! Invalid OOXML."
    assert t_nodes[0].text == "Contract", "Text should be concatenated"
