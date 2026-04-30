import subprocess

import docx
from docx.oxml import parse_xml


def test_extractor_ignores_tracked_formatting():
    """
    Reproduces Bug 10: Extractor fails to generate CriticMarkup for <w:rPrChange>.
    """
    d = docx.Document()
    p = d.add_paragraph("Test")
    r = p.runs[0]
    r.bold = True
    r._r.append(
        parse_xml(
            '<w:rPrChange xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'w:id="1" w:author="QA" w:date="2026-04-30T00:00:00Z"><w:rPr><w:b/></w:rPr></w:rPrChange>'
        )
    )
    d.save("test_fmt_track_pytest.docx")

    output = subprocess.check_output(["uv", "run", "adeu", "extract", "test_fmt_track_pytest.docx"]).decode("utf-8")

    # Expected: {==**Test**==}{>>[Chg:1] QA<<} or {++**Test**++}
    # Actual: **Test** (no tracking markup)
    assert "{=" in output or "{+" in output or "{>" in output, (
        "BUG: Tracked formatting change was completely ignored by the extractor."
    )
