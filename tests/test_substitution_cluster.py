import io

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine


def test_substitution_cluster_format():
    """
    Scenario:
    We manually construct a DOM with:
    1. A deleted run "Old" (ID=1)
    2. An inserted run "New" (ID=2)
    3. Both wrapped in a Comment (ID=100)

    Expectation:
    {--Old--}{++New++}{>>[Chg:1] Author
    [Chg:2] Author
    [Com:100] Author: Comment body<<}

    Validates:
    - No metadata block between -- and ++.
    - Metadata is merged at the end.
    - Canonical order: Chg first, then Com.
    """
    doc = Document()
    p = doc.add_paragraph()

    # 1. Comment Start
    c_start = OxmlElement("w:commentRangeStart")
    c_start.set(qn("w:id"), "100")
    p._element.append(c_start)

    # 2. Deletion (ID=1) containing "Old"
    del_run = OxmlElement("w:del")
    del_run.set(qn("w:id"), "1")
    del_run.set(qn("w:author"), "Alice")
    r_del = OxmlElement("w:r")
    t_del = OxmlElement("w:delText")
    t_del.text = "Old"
    r_del.append(t_del)
    del_run.append(r_del)
    p._element.append(del_run)

    # 3. Insertion (ID=2) containing "New"
    ins_run = OxmlElement("w:ins")
    ins_run.set(qn("w:id"), "2")
    ins_run.set(qn("w:author"), "Bob")
    r_ins = OxmlElement("w:r")
    t_ins = OxmlElement("w:t")
    t_ins.text = "New"
    r_ins.append(t_ins)
    ins_run.append(r_ins)
    p._element.append(ins_run)

    # 4. Comment End
    c_end = OxmlElement("w:commentRangeEnd")
    c_end.set(qn("w:id"), "100")
    p._element.append(c_end)

    # 5. Comment Reference (Standard Word structure)
    r_ref = OxmlElement("w:r")
    ref = OxmlElement("w:commentReference")
    ref.set(qn("w:id"), "100")
    r_ref.append(ref)
    p._element.append(r_ref)

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    # Pre-inject comment data into Comments Part so Ingest can read it
    engine = RedlineEngine(stream)
    # We cheat and use the engine's internal comment manager to inject the XML part content
    # matching ID 100
    _ = engine.comments_manager.comments_part  # Trigger creation

    # Manually append the comment node
    # <w:comment w:id="100" ...><w:p><w:r><w:t>The Comment</w:t></w:r></w:p></w:comment>
    c_node = OxmlElement("w:comment")
    c_node.set(qn("w:id"), "100")
    c_node.set(qn("w:author"), "Reviewer")
    cp = OxmlElement("w:p")
    cr = OxmlElement("w:r")
    ct = OxmlElement("w:t")
    ct.text = "The Comment"
    cr.append(ct)
    cp.append(cr)
    c_node.append(cp)
    engine.comments_manager.comments_part.element.append(c_node)

    # Save modified stream with comments part
    final_stream = engine.save_to_stream()

    # TEST: Extract text
    text = extract_text_from_stream(final_stream)

    # Assert Format
    expected_snippet = "{--Old--}{++New++}{>>"
    assert expected_snippet in text, f"Expected cluster format, got: {text}"

    # Assert Order
    # Find the metadata block content
    start = text.find("{>>") + 3
    end = text.find("<<", start)
    meta = text[start:end]

    lines = meta.split("\n")
    # Expect Chg lines first, then Com
    # [Chg:1] Alice
    # [Chg:2] Bob
    # [Com:100] Reviewer: The Comment

    assert "Chg:1" in lines[0] or "Chg:2" in lines[0]
    assert "Chg:1" in lines[1] or "Chg:2" in lines[1]
    assert "Com:100" in lines[2]

    assert "Alice" in meta
    assert "Bob" in meta
    assert "The Comment" in meta
