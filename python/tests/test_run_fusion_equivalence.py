"""Pins the fused per-run projection step against the two functions it
replaced, per docs/PERFORMANCE.md §3.6 ("pin the new algorithm against a
verbatim copy of the OLD algorithm").

`get_run_text_and_markers(r_element, is_heading)` must return exactly
`(get_run_text(run), *get_run_style_markers(run, is_heading))` for every run
shape, because both Virtual Text twins call it on their hot path and their
byte-identical output is a contract with downstream agents.

The reference implementations below are VERBATIM copies of the originals as
they stood before fusion, so this test keeps passing even if the originals are
later deleted or refactored.
"""

from docx import Document
from docx.oxml.ns import qn
from docx.text.run import Run

from adeu.utils.docx import (
    QN_W_B,
    QN_W_BR,
    QN_W_CR,
    QN_W_DELTEXT,
    QN_W_I,
    QN_W_RPR,
    QN_W_T,
    QN_W_TAB,
    QN_W_VAL,
    get_run_style_markers,
    get_run_text,
    get_run_text_and_markers,
)

# --------------------------------------------------------------------------
# Verbatim pre-fusion implementations (reference oracle).
# --------------------------------------------------------------------------


def old_get_run_text(run) -> str:
    text = ""
    for child in run._element:
        if child.tag == QN_W_T or child.tag == QN_W_DELTEXT:
            raw = child.text or ""
            text += raw.replace("\t", " ")
        elif child.tag == QN_W_TAB:
            text += " "
        elif child.tag == QN_W_BR:
            if child.get(qn("w:type")) == "page":
                text += '<w:br w:type="page"/>'
            else:
                text += "\n"
        elif child.tag == QN_W_CR:
            text += "\n"
    return text


def old_get_run_style_markers(run, is_heading) -> tuple[str, str]:
    prefix = ""
    suffix = ""
    rPr = run._element.find(QN_W_RPR)
    is_bold = False
    is_italic = False
    if rPr is not None:
        b = rPr.find(QN_W_B)
        if b is not None:
            val = b.get(QN_W_VAL)
            if val not in ("0", "false", "off"):
                is_bold = True
        i = rPr.find(QN_W_I)
        if i is not None:
            val = i.get(QN_W_VAL)
            if val not in ("0", "false", "off"):
                is_italic = True
    if is_bold and not is_heading:
        prefix += "**"
        suffix = "**" + suffix
    if is_italic:
        prefix += "_"
        suffix = "_" + suffix
    return prefix, suffix


# --------------------------------------------------------------------------
# Run shapes: one entry per branch of the fused function.
# --------------------------------------------------------------------------

RUN_XML = [
    ("plain text", "<w:r><w:t>hello</w:t></w:r>"),
    ("empty run", "<w:r/>"),
    ("empty w:t", "<w:r><w:t></w:t></w:r>"),
    ("bold", "<w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r>"),
    ("italic", "<w:r><w:rPr><w:i/></w:rPr><w:t>it</w:t></w:r>"),
    ("bold+italic", "<w:r><w:rPr><w:b/><w:i/></w:rPr><w:t>bi</w:t></w:r>"),
    ("bold val=0", '<w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>x</w:t></w:r>'),
    ("bold val=false", '<w:r><w:rPr><w:b w:val="false"/></w:rPr><w:t>x</w:t></w:r>'),
    ("bold val=off", '<w:r><w:rPr><w:b w:val="off"/></w:rPr><w:t>x</w:t></w:r>'),
    ("bold val=1", '<w:r><w:rPr><w:b w:val="1"/></w:rPr><w:t>x</w:t></w:r>'),
    ("italic val=0", '<w:r><w:rPr><w:i w:val="0"/></w:rPr><w:t>x</w:t></w:r>'),
    ("rPr without b/i", '<w:r><w:rPr><w:sz w:val="20"/></w:rPr><w:t>x</w:t></w:r>'),
    ("literal tab in text", "<w:r><w:t>a\tb</w:t></w:r>"),
    ("w:tab element", "<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t></w:r>"),
    ("w:br plain", "<w:r><w:t>a</w:t><w:br/><w:t>b</w:t></w:r>"),
    ("w:br page", '<w:r><w:t>a</w:t><w:br w:type="page"/><w:t>b</w:t></w:r>'),
    ("w:cr", "<w:r><w:t>a</w:t><w:cr/><w:t>b</w:t></w:r>"),
    ("delText", "<w:r><w:delText>gone</w:delText></w:r>"),
    ("delText + tab", "<w:r><w:delText>a\tb</w:delText><w:tab/></w:r>"),
    ("multiple w:t", "<w:r><w:t>a</w:t><w:t>b</w:t><w:t>c</w:t></w:r>"),
    ("drawing only", "<w:r><w:drawing/></w:r>"),
    ("bold + drawing only", "<w:r><w:rPr><w:b/></w:rPr><w:drawing/></w:r>"),
    ("commentReference", '<w:r><w:commentReference w:id="1"/></w:r>'),
    ("whitespace only", '<w:r><w:t xml:space="preserve"> </w:t></w:r>'),
    ("rPr after text", "<w:r><w:t>x</w:t><w:rPr><w:b/></w:rPr></w:r>"),
    (
        "bold + tab + br page",
        '<w:r><w:rPr><w:b/><w:i/></w:rPr><w:t>a</w:t><w:tab/><w:br w:type="page"/><w:t>b</w:t></w:r>',
    ),
]

NSDECL = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:xml="http://www.w3.org/XML/1998/namespace"'
)


def _make_run(xml: str):
    from docx.oxml import parse_xml

    doc = Document()
    p = doc.add_paragraph()
    el = parse_xml(xml.replace("<w:r>", f"<w:r {NSDECL}>", 1).replace("<w:r/>", f"<w:r {NSDECL}/>", 1))
    p._p.append(el)
    return Run(el, p)


def test_fused_matches_old_pair_for_every_run_shape():
    for label, xml in RUN_XML:
        run = _make_run(xml)
        for is_heading in (False, True):
            want_text = old_get_run_text(run)
            want_pre, want_suf = old_get_run_style_markers(run, is_heading)
            got = get_run_text_and_markers(run._element, is_heading)
            assert got == (want_text, want_pre, want_suf), (
                f"{label} (is_heading={is_heading}): fused={got!r} old={(want_text, want_pre, want_suf)!r}"
            )


def test_fused_matches_current_public_helpers():
    """The surviving public helpers must agree with the fused function too —
    they are still used off the hot path (lookahead, outline, sanitize)."""
    for label, xml in RUN_XML:
        run = _make_run(xml)
        for is_heading in (False, True):
            text, pre, suf = get_run_text_and_markers(run._element, is_heading)
            assert text == get_run_text(run), f"{label}: text drift"
            assert (pre, suf) == get_run_style_markers(run, is_heading), f"{label}: marker drift"


def test_old_reference_is_actually_exercised():
    """Guard against the oracle silently degenerating (e.g. all shapes empty)."""
    texts = set()
    markers = set()
    for _, xml in RUN_XML:
        run = _make_run(xml)
        texts.add(old_get_run_text(run))
        markers.add(old_get_run_style_markers(run, False))
    assert len(texts) > 8, f"oracle text coverage too thin: {texts}"
    assert {"", "**"} <= {m[0] for m in markers} or len(markers) >= 3, markers
    assert ("**", "**") in markers
    assert ("**_", "_**") in markers
